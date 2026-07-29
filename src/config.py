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


@dataclass(frozen=True)
class Settings:
    device_id: str
    server_url: str
    api_key: str
    database_path: Path
    image_dir: Path
    log_level: str
    http_timeout_seconds: float
    scd41_i2c_bus: int
    scd41_poll_seconds: int
    camera_interval_seconds: int
    rgb_camera_index: int
    lepton_spi_bus: int
    lepton_spi_device: int
    lepton_i2c_bus: int
    lepton_i2c_address: int
    lepton_width: int | None
    lepton_height: int | None
    modbus_port: str
    modbus_baudrate: int
    modbus_slave_id: int | None
    modbus_state_register: int | None
    modbus_control_register: int | None
    modbus_power_register: int | None


def load_settings(env_file: str | None = None) -> Settings:
    load_dotenv(env_file)
    device_id = os.getenv("DEVICE_ID", "").strip()
    if not device_id:
        raise ValueError("DEVICE_ID is required")
    settings = Settings(
        device_id=device_id,
        server_url=os.getenv("SERVER_URL", "").rstrip("/"),
        api_key=os.getenv("API_KEY", ""),
        database_path=Path(os.getenv("DATABASE_PATH", "data/sensor.db")),
        image_dir=Path(os.getenv("IMAGE_DIR", "data/images")),
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        http_timeout_seconds=float(os.getenv("HTTP_TIMEOUT_SECONDS", "10")),
        scd41_i2c_bus=_int("SCD41_I2C_BUS", "1"),
        scd41_poll_seconds=_int("SCD41_POLL_SECONDS", "1"),
        camera_interval_seconds=_int("CAMERA_INTERVAL_SECONDS", "10"),
        rgb_camera_index=_int("RGB_CAMERA_INDEX", "0"),
        lepton_spi_bus=_int("LEPTON_SPI_BUS", "0"),
        lepton_spi_device=_int("LEPTON_SPI_DEVICE", "0"),
        lepton_i2c_bus=_int("LEPTON_I2C_BUS", "1"),
        lepton_i2c_address=_int("LEPTON_I2C_ADDRESS", "0x2A"),
        lepton_width=_optional_int("LEPTON_WIDTH"),
        lepton_height=_optional_int("LEPTON_HEIGHT"),
        modbus_port=os.getenv("MODBUS_PORT", "/dev/ttyUSB0"),
        modbus_baudrate=int(os.getenv("MODBUS_BAUDRATE", "9600")),
        modbus_slave_id=_optional_int("MODBUS_SLAVE_ID"),
        modbus_state_register=_optional_int("MODBUS_STATE_REGISTER"),
        modbus_control_register=_optional_int("MODBUS_CONTROL_REGISTER"),
        modbus_power_register=_optional_int("MODBUS_POWER_REGISTER"),
    )
    if settings.scd41_poll_seconds < 1:
        raise ValueError("SCD41_POLL_SECONDS must be at least 1")
    if settings.camera_interval_seconds < 1:
        raise ValueError("CAMERA_INTERVAL_SECONDS must be at least 1")
    if (settings.lepton_width is None) != (settings.lepton_height is None):
        raise ValueError("LEPTON_WIDTH and LEPTON_HEIGHT must be set together")
    if len(settings.api_key) < 16:
        raise ValueError("API_KEY must be at least 16 characters")
    return settings
