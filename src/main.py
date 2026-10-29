from __future__ import annotations

import logging
import signal
import threading
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


class Camera(Protocol):
    def capture(self) -> Path: ...


class FailureLatch:
    """硬體連續失敗 threshold 次才寫一次 log（偶爾漏一張是常態，不算故障）；
    之後持續故障不再重複寫，恢復後再壞才會再寫（避免每 5 分鐘洗版佔 SD 卡）"""

    def __init__(self, threshold: int = 3) -> None:
        self.threshold = threshold
        self._counts: dict[str, int] = {}

    def failed(self, key: str, message: str) -> None:
        self._counts[key] = self._counts.get(key, 0) + 1
        if self._counts[key] == self.threshold:
            LOGGER.exception("%s (%d times in a row; not logged again until it recovers)", message, self.threshold)

    def ok(self, key: str) -> None:
        if self._counts.pop(key, 0) >= self.threshold:
            LOGGER.warning("%s recovered", key)


def guarded(name: str, task: Callable[[], None]) -> Callable[[], None]:
    def run() -> None:
        try:
            task()
        except Exception:
            LOGGER.exception("%s task failed", name)
    return run


_CAMERA_TASK_LOCK = threading.Lock()


def build_camera_task(
    database: LocalDatabase,
    device_id: str,
    rgb_camera: Camera,
    ir_camera: Camera,
    min_disk_free_mb: int = 1500,
    image_dir: Path | None = None,
) -> Callable[[], None]:
    latch = FailureLatch()

    def capture() -> None:
        if not _CAMERA_TASK_LOCK.acquire(blocking=False):
            LOGGER.warning("Camera capture already in progress. Skipping duplicate run.")
            return
        try:
            # Enforce safety watermark before new captures to prevent SD card exhaustion
            database.enforce_disk_limit(min_free_mb=min_disk_free_mb, image_dir=image_dir)
            for image_type, camera in (("RGB", rgb_camera), ("IR", ir_camera)):
                try:
                    path = camera.capture()
                    database.insert_camera(device_id, image_type, str(path))
                    LOGGER.info("[%s Camera] Saved image to: %s", image_type, path)
                    latch.ok(f"{image_type} camera")
                except Exception:
                    latch.failed(f"{image_type} camera", f"{image_type} camera capture failed")
        finally:
            _CAMERA_TASK_LOCK.release()

    return capture


def build_scd41_logging_task(
    database: LocalDatabase,
    device_id: str,
    scd41: Readable,
) -> Callable[[], None]:
    latch = FailureLatch()

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
                latch.ok("SCD41")
        except Exception:
            latch.failed("SCD41", "SCD41 read failed")

    return collect


