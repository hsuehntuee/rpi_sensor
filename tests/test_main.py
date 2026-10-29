from unittest.mock import Mock

from src.main import build_camera_task, build_scd41_task, build_sensor_task, guarded


def test_one_sensor_failure_preserves_other_reading(database):
    climate = Mock()
    climate.read.side_effect = RuntimeError("timing error")
    co2 = Mock()
    co2.read.return_value = {"co2_ppm": 700}

    build_sensor_task(database, "device-1", climate, co2)()

    row = database.unsynced("env_metrics")[0]
    assert row["temperature"] is None
    assert row["humidity"] is None
    assert row["co2_ppm"] == 700


def test_camera_failures_do_not_prevent_other_camera(database):
    rgb = Mock()
    rgb.capture.side_effect = RuntimeError("camera missing")
    infrared = Mock()
    infrared.capture.return_value = "/data/ir.jpg"

    build_camera_task(database, "device-1", rgb, infrared)()

    rows = database.unsynced("camera_logs")
    assert len(rows) == 1
    assert rows[0]["image_type"] == "IR"


def test_guarded_task_does_not_escape():
    guarded("broken", Mock(side_effect=RuntimeError("failure")))()


def test_scd41_task_skips_when_no_new_sample(database):
    sensor = Mock()
    sensor.read.side_effect = [
        None,
        {"temperature": 25.0, "humidity": 55.0, "co2_ppm": 800},
    ]
    task = build_scd41_task(database, "device-1", sensor)

    task()
    assert database.unsynced("env_metrics") == []
    task()
    assert database.unsynced("env_metrics")[0]["co2_ppm"] == 800


def test_single_camera_miss_is_not_logged(database, caplog):
    rgb = Mock()
    rgb.capture.side_effect = [RuntimeError("missed"), "/data/rgb.jpg"]
    infrared = Mock()
    infrared.capture.return_value = "/data/ir.jpg"
    task = build_camera_task(database, "device-1", rgb, infrared)
    task()
    task()
    assert not any("camera" in r.getMessage() for r in caplog.records)


def test_repeated_camera_failure_is_logged_once(database, caplog):
    rgb = Mock()
    rgb.capture.side_effect = RuntimeError("camera missing")
    infrared = Mock()
    infrared.capture.return_value = "/data/ir.jpg"
    task = build_camera_task(database, "device-1", rgb, infrared)
    for _ in range(3):
        task()
    assert sum("RGB camera capture failed" in r.getMessage() for r in caplog.records) == 1
