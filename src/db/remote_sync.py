from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

from .local_sqlite import LocalDatabase


class RemoteSync:
    def __init__(
        self,
        database: LocalDatabase,
        server_url: str,
        device_id: str,
        api_key: str,
        timeout: float = 10,
        session: Any = requests,
    ) -> None:
        if not server_url.strip():
            raise ValueError("server_url is required")
        self.database = database
        self.endpoint = f"{server_url.rstrip('/')}/api/v1/sync/metrics"
        self.image_endpoint = f"{server_url.rstrip('/')}/api/v1/sync/images"
        self.device_id = device_id
        self.headers = {"X-API-Key": api_key}
        self.timeout = timeout
        self.session = session

    @staticmethod
    def _public(row: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
        result = {field: row[field] for field in fields}
        timestamp = result.get("timestamp")
        if isinstance(timestamp, str) and timestamp:
            result["timestamp"] = timestamp.replace(" ", "T") + (
                "" if timestamp.endswith("Z") else "Z"
            )
        return result

    def sync_metrics(self, batch_limit: int = 150) -> int:
        env_rows = self.database.unsynced("env_metrics", limit=batch_limit)
        hvac_rows = self.database.unsynced("hvac_status", limit=batch_limit)
        if not env_rows and not hvac_rows:
            return 0
        payload = {
            "device_id": self.device_id,
            "sync_timestamp": datetime.now(timezone.utc).isoformat(),
            "env_data": [
                self._public(
                    row, ("timestamp", "temperature", "humidity", "co2_ppm")
                )
                for row in env_rows
            ],
            "hvac_data": [
                self._public(row, ("timestamp", "hvac_state", "power_w"))
                for row in hvac_rows
            ],
        }
        timeout = max(self.timeout, 30.0)
        response = self.session.post(
            self.endpoint,
            json=payload,
            headers=self.headers,
            timeout=timeout,
        )
        response.raise_for_status()
        self.database.mark_synced("env_metrics", [row["id"] for row in env_rows])
        self.database.mark_synced("hvac_status", [row["id"] for row in hvac_rows])
        return len(env_rows) + len(hvac_rows)

    def sync_images(self) -> int:
        rows = self.database.unsynced("camera_logs", limit=50)
        synced = 0
        timeout = max(self.timeout, 30.0)
        for row in rows:
            path = Path(row["file_path"])
            if not path.is_file():
                self.database.mark_synced("camera_logs", [row["id"]])
                continue
            timestamp = self._public(row, ("timestamp",))["timestamp"]
            with path.open("rb") as image:
                response = self.session.post(
                    self.image_endpoint,
                    data={
                        "device_id": row["device_id"],
                        "timestamp": timestamp,
                        "image_type": row["image_type"],
                    },
                    files={"image": (path.name, image, "image/jpeg")},
                    headers=self.headers,
                    timeout=timeout,
                )
            response.raise_for_status()
            self.database.mark_synced("camera_logs", [row["id"]])
            synced += 1
        return synced

    def sync_all(self) -> int:
        return self.sync_metrics() + self.sync_images()