def build_scheduler(
    settings: Settings,
    sensor_task: Callable[[], None],
    camera_task: Callable[[], None],
    sync_task: Callable[[], None],
    retention_task: Callable[[], None] | None = None,
) -> BlockingScheduler:
    scheduler = BlockingScheduler(timezone="Asia/Taipei")
    # 準時整點排程 (Cron Trigger)：以標準時間的每 5 分鐘整點（如 10:00, 10:05, 10:10...）取樣一次
    scheduler.add_job(
        guarded("sensor", sensor_task),
        "cron",
        minute=settings.sample_cron_minute,
        second=0,
        id="sensor",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        guarded("camera", camera_task),
        "cron",
        minute=settings.sample_cron_minute,
        second=0,
        id="camera",
        max_instances=1,
        coalesce=True,
    )
    # 同步任務：每 30 秒自動將本地 SQLite 數據與照片同步上傳至 Server
    scheduler.add_job(
        guarded("sync", sync_task),
        "interval",
        seconds=30,
        id="sync",
        max_instances=1,
        coalesce=True,
    )
    # 本地磁碟維護任務：每日凌晨 03:00 自動清理已同步超過保留天數的舊檔案與歷史紀錄
    if retention_task is not None:
        scheduler.add_job(
            guarded("retention", retention_task),
            "cron",
            hour=3,
            minute=0,
            id="retention",
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
    # Suppress APScheduler per-job execution chatter (Running/Executed lines)
    logging.getLogger("apscheduler.executors.default").setLevel(logging.WARNING)
    logging.getLogger("apscheduler.scheduler").setLevel(logging.WARNING)
    database = LocalDatabase(settings.database_path)
    # SERVER_URL=off：完全不連線上傳，資料留在本機；之後填入 Server 網址會自動補傳全部積壓資料
    sync = None
    if settings.server_url != "off":
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
        rgb_camera = PiCamera(
            settings.image_dir,
            camera_index=settings.rgb_camera_index,
            width=settings.rgb_width,
            height=settings.rgb_height,
            quality=settings.rgb_quality,
        )
        LOGGER.info("Successfully initialized RGB PiCamera on index %d (%dx%d, q=%d)",
                    settings.rgb_camera_index, settings.rgb_width, settings.rgb_height, settings.rgb_quality)
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
            colormap=settings.lepton_colormap,
            reset_gpio=settings.lepton_reset_gpio,
            i2c_bus=settings.lepton_i2c_bus,
            i2c_address=settings.lepton_i2c_address,
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

    # 4. IR air-conditioner remote (ESP32 on USB serial). The port is opened lazily
    # on first use, so a missing ESP32 never blocks sensing/camera/sync.
    ac_remote = None
    if settings.ac_serial_port.lower() not in {"", "off", "none", "false"}:
        try:
            from src.control.ir_ac import IrAcRemote
            ac_remote = IrAcRemote(settings.ac_serial_port, settings.ac_protocol, settings.ac_model)
            LOGGER.info("IR AC remote enabled (port=%s, protocol=%s)",
                        settings.ac_serial_port, settings.ac_protocol or "firmware default")
        except Exception as exc:
            LOGGER.warning("Could not set up IR AC remote: %s", exc)

    class DummySCD41:
        def read(self) -> dict[str, float | int] | None:
            return None

    if scd41_sensor is None:
        scd41_sensor = DummySCD41()

    # Build scheduled tasks
    sensor_task = build_scd41_logging_task(
        database=database,
        device_id=settings.device_id,
        scd41=scd41_sensor,
    )

    camera_task = build_camera_task(
        database=database,
        device_id=settings.device_id,
        rgb_camera=rgb_camera,
        ir_camera=ir_camera,
        min_disk_free_mb=settings.min_disk_free_mb,
        image_dir=settings.image_dir,
    )

    _sync_fail_count = 0

    def sync_task() -> None:
        nonlocal _sync_fail_count
        if sync is None:
            return
        try:
            count = sync.sync_all(metrics_batch=100, image_batch=10)
            if count > 0:
                if _sync_fail_count > 0:
                    LOGGER.info("RemoteSync: connection restored after %d failed attempts! Resuming gradual sync.", _sync_fail_count)
                _sync_fail_count = 0
                LOGGER.info("RemoteSync: %d items synced to %s", count, settings.server_url)
            elif _sync_fail_count == 0:
                pass  # server reachable but nothing to sync — silent
        except Exception as exc:
            _sync_fail_count += 1
            if _sync_fail_count == 1:  # 資料暫存本機，不算緊急；快滿時由磁碟示警處理
                LOGGER.info("RemoteSync: server unreachable (%s), entering offline buffering mode (will retry silently)", exc)
            elif _sync_fail_count % 1440 == 0:  # once every 12 hours (1440 * 30s)
                LOGGER.info(
                    "RemoteSync: still offline after %d attempts (%.1f days offline). Data safely buffered locally.",
                    _sync_fail_count, (_sync_fail_count * 30) / 86400,
                )

    def maintenance_task() -> None:
        res = database.purge_old_synced_data(settings.image_retention_days)
        freed = database.enforce_disk_limit(settings.min_disk_free_mb, image_dir=settings.image_dir)
        deleted_imgs = res.get("deleted_images", 0) + freed
        if deleted_imgs > 0:
            LOGGER.info("Daily Maintenance: pruned %d old/excess photos to maintain disk safety", deleted_imgs)

    scheduler = build_scheduler(
        settings,
        sensor_task=sensor_task,
        camera_task=camera_task,
        sync_task=sync_task,
        retention_task=maintenance_task,
    )
    stop_once = threading.Event()
    web_server = None

    if settings.edge_web_enabled:
        try:
            import uvicorn
            from src.web.app import create_edge_app

            def _manual_capture_callback() -> None:
                LOGGER.info("[Web Action] Triggered manual camera capture")
                guarded("manual_camera", camera_task)()

            def _manual_sync_callback() -> int:
                if sync is None:
                    raise RuntimeError("尚未設定 Server（SERVER_URL=off），資料保留在本機")
                LOGGER.info("[Web Action] Triggered manual server sync")
                return sync.sync_all()

            hardware_info = {
                "scd41": "Hardware Active" if not isinstance(scd41_sensor, DummySCD41) else "Standalone / Emulated",
                "camera_rgb": "PiCamera Active" if not isinstance(rgb_camera, DummyCamera) else "Standalone / Emulated",
                "camera_ir": "FLIR Lepton Active" if not isinstance(ir_camera, DummyCamera) else "Standalone / Emulated",
                "ac_remote": f"ESP32 IR ({settings.ac_serial_port})" if ac_remote is not None else "Disabled",
            }

            web_app = create_edge_app(
                settings=settings,
                database=database,
                capture_callback=_manual_capture_callback,
                sync_callback=_manual_sync_callback,
                hardware_status=hardware_info,
                ac_remote=ac_remote,
            )

            config = uvicorn.Config(
                app=web_app,
                host=settings.edge_web_host,
                port=settings.edge_web_port,
                log_level="warning",
                access_log=False,
            )
            web_server = uvicorn.Server(config)

            def _run_web() -> None:
                LOGGER.info("🚀 Starting Edge Local Dashboard on http://%s:%d/dashboard", settings.edge_web_host, settings.edge_web_port)
                web_server.run()

            web_thread = threading.Thread(target=_run_web, daemon=True, name="edge_web_server")
            web_thread.start()
        except Exception as exc:
            LOGGER.warning("Could not start Edge Web Dashboard: %s", exc)

    def shutdown(*_: object) -> None:
        if not stop_once.is_set():
            stop_once.set()
            if web_server is not None:
                web_server.should_exit = True
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

    # Execute initial test capture & sync immediately on startup
    guarded("initial_sensor", sensor_task)()
    guarded("initial_camera", camera_task)()
    guarded("initial_sync", sync_task)()

    scheduler.start()


if __name__ == "__main__":
    main()
