from unittest.mock import Mock
from pathlib import Path
import tempfile
import pytest
from fastapi.testclient import TestClient

from src.config import Settings
from src.db.local_sqlite import LocalDatabase
from src.web.app import create_edge_app


@pytest.fixture
def mock_settings(tmp_path: Path) -> Settings:
    img_dir = tmp_path / "images"
    img_dir.mkdir(parents=True, exist_ok=True)
    return Settings(
        device_id="test_edge_node",
        server_url="http://test-server:8000",
        api_key="secret-api-key-1234567890",
        database_path=tmp_path / "sensor.db",
        image_dir=img_dir,
        log_level="INFO",
        http_timeout_seconds=10.0,
        scd41_i2c_bus=2,
        scd41_poll_seconds=300,
        camera_interval_seconds=300,
        sample_interval_minutes=5,
        sample_cron_minute="*/5",
        rgb_camera_index=0,
        rgb_width=1920,
        rgb_height=1080,
        rgb_quality=85,
        image_retention_days=14,
        min_disk_free_mb=1500,
        lepton_spi_bus=0,
        lepton_spi_device=0,
        lepton_i2c_bus=1,
        lepton_i2c_address=0x2A,
        lepton_width=160,
        lepton_height=120,
        lepton_colormap="rainbow",
        lepton_reset_gpio=None,
        ac_protocol="HITACHI_AC344",
        ac_name="304D",
        edge_web_enabled=True,
        edge_web_host="0.0.0.0",
        edge_web_port=8080,
    )


def test_edge_dashboard_health(mock_settings, database):
    app = create_edge_app(mock_settings, database)
    client = TestClient(app)
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["device_id"] == "test_edge_node"


def test_edge_dashboard_html_pages(mock_settings, database):
    app = create_edge_app(mock_settings, database)
    client = TestClient(app)
    for path in ["/", "/dashboard", "/view"]:
        res = client.get(path)
        assert res.status_code == 200
        assert "邊緣節點監控" in res.text
        assert "SCD41" in res.text


def test_edge_overview_api(mock_settings, database):
    database.insert_env("test_edge_node", 24.8, 58.2, 620)
    database.insert_camera("test_edge_node", "RGB", str(mock_settings.image_dir / "rgb.jpg"))
    database.insert_camera("test_edge_node", "IR", str(mock_settings.image_dir / "ir.jpg"))

    app = create_edge_app(mock_settings, database)
    client = TestClient(app)
    res = client.get("/api/v1/overview")
    assert res.status_code == 200
    data = res.json()

    assert data["device_id"] == "test_edge_node"
    assert data["counts"]["total_records"] == 3
    assert len(data["latest_env"]) == 1
    assert data["latest_env"][0]["temperature"] == 24.8
    assert len(data["latest_rgb_images"]) == 1
    assert len(data["latest_ir_images"]) == 1


def test_edge_images_list(mock_settings, database):
    # Create sample image files
    test_img1 = mock_settings.image_dir / "20260813_120000_RGB_sample.jpg"
    test_img1.write_bytes(b"dummy_image_data_rgb")
    test_img2 = mock_settings.image_dir / "20260813_120000_IR_sample.jpg"
    test_img2.write_bytes(b"dummy_image_data_ir")

    app = create_edge_app(mock_settings, database)
    client = TestClient(app)
    res = client.get("/api/v1/images/list")
    assert res.status_code == 200
    images = res.json()
    assert len(images) == 2
    types = {img["type"] for img in images}
    assert "RGB" in types
    assert "IR" in types


def test_edge_actions(mock_settings, database):
    mock_capture = Mock()
    mock_sync = Mock(return_value=3)

    app = create_edge_app(
        mock_settings,
        database,
        capture_callback=mock_capture,
        sync_callback=mock_sync,
    )
    client = TestClient(app)

    # Test capture action
    res_cap = client.post("/api/v1/actions/capture")
    assert res_cap.status_code == 200
    assert res_cap.json()["status"] == "success"
    assert res_cap.json()["synced_count"] == 3
    mock_capture.assert_called_once()
    mock_sync.assert_called_once()

    # Test sync action
    res_sync = client.post("/api/v1/actions/sync")
    assert res_sync.status_code == 200
    assert res_sync.json()["status"] == "success"
    assert res_sync.json()["synced_count"] == 3


def test_ac_page_and_status_without_remote(mock_settings, database):
    client = TestClient(create_edge_app(mock_settings, database))
    assert "冷氣" in client.get("/ac").text
    data = client.get("/api/v1/ac").json()
    assert data["connected"] is False
    assert data["name"] == "304D"
    assert client.post("/api/v1/ac", json={"power": True}).status_code == 503


def test_ac_set_and_status(mock_settings, database):
    database.insert_env("test_edge_node", 27.5, 60.0, 600)
    remote = Mock()
    remote.status.return_value = {
        "connected": True, "port": "/dev/ttyUSB0", "protocol": "HITACHI_AC344",
        "state": {"power": True, "mode": "cool", "temp": 25, "fan": "auto", "swing": False},
    }
    client = TestClient(create_edge_app(mock_settings, database, ac_remote=remote))

    res = client.post("/api/v1/ac", json={"power": True, "mode": "cool", "temp": 25})
    assert res.status_code == 200
    remote.set_state.assert_called_once_with(power=True, mode="cool", temp=25.0, fan="auto", swing=False)
    assert res.json()["state"]["temp"] == 25
    assert res.json()["room"]["temperature"] == 27.5

    remote.set_state.side_effect = RuntimeError("ESP32 沒有回應")
    assert client.post("/api/v1/ac", json={"power": False}).status_code == 502
