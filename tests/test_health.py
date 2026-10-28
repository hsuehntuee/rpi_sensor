from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

import pytest

from src.config import Settings
from src.web import health
from src.web.health import collect_health

ALL_OK_HW = {"scd41": "Hardware Active", "camera_rgb": "PiCamera Active", "camera_ir": "FLIR Lepton Active"}


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(device_id="dev", server_url="http://s", api_key="k" * 20,
                    database_path=tmp_path / "db.sqlite", image_dir=tmp_path)


@pytest.fixture(autouse=True)
def plenty_of_disk(monkeypatch):
    monkeypatch.setattr(health.shutil, "disk_usage", lambda _: (128 * 2**30, 28 * 2**30, 100 * 2**30))


def titles(result):
    return {(a["level"], a["title"]) for a in result["alerts"]}


def fresh_data(database):
    database.insert_env("dev", 25.0, 50.0, 500)
    database.insert_camera("dev", "RGB", "/x.jpg")
    database.insert_camera("dev", "IR", "/y.jpg")


def test_all_good_has_no_alerts(settings, database):
    fresh_data(database)
    for table in ("env_metrics", "camera_logs"):
        database.mark_synced(table, [r["id"] for r in database.unsynced(table)])
    result = collect_health(settings, database, ALL_OK_HW)
    assert result["alerts"] == []
    assert result["disk"]["free_mb"] == 100 * 1024


def test_low_disk_and_purge_of_unsynced_photos_are_critical(settings, database, monkeypatch):
    monkeypatch.setattr(health.shutil, "disk_usage", lambda _: (128 * 2**30, 127 * 2**30, 1 * 2**30))
    database._record_disk_purge({"time": datetime.now(timezone.utc).isoformat(), "deleted_photos": 40,
                                 "unsynced_deleted": 12, "free_mb_before": 1000, "free_mb_after": 2100})
    result = collect_health(settings, database, ALL_OK_HW)
    assert ("critical", "儲存空間即將用完") in titles(result)
    assert ("critical", "空間不足，系統已自動刪除未上傳的照片") in titles(result)
    assert result["alerts"][0]["contact"] is True


def test_stale_camera_needs_contact_but_upload_backlog_does_not_alert(settings, database):
    fresh_data(database)  # 全部未上傳
    later = datetime.now(timezone.utc) + timedelta(hours=30)
    result = collect_health(settings, database, ALL_OK_HW, now=later)
    assert ("critical", "RGB 相機沒有新資料") in titles(result)
    assert all("Server" not in a["title"] for a in result["alerts"])
    assert all(a["contact"] for a in result["alerts"])


def test_missing_hardware_and_disconnected_esp32(settings, database):
    fresh_data(database)
    remote = Mock()
    remote.status.side_effect = RuntimeError("找不到 ESP32")
    hw = {**ALL_OK_HW, "camera_ir": "Standalone / Emulated"}
    result = collect_health(settings, database, hw, ac_remote=remote)
    assert ("critical", "未偵測到熱影像相機 (FLIR Lepton)") in titles(result)
    assert ("warning", "冷氣遙控器 (ESP32) 未連線") in titles(result)


def test_a_failing_check_becomes_an_alert(settings, database, monkeypatch):
    monkeypatch.setattr(database, "get_activity", Mock(side_effect=RuntimeError("db locked")))
    result = collect_health(settings, database, ALL_OK_HW)
    assert ("warning", "監控項目「資料與硬體」讀取失敗") in titles(result)
    assert any("db locked" in a["detail"] for a in result["alerts"])


def test_disk_guard_records_unsynced_photo_loss(database, monkeypatch, tmp_path):
    from src.db import local_sqlite
    database.insert_camera("dev", "RGB", str(tmp_path / "a.jpg"))
    database.insert_camera("dev", "RGB", str(tmp_path / "b.jpg"))
    database.mark_synced("camera_logs", [database.unsynced("camera_logs")[0]["id"]])
    usage = iter([(32 * 2**30, 0, 1 * 2**30), (32 * 2**30, 0, 3 * 2**30)])  # 先剩 1GB，刪完剩 3GB
    monkeypatch.setattr(local_sqlite.shutil, "disk_usage", lambda _: next(usage))
    database.enforce_disk_limit(min_free_mb=1500, image_dir=tmp_path)
    purge = database.get_last_disk_purge()
    assert purge["unsynced_deleted"] == 1
    assert purge["free_mb_before"] == 1024


def test_only_contact_alerts_are_logged_once(settings, database, monkeypatch, caplog):
    import logging
    health._logged_contact_alerts.clear()
    remote = Mock()
    remote.status.side_effect = RuntimeError("找不到 ESP32")  # 可自行處理：不寫 log
    monkeypatch.setattr(health.shutil, "disk_usage", lambda _: (128 * 2**30, 127 * 2**30, 1 * 2**30))  # 需聯繫
    fresh_data(database)
    with caplog.at_level(logging.WARNING, logger="src.web.health"):
        for _ in range(3):
            collect_health(settings, database, ALL_OK_HW, ac_remote=remote)
    lines = [r.getMessage() for r in caplog.records]
    assert len(lines) == 1 and "儲存空間即將用完" in lines[0]
