from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Sequence

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
