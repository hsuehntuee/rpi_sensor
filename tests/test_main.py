from unittest.mock import Mock

from src.main import build_camera_task, build_scd41_task, build_sensor_task, guarded


def test_sensor_task_writes_env_and_hvac(database):
    climate = Mock()
    climate.read.return_value = {"temperature": 25.4, "humidity": 60.1}
    co2 = Mock()
    co2.read.return_value = {"co2_ppm": 450}
    hvac = Mock()
    hvac.read_status.return_value = {"hvac_state": 1, "power_w": 1200.5}

    build_sensor_task(database, "device-1", climate, co2, hvac)()

    assert database.unsynced("env_metrics")[0]["co2_ppm"] == 450
    assert database.unsynced("hvac_status")[0]["power_w"] == 1200.5


def test_hvac_failure_records_offline_status(database):
    climate = Mock()
    climate.read.return_value = {"temperature": 25.4, "humidity": 60.1}
    co2 = Mock()
    co2.read.return_value = {"co2_ppm": 450}
    hvac = Mock()
    hvac.read_status.side_effect = RuntimeError("offline")

    build_sensor_task(database, "device-1", climate, co2, hvac)()

    assert database.unsynced("hvac_status")[0]["hvac_state"] == -1


def test_one_sensor_failure_preserves_other_reading(database):
    climate = Mock()
    climate.read.side_effect = RuntimeError("timing error")
    co2 = Mock()
    co2.read.return_value = {"co2_ppm": 700}
    hvac = Mock()
    hvac.read_status.return_value = {"hvac_state": 0, "power_w": None}

    build_sensor_task(database, "device-1", climate, co2, hvac)()

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
