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
        lepton_spi_bus=0,
        lepton_spi_device=0,
        lepton_i2c_bus=1,
        lepton_i2c_address=0x2A,
        lepton_width=160,
        lepton_height=120,
        lepton_colormap="rainbow",
        modbus_port="/dev/ttyUSB0",
        modbus_baudrate=9600,
        modbus_slave_id=1,
        modbus_state_register=100,
        modbus_control_register=101,
        modbus_power_register=102,
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
        assert "樹莓派 5 邊緣節點即時儀表板" in res.text
        assert "SCD41" in res.text


def test_edge_overview_api(mock_settings, database):
    database.insert_env("test_edge_node", 24.8, 58.2, 620)
    database.insert_hvac("test_edge_node", 1, 1500.0)
    database.insert_camera("test_edge_node", "RGB", str(mock_settings.image_dir / "rgb.jpg"))
    database.insert_camera("test_edge_node", "IR", str(mock_settings.image_dir / "ir.jpg"))

    app = create_edge_app(mock_settings, database)
    client = TestClient(app)
    res = client.get("/api/v1/overview")
    assert res.status_code == 200
    data = res.json()

    assert data["device_id"] == "test_edge_node"
    assert data["counts"]["total_records"] == 4
    assert len(data["latest_env"]) == 1
    assert data["latest_env"][0]["temperature"] == 24.8
    assert len(data["latest_hvac"]) == 1
    assert data["latest_hvac"][0]["power_w"] == 1500.0
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
