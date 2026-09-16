from __future__ import annotations

import logging
import shutil
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Sequence

LOGGER = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS env_metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id TEXT NOT NULL,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
    temperature REAL,
    humidity REAL,
    co2_ppm INTEGER,
    is_synced INTEGER DEFAULT 0 CHECK (is_synced IN (0, 1))
);
CREATE TABLE IF NOT EXISTS hvac_status (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id TEXT NOT NULL,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
    hvac_state INTEGER,
    power_w REAL,
    is_synced INTEGER DEFAULT 0 CHECK (is_synced IN (0, 1))
);
CREATE TABLE IF NOT EXISTS camera_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id TEXT NOT NULL,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
    image_type TEXT NOT NULL CHECK (image_type IN ('RGB', 'IR')),
    file_path TEXT NOT NULL,
    is_synced INTEGER DEFAULT 0 CHECK (is_synced IN (0, 1))
);
CREATE INDEX IF NOT EXISTS idx_env_unsynced ON env_metrics(is_synced, id);
CREATE INDEX IF NOT EXISTS idx_hvac_unsynced ON hvac_status(is_synced, id);
CREATE INDEX IF NOT EXISTS idx_camera_unsynced ON camera_logs(is_synced, id);
"""


class LocalDatabase:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self._memory_connection: sqlite3.Connection | None = None
        if self.path == ":memory:":
            self._memory_connection = sqlite3.connect(
                ":memory:", check_same_thread=False
            )
            self._memory_connection.row_factory = sqlite3.Row
        else:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = self._memory_connection or sqlite3.connect(self.path, timeout=20.0)
        connection.row_factory = sqlite3.Row
        if self.path != ":memory:":
            try:
                connection.execute("PRAGMA synchronous=NORMAL;")
            except Exception:
                pass
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            if self._memory_connection is None:
                connection.close()

    def close(self) -> None:
        if self._memory_connection is not None:
            self._memory_connection.close()
            self._memory_connection = None

    def initialize(self) -> None:
        with self.connect() as connection:
            if self.path != ":memory:":
                try:
                    connection.execute("PRAGMA journal_mode=WAL;")
                except Exception:
                    pass
            connection.executescript(SCHEMA)

    def insert_env(
        self,
        device_id: str,
        temperature: float | None,
        humidity: float | None,
        co2_ppm: int | None,
    ) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                """INSERT INTO env_metrics
                (device_id, temperature, humidity, co2_ppm) VALUES (?, ?, ?, ?)""",
                (device_id, temperature, humidity, co2_ppm),
            )
            return int(cursor.lastrowid)

    def insert_hvac(
        self, device_id: str, hvac_state: int, power_w: float | None
    ) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                """INSERT INTO hvac_status (device_id, hvac_state, power_w)
                VALUES (?, ?, ?)""",
                (device_id, hvac_state, power_w),
            )
            return int(cursor.lastrowid)

    def insert_camera(self, device_id: str, image_type: str, file_path: str) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                """INSERT INTO camera_logs (device_id, image_type, file_path)
                VALUES (?, ?, ?)""",
                (device_id, image_type, file_path),
            )
            return int(cursor.lastrowid)

    def unsynced(self, table: str, limit: int = 500) -> list[dict[str, Any]]:
        if table not in {"env_metrics", "hvac_status", "camera_logs"}:
            raise ValueError("invalid table")
        with self.connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM {table} WHERE is_synced = 0 ORDER BY id LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_synced(self, table: str, ids: Sequence[int]) -> None:
        if table not in {"env_metrics", "hvac_status", "camera_logs"}:
            raise ValueError("invalid table")
        if not ids:
            return
        placeholders = ",".join("?" for _ in ids)
        with self.connect() as connection:
            connection.execute(
                f"UPDATE {table} SET is_synced = 1 WHERE id IN ({placeholders})",
                tuple(ids),
            )

    def get_counts(self) -> dict[str, int]:
        with self.connect() as connection:
            env_total = connection.execute("SELECT COUNT(*) FROM env_metrics").fetchone()[0]
            env_unsynced = connection.execute("SELECT COUNT(*) FROM env_metrics WHERE is_synced = 0").fetchone()[0]

            hvac_total = connection.execute("SELECT COUNT(*) FROM hvac_status").fetchone()[0]
            hvac_unsynced = connection.execute("SELECT COUNT(*) FROM hvac_status WHERE is_synced = 0").fetchone()[0]

            camera_rgb_total = connection.execute("SELECT COUNT(*) FROM camera_logs WHERE image_type = 'RGB'").fetchone()[0]
            camera_rgb_unsynced = connection.execute("SELECT COUNT(*) FROM camera_logs WHERE image_type = 'RGB' AND is_synced = 0").fetchone()[0]

            camera_ir_total = connection.execute("SELECT COUNT(*) FROM camera_logs WHERE image_type = 'IR'").fetchone()[0]
            camera_ir_unsynced = connection.execute("SELECT COUNT(*) FROM camera_logs WHERE image_type = 'IR' AND is_synced = 0").fetchone()[0]

            total_records = env_total + hvac_total + camera_rgb_total + camera_ir_total
            total_unsynced = env_unsynced + hvac_unsynced + camera_rgb_unsynced + camera_ir_unsynced

            return {
                "env_total": env_total,
                "env_unsynced": env_unsynced,
                "hvac_total": hvac_total,
                "hvac_unsynced": hvac_unsynced,
                "camera_rgb_total": camera_rgb_total,
                "camera_rgb_unsynced": camera_rgb_unsynced,
                "camera_ir_total": camera_ir_total,
                "camera_ir_unsynced": camera_ir_unsynced,
                "total_records": total_records,
                "total_unsynced": total_unsynced,
            }

    def get_latest_env(self, limit: int = 15) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT id, device_id, timestamp, temperature, humidity, co2_ppm, is_synced "
                "FROM env_metrics ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_latest_hvac(self, limit: int = 15) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT id, device_id, timestamp, hvac_state, power_w, is_synced "
                "FROM hvac_status ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_latest_cameras(
        self, limit: int = 10, image_type: str | None = None
    ) -> list[dict[str, Any]]:
        with self.connect() as connection:
            if image_type:
                rows = connection.execute(
                    "SELECT id, device_id, timestamp, image_type, file_path, is_synced "
                    "FROM camera_logs WHERE image_type = ? ORDER BY id DESC LIMIT ?",
                    (image_type, limit),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT id, device_id, timestamp, image_type, file_path, is_synced "
                    "FROM camera_logs ORDER BY id DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [dict(row) for row in rows]

    def purge_old_synced_data(self, retention_days: int = 14) -> dict[str, int]:
        """Safely delete synced photos from disk and prune old synced records from SQLite.

        Only purges records where is_synced == 1 and timestamp is older than retention_days.
        Unsynced records are NEVER deleted by this scheduled retention task.
        """
        if retention_days < 1:
            retention_days = 14

        deleted_images = 0
        deleted_camera_rows = 0
        deleted_env_rows = 0
        deleted_hvac_rows = 0

        cutoff = f"-{retention_days} days"

        with self.connect() as connection:
            # 1. Find synced images older than retention_days and delete physical files
            old_cams = connection.execute(
                "SELECT file_path FROM camera_logs WHERE is_synced = 1 AND timestamp < datetime('now', ?)",
                (cutoff,),
            ).fetchall()

            for row in old_cams:
                fpath = row["file_path"]
                try:
                    p = Path(fpath)
                    if p.is_file():
                        p.unlink(missing_ok=True)
                        deleted_images += 1
                except Exception:
                    pass

            # Safe direct DELETE without SQLITE_LIMIT_VARIABLE_NUMBER placeholder risk
            cur_cam = connection.execute(
                "DELETE FROM camera_logs WHERE is_synced = 1 AND timestamp < datetime('now', ?)",
                (cutoff,),
            )
            deleted_camera_rows = cur_cam.rowcount

            # 2. Prune old synced env metrics
            cur_env = connection.execute(
                "DELETE FROM env_metrics WHERE is_synced = 1 AND timestamp < datetime('now', ?)",
                (cutoff,),
            )
            deleted_env_rows = cur_env.rowcount

            # 3. Prune old synced hvac status
            cur_hvac = connection.execute(
                "DELETE FROM hvac_status WHERE is_synced = 1 AND timestamp < datetime('now', ?)",
                (cutoff,),
            )
            deleted_hvac_rows = cur_hvac.rowcount

        return {
            "deleted_images": deleted_images,
            "deleted_camera_rows": deleted_camera_rows,
            "deleted_env_rows": deleted_env_rows,
            "deleted_hvac_rows": deleted_hvac_rows,
        }

    def enforce_disk_limit(
        self,
        min_free_mb: int = 1500,
        target_free_mb: int = 2000,
        image_dir: str | Path | None = None,
    ) -> int:
        """Emergency disk safety watermark guard to prevent SD card exhaustion during long offline periods.

        If available disk space drops below min_free_mb, deletes oldest photos until
        free disk space reaches target_free_mb. Synced photos are purged first; if still
        low, oldest unsynced photos are pruned to protect the host OS from read-only crashes.
        Numerical telemetry (env_metrics/hvac_status) is NEVER deleted here.
        """
        check_dir = Path(image_dir) if image_dir else Path(self.path).parent
        try:
            total, used, free = shutil.disk_usage(check_dir)
            total_mb = total // (1024 * 1024)
            free_mb = free // (1024 * 1024)
        except Exception:
            return 0

        # Adapt threshold if partition total size is smaller than min_free_mb
        effective_min_free = min(min_free_mb, int(total_mb * 0.15)) if total_mb > 0 else min_free_mb
        effective_target_free = min(target_free_mb, int(total_mb * 0.25)) if total_mb > 0 else target_free_mb

        if free_mb >= effective_min_free:
            return 0

        LOGGER.warning(
            "[Disk Safety Guard] Low disk space detected (%d MB free < %d MB limit). "
            "Purging oldest photos to restore %d MB safety margin...",
            free_mb, effective_min_free, effective_target_free,
        )

        deleted_count = 0
        with self.connect() as connection:
            # Order: synced photos first (is_synced DESC), then oldest id (id ASC)
            while free_mb < target_free_mb:
                candidates = connection.execute(
                    "SELECT id, file_path, is_synced FROM camera_logs ORDER BY is_synced DESC, id ASC LIMIT 100"
                ).fetchall()
                if not candidates:
                    break

                ids_to_delete = []
                for row in candidates:
                    ids_to_delete.append(row["id"])
                    try:
                        p = Path(row["file_path"])
                        if p.is_file():
                            p.unlink(missing_ok=True)
                            deleted_count += 1
                    except Exception:
                        pass

                if ids_to_delete:
                    placeholders = ",".join("?" for _ in ids_to_delete)
                    connection.execute(
                        f"DELETE FROM camera_logs WHERE id IN ({placeholders})",
                        tuple(ids_to_delete),
                    )

                try:
                    _, _, free = shutil.disk_usage(check_dir)
                    free_mb = free // (1024 * 1024)
                except Exception:
                    break

        if deleted_count > 0:
            LOGGER.info(
                "[Disk Safety Guard] Purged %d oldest photos. Free disk space restored to %d MB.",
                deleted_count, free_mb,
            )
        return deleted_count
