from __future__ import annotations

import logging
import signal
import threading
from datetime import datetime, timezone
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from apscheduler.schedulers.blocking import BlockingScheduler

from src.config import Settings, load_settings
from src.db.local_sqlite import LocalDatabase
from src.db.remote_sync import RemoteSync

LOGGER = logging.getLogger(__name__)


class Readable(Protocol):
    def read(self) -> dict[str, Any]: ...


class StatusReadable(Protocol):
    def read_status(self) -> dict[str, Any]: ...


class Camera(Protocol):
    def capture(self) -> Path: ...


def guarded(name: str, task: Callable[[], None]) -> Callable[[], None]:
    def run() -> None:
        try:
            task()
        except Exception:
            LOGGER.exception("%s task failed", name)
    return run


def build_sensor_task(
    database: LocalDatabase,
    device_id: str,
    climate_sensor: Readable,
    co2_sensor: Readable,
    hvac: StatusReadable,
) -> Callable[[], None]:
    def collect() -> None:
        temperature: float | None = None
        humidity: float | None = None
        co2_ppm: int | None = None
        try:
            climate = climate_sensor.read()
            temperature = float(climate["temperature"])
            humidity = float(climate["humidity"])
        except Exception:
            LOGGER.exception("climate sensor read failed")
        try:
            co2 = co2_sensor.read()
            co2_ppm = int(co2["co2_ppm"])
        except Exception:
            LOGGER.exception("CO2 sensor read failed")
        if temperature is not None or humidity is not None or co2_ppm is not None:
            database.insert_env(
                device_id=device_id,
                temperature=temperature,
                humidity=humidity,
                co2_ppm=co2_ppm,
            )
        try:
            status = hvac.read_status()
            database.insert_hvac(
                device_id=device_id,
                hvac_state=int(status["hvac_state"]),
                power_w=(
                    None
                    if status.get("power_w") is None
                    else float(status["power_w"])
                ),
            )
        except Exception:
            database.insert_hvac(device_id, hvac_state=-1, power_w=None)
            LOGGER.exception("HVAC status read failed")

    return collect


def build_scd41_task(
    database: LocalDatabase,
    device_id: str,
    sensor: Readable,
) -> Callable[[], None]:
    def collect() -> None:
        reading = sensor.read()
        if reading is None:
            return
        database.insert_env(
            device_id=device_id,
            temperature=float(reading["temperature"]),
            humidity=float(reading["humidity"]),
            co2_ppm=int(reading["co2_ppm"]),
        )

    return collect


def build_camera_task(
    database: LocalDatabase,
    device_id: str,
    rgb_camera: Camera,
    ir_camera: Camera,
) -> Callable[[], None]:
    def capture() -> None:
        for image_type, camera in (("RGB", rgb_camera), ("IR", ir_camera)):
            try:
                path = camera.capture()
                database.insert_camera(device_id, image_type, str(path))
                LOGGER.info("[%s Camera] Saved image to: %s", image_type, path)
            except Exception:
                LOGGER.exception("%s camera capture failed", image_type)

    return capture


def build_scd41_hvac_task(
    database: LocalDatabase,
    device_id: str,
    scd41: Readable,
    hvac: StatusReadable | None,
) -> Callable[[], None]:
    def collect() -> None:
        try:
            reading = scd41.read()
            if reading is not None:
                temp = float(reading["temperature"])
                hum = float(reading["humidity"])
                co2 = int(reading["co2_ppm"])
                database.insert_env(
                    device_id=device_id,
                    temperature=temp,
                    humidity=hum,
                    co2_ppm=co2,
                )
                LOGGER.info("[SCD41 Sensor] Reading: Temp=%.1f°C, Humidity=%.1f%%, CO2=%d ppm", temp, hum, co2)
        except Exception:
            LOGGER.exception("SCD41 read failed")

        if hvac is not None:
            try:
                status = hvac.read_status()
                database.insert_hvac(
                    device_id=device_id,
                    hvac_state=int(status["hvac_state"]),
                    power_w=(
                        None
                        if status.get("power_w") is None
                        else float(status["power_w"])
                    ),
                )
            except Exception:
                database.insert_hvac(device_id, hvac_state=-1, power_w=None)
                LOGGER.exception("HVAC status read failed")

    return collect


def build_scheduler(
    settings: Settings,
    sensor_task: Callable[[], None],
    camera_task: Callable[[], None],
    sync_task: Callable[[], None],
) -> BlockingScheduler:
    scheduler = BlockingScheduler(timezone="UTC")
    scheduler.add_job(
        guarded("sensor", sensor_task),
        "interval",
        seconds=settings.scd41_poll_seconds,
        id="sensor",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        guarded("camera", camera_task),
        "interval",
        seconds=settings.camera_interval_seconds,
        id="camera",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        guarded("sync", sync_task),
        "interval",
        seconds=30,
        id="sync",
        max_instances=1,
        coalesce=True,
    )
    return scheduler


