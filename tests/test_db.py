def test_crud_and_sync_flags(database):
    env_id = database.insert_env("device-1", 25.4, 60.1, 450)
    hvac_id = database.insert_hvac("device-1", 1, 1200.5)
    camera_id = database.insert_camera("device-1", "RGB", "/data/a.jpg")

    assert database.unsynced("env_metrics")[0]["id"] == env_id
    assert database.unsynced("hvac_status")[0]["id"] == hvac_id
    assert database.unsynced("camera_logs")[0]["id"] == camera_id

    database.mark_synced("env_metrics", [env_id])
    assert database.unsynced("env_metrics") == []


def test_invalid_table_is_rejected(database):
    import pytest

    with pytest.raises(ValueError):
        database.unsynced("not_a_table")


def test_query_helpers(database):
    database.insert_env("dev-1", 26.5, 55.0, 500)
    database.insert_env("dev-1", 27.0, 54.0, 520)
    database.insert_hvac("dev-1", 1, 850.0)
    database.insert_camera("dev-1", "RGB", "/data/images/test_rgb.jpg")
    database.insert_camera("dev-1", "IR", "/data/images/test_ir.jpg")

    counts = database.get_counts()
    assert counts["env_total"] == 2
    assert counts["env_unsynced"] == 2
    assert counts["hvac_total"] == 1
    assert counts["camera_rgb_total"] == 1
    assert counts["camera_ir_total"] == 1
    assert counts["total_records"] == 4
    assert counts["total_unsynced"] == 4

    latest_env = database.get_latest_env(limit=5)
    assert len(latest_env) == 2
    assert latest_env[0]["temperature"] == 27.0

    latest_hvac = database.get_latest_hvac(limit=5)
    assert len(latest_hvac) == 1
    assert latest_hvac[0]["power_w"] == 850.0

    latest_rgb = database.get_latest_cameras(limit=5, image_type="RGB")
    assert len(latest_rgb) == 1
    assert latest_rgb[0]["image_type"] == "RGB"

    latest_ir = database.get_latest_cameras(limit=5, image_type="IR")
    assert len(latest_ir) == 1
    assert latest_ir[0]["image_type"] == "IR"
