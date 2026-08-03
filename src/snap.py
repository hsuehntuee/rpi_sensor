from __future__ import annotations

import logging
import sys
from pathlib import Path

from src.config import load_settings
from src.db.local_sqlite import LocalDatabase
from src.db.remote_sync import RemoteSync

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
LOGGER = logging.getLogger("snap_and_sync")


def main() -> None:
    settings = load_settings()
    database = LocalDatabase(settings.database_path)

    print("==================================================")
    print("📸 Instantly Capturing RGB & IR Photos and Syncing...")
    print("==================================================")

    # 1. Initialize RGB Camera
    rgb_camera = None
    try:
        from src.sensors.camera_rgb import PiCamera
        rgb_camera = PiCamera(
            image_dir=settings.image_dir,
            camera_index=settings.rgb_camera_index,
        )
        LOGGER.info("Initialized RGB camera")
    except Exception as exc:
        LOGGER.warning("Could not initialize RGB camera: %s", exc)

    # 2. Initialize IR Camera
    ir_camera = None
    try:
        from src.sensors.camera_ir import PiIRCamera, probe_lepton
        model = probe_lepton(
            bus=settings.lepton_cci_bus,
            address=settings.lepton_cci_address,
        )
        ir_camera = PiIRCamera(
            image_dir=settings.image_dir,
            spi_bus=settings.lepton_spi_bus,
            spi_device=settings.lepton_spi_device,
            width=model.width,
            height=model.height,
            colormap=settings.ir_colormap,
            upscale_factor=settings.ir_upscale_factor,
        )
        LOGGER.info("Initialized FLIR Lepton IR camera")
    except Exception as exc:
        LOGGER.warning("Could not initialize IR camera: %s", exc)

    # 3. Capture RGB
    if rgb_camera is not None:
        try:
            rgb_path = rgb_camera.capture()
            database.insert_camera(settings.device_id, "RGB", str(rgb_path))
            print(f"✅ [RGB Photo Captured] -> {rgb_path}")
        except Exception as exc:
            print(f"❌ [RGB Photo Failed] -> {exc}")
    else:
        print("⚠️ [RGB Photo Skipped] -> Camera not available")

    # 4. Capture IR
    if ir_camera is not None:
        try:
            ir_path = ir_camera.capture()
            database.insert_camera(settings.device_id, "IR", str(ir_path))
            print(f"✅ [IR Photo Captured] -> {ir_path}")
        except Exception as exc:
            print(f"❌ [IR Photo Failed] -> {exc}")
    else:
        print("⚠️ [IR Photo Skipped] -> Camera not available")

    # 5. Instantly Sync to Server
    sync = RemoteSync(
        database=database,
        server_url=settings.server_url,
        device_id=settings.device_id,
        api_key=settings.api_key,
    )
    print("--------------------------------------------------")
    print(f"📡 Syncing images to Server ({settings.server_url})...")
    synced_count = sync.sync_all()
    print(f"🎉 [Sync Finished] Successfully uploaded {synced_count} items to Server!")
    print("==================================================")


if __name__ == "__main__":
    main()
