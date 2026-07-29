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