def main() -> None:
    settings = load_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    database = LocalDatabase(settings.database_path)
    sync = RemoteSync(
        database,
        settings.server_url,
        settings.device_id,
        settings.api_key,
        settings.http_timeout_seconds,
    )

    # --- Hardware Initialization ---

    # 1. Initialize SCD41 Sensor
    scd41_sensor = None
    try:
        import adafruit_scd4x
        from adafruit_blinka.microcontroller.generic_linux.i2c import I2C as BlinkaI2C
        from src.sensors.scd41 import SCD41Sensor

        class SCD41I2CAdapter:
            def __init__(self, bus_num: int) -> None:
                self._bus = BlinkaI2C(bus_num)

            def try_lock(self) -> bool:
                return True

            def unlock(self) -> None:
                pass

            def readfrom_into(self, address: int, buffer: bytearray, *, start: int = 0, end: int | None = None) -> None:
                self._bus.readfrom_into(address, buffer, start=start, end=end)

            def writeto(self, address: int, buffer: bytes | bytearray, *, start: int = 0, end: int | None = None, stop: bool = True) -> None:
                self._bus.writeto(address, buffer, start=start, end=end, stop=stop)

        i2c_bus = SCD41I2CAdapter(bus_num=settings.scd41_i2c_bus)
        driver = adafruit_scd4x.SCD4X(i2c_bus)
        scd41_sensor = SCD41Sensor(driver)
        LOGGER.info("Successfully initialized SCD41 hardware sensor on I2C bus %d", settings.scd41_i2c_bus)
    except Exception as exc:
        LOGGER.warning("Could not initialize real SCD41 hardware: %s. Using dummy sensor.", exc)

    # 2. Initialize RGB Camera
    rgb_camera = None
    try:
        from src.sensors.camera_rgb import PiCamera
        rgb_camera = PiCamera(settings.image_dir, camera_index=settings.rgb_camera_index)
        LOGGER.info("Successfully initialized RGB PiCamera on index %d", settings.rgb_camera_index)
    except Exception as exc:
        LOGGER.warning("Could not initialize real RGB camera: %s. Using dummy camera.", exc)

    # 3. Initialize IR Camera (FLIR Lepton)
    ir_camera = None
    try:
        from src.sensors.camera_ir import probe_lepton, PiIRCamera
        model = probe_lepton(
            bus_number=settings.lepton_i2c_bus,
            address=settings.lepton_i2c_address,
            fallback_width=settings.lepton_width or 160,
            fallback_height=settings.lepton_height or 120,
        )
        LOGGER.info("Detected FLIR Lepton: %s (%dx%d)", model.family, model.width, model.height)
        ir_camera = PiIRCamera(
            settings.image_dir,
            spi_bus=settings.lepton_spi_bus,
            spi_device=settings.lepton_spi_device,
            width=model.width,
            height=model.height,
        )
        LOGGER.info("Successfully initialized FLIR Lepton IR Camera on SPI bus %d, device %d",
                    settings.lepton_spi_bus, settings.lepton_spi_device)
    except Exception as exc:
        LOGGER.warning("Could not initialize real FLIR Lepton IR camera: %s. Using dummy camera.", exc)

    # Fallbacks for unconfigured/missing camera hardware
    class DummyCamera:
        def __init__(self, name: str) -> None:
            self.name = name
        def capture(self) -> Path:
            raise RuntimeError(f"{self.name} camera is not available/configured")

    if rgb_camera is None:
        rgb_camera = DummyCamera("RGB")
    if ir_camera is None:
        ir_camera = DummyCamera("IR")

    # 4. Initialize HVAC Modbus Control
    hvac_control = None
    if (
        settings.modbus_slave_id is not None
        and settings.modbus_state_register is not None
        and settings.modbus_control_register is not None
    ):
        try:
            from pymodbus.client import ModbusSerialClient
            from src.control.hvac_modbus import HVACModbus

            modbus_client = ModbusSerialClient(
                port=settings.modbus_port,
                baudrate=settings.modbus_baudrate,
            )
            hvac_control = HVACModbus(
                client=modbus_client,
                slave_id=settings.modbus_slave_id,
                state_register=settings.modbus_state_register,
                control_register=settings.modbus_control_register,
                power_register=settings.modbus_power_register,
            )
            LOGGER.info("Successfully initialized HVAC Modbus client on port %s", settings.modbus_port)
        except Exception as exc:
            LOGGER.warning("Could not initialize Modbus HVAC control: %s", exc)
    else:
        LOGGER.info("Modbus HVAC settings are not fully configured. HVAC tracking is disabled.")

    class DummySCD41:
        def read(self) -> dict[str, float | int] | None:
            return None

    if scd41_sensor is None:
        scd41_sensor = DummySCD41()

    # Build scheduled tasks
    sensor_task = build_scd41_hvac_task(
        database=database,
        device_id=settings.device_id,
        scd41=scd41_sensor,
        hvac=hvac_control,
    )

    camera_task = build_camera_task(
        database=database,
        device_id=settings.device_id,
        rgb_camera=rgb_camera,
        ir_camera=ir_camera,
    )

    def sync_task() -> None:
        try:
            count = sync.sync_all()
            LOGGER.info("RemoteSync executed: %d items synced to %s", count, settings.server_url)
        except Exception as exc:
            LOGGER.warning("RemoteSync encountered sync error: %s (queued for next interval)", exc)

    scheduler = build_scheduler(
        settings,
        sensor_task=sensor_task,
        camera_task=camera_task,
        sync_task=sync_task,
    )
    stop_once = threading.Event()

    def shutdown(*_: object) -> None:
        if not stop_once.is_set():
            stop_once.set()
            scheduler.shutdown(wait=False)

    def trigger_instant_snap(*_: object) -> None:
        LOGGER.info("Received SIGUSR1: Instantly capturing RGB/IR photos & syncing to Server...")
        def _run() -> None:
            guarded("instant_camera", camera_task)()
            guarded("instant_sync", sync_task)()
        threading.Thread(target=_run, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    if hasattr(signal, "SIGUSR1"):
        try:
            signal.signal(signal.SIGUSR1, trigger_instant_snap)
        except (ValueError, OSError):
            pass

    # Execute initial sync & camera capture immediately on startup
    guarded("initial_camera", camera_task)()
    guarded("initial_sync", sync_task)()

    scheduler.start()


if __name__ == "__main__":
    main()
