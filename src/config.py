from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


def _optional_int(name: str) -> int | None:
    value = os.getenv(name, "").strip()
    return int(value) if value else None


def _int(name: str, default: str) -> int:
    return int(os.getenv(name, default), 0)


def _bool(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "on", "t"}


@dataclass(frozen=True)
class Settings:
    device_id: str
    server_url: str
    api_key: str
    database_path: Path
    image_dir: Path
    log_level: str = "INFO"
    http_timeout_seconds: float = 10.0
    scd41_i2c_bus: int = 2
    scd41_poll_seconds: int = 300
    camera_interval_seconds: int = 300
    sample_interval_minutes: int = 5
    sample_cron_minute: str = "*/5"
    rgb_camera_index: int = 0
    rgb_width: int = 1920
    rgb_height: int = 1080
    rgb_quality: int = 85
    image_retention_days: int = 14
    min_disk_free_mb: int = 1500
    lepton_spi_bus: int = 0
    lepton_spi_device: int = 0
    lepton_i2c_bus: int = 1
    lepton_i2c_address: int = 0x2A
    lepton_width: int | None = 160
    lepton_height: int | None = 120
    lepton_colormap: str = "rainbow"
    lepton_reset_gpio: int | None = None
    modbus_port: str = "/dev/ttyUSB0"
    modbus_baudrate: int = 9600
    modbus_slave_id: int | None = None
    modbus_state_register: int | None = None
    modbus_control_register: int | None = None
    modbus_power_register: int | None = None
    edge_web_enabled: bool = True
    edge_web_host: str = "0.0.0.0"
    edge_web_port: int = 8080


def load_settings(env_file: str | None = None) -> Settings:
    load_dotenv(env_file)
    device_id = os.getenv("DEVICE_ID", "rpi_edge_01").strip()
    if not device_id:
        device_id = "rpi_edge_01"
    api_key = os.getenv("API_KEY", "default_secret_api_key_1234567890").strip()
    if len(api_key) < 16:
        api_key = "default_secret_api_key_1234567890"
    server_url = os.getenv("SERVER_URL", "http://localhost:8000").strip().rstrip("/")
    if not server_url:
        server_url = "http://localhost:8000"
    raw_db_path = os.getenv("DATABASE_PATH", "/data/sensor.db").strip()
    if raw_db_path and not raw_db_path.startswith("/"):
        raw_db_path = "/" + raw_db_path

    raw_img_dir = os.getenv("IMAGE_DIR", "/data/images").strip()
    if raw_img_dir and not raw_img_dir.startswith("/"):
        raw_img_dir = "/" + raw_img_dir

    sample_interval_minutes = _int("SAMPLE_INTERVAL_MINUTES", "5")
    if sample_interval_minutes < 1:
        sample_interval_minutes = 5

    sample_cron_minute = os.getenv("SAMPLE_CRON_MINUTE", f"*/{sample_interval_minutes}").strip()
    if not sample_cron_minute:
        sample_cron_minute = f"*/{sample_interval_minutes}"

    settings = Settings(
        device_id=device_id,
        server_url=server_url,
        api_key=api_key,
        database_path=Path(raw_db_path),
        image_dir=Path(raw_img_dir),
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        http_timeout_seconds=float(os.getenv("HTTP_TIMEOUT_SECONDS", "10")),
        scd41_i2c_bus=_int("SCD41_I2C_BUS", "2"),
        scd41_poll_seconds=_int("SCD41_POLL_SECONDS", "300"),
        camera_interval_seconds=_int("CAMERA_INTERVAL_SECONDS", "300"),
        sample_interval_minutes=sample_interval_minutes,
        sample_cron_minute=sample_cron_minute,
        rgb_camera_index=_int("RGB_CAMERA_INDEX", "0"),
        rgb_width=_int("RGB_WIDTH", "1920"),
        rgb_height=_int("RGB_HEIGHT", "1080"),
        rgb_quality=_int("RGB_QUALITY", "85"),
        image_retention_days=_int("IMAGE_RETENTION_DAYS", "14"),
        min_disk_free_mb=_int("MIN_DISK_FREE_MB", "1500"),
        lepton_spi_bus=_int("LEPTON_SPI_BUS", "0"),
        lepton_spi_device=_int("LEPTON_SPI_DEVICE", "0"),
        lepton_i2c_bus=_int("LEPTON_I2C_BUS", "1"),
        lepton_i2c_address=_int("LEPTON_I2C_ADDRESS", "0x2A"),
        lepton_width=_optional_int("LEPTON_WIDTH") or 160,
        lepton_height=_optional_int("LEPTON_HEIGHT") or 120,
        lepton_colormap=os.getenv("LEPTON_COLORMAP", "rainbow").lower(),
        lepton_reset_gpio=_optional_int("LEPTON_RESET_GPIO"),
        modbus_port=os.getenv("MODBUS_PORT", "/dev/ttyUSB0"),
        modbus_baudrate=int(os.getenv("MODBUS_BAUDRATE", "9600")),
        modbus_slave_id=_optional_int("MODBUS_SLAVE_ID"),
        modbus_state_register=_optional_int("MODBUS_STATE_REGISTER"),
        modbus_control_register=_optional_int("MODBUS_CONTROL_REGISTER"),
        modbus_power_register=_optional_int("MODBUS_POWER_REGISTER"),
        edge_web_enabled=_bool("EDGE_WEB_ENABLED", True),
        edge_web_host=os.getenv("EDGE_WEB_HOST", "0.0.0.0").strip(),
        edge_web_port=_int("EDGE_WEB_PORT", "8080"),
    )
    if settings.scd41_poll_seconds < 1:
        raise ValueError("SCD41_POLL_SECONDS must be at least 1")
    if settings.camera_interval_seconds < 1:
        raise ValueError("CAMERA_INTERVAL_SECONDS must be at least 1")
    if (settings.lepton_width is None) != (settings.lepton_height is None):
        raise ValueError("LEPTON_WIDTH and LEPTON_HEIGHT must be set together")
    return settings
