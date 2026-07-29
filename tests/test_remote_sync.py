from unittest.mock import Mock

import pytest

from src.db.remote_sync import RemoteSync


def test_success_marks_only_posted_rows(database):
    row_id = database.insert_env("device-1", 25.4, 60.1, 450)
    session = Mock()
    session.post.return_value.raise_for_status.return_value = None

    count = RemoteSync(
        database, "https://example.test", "device-1", "test-api-key-1234",
        session=session,
    ).sync_metrics()

    assert count == 1
    assert database.unsynced("env_metrics") == []
    payload = session.post.call_args.kwargs["json"]
    assert payload["env_data"][0]["co2_ppm"] == 450
    assert payload["env_data"][0]["timestamp"].endswith("Z")
    assert row_id > 0


def test_http_failure_keeps_rows_unsynced(database):
    database.insert_env("device-1", 25.4, 60.1, 450)
    session = Mock()
    session.post.return_value.raise_for_status.side_effect = RuntimeError("offline")

    with pytest.raises(RuntimeError):
        RemoteSync(
            database, "https://example.test", "device-1", "test-api-key-1234",
            session=session,
        ).sync_metrics()

    assert len(database.unsynced("env_metrics")) == 1


def test_image_success_marks_row_synced(database, tmp_path):
    image_path = tmp_path / "thermal.png"
    image_path.write_bytes(b"image")
    database.insert_camera("device-1", "IR", str(image_path))
    session = Mock()
    session.post.return_value.raise_for_status.return_value = None

    count = RemoteSync(
        database, "https://example.test", "device-1", "test-api-key-1234",
        session=session,
    ).sync_images()

    assert count == 1
    assert database.unsynced("camera_logs") == []
    assert session.post.call_args.kwargs["headers"]["X-API-Key"] == "test-api-key-1234"
