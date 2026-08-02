from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import subprocess
from typing import Any, Callable


import shutil


class RGBCamera:
    def __init__(
        self,
        image_dir: Path,
        capture: Callable[[Path], None],
        image_type: str = "rgb",
    ) -> None:
        self.image_dir = image_dir
        self.capture_impl = capture
        self.image_type = image_type

    def capture(self) -> Path:
        self.image_dir.mkdir(parents=True, exist_ok=True)
        path = self.image_dir / (
            f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}_{self.image_type}.jpg"
        )
        self.capture_impl(path)
        return path


class PiCamera(RGBCamera):
    """Raspberry Pi Camera adapter using the supported rpicam-still or libcamera-still command."""

    def __init__(
        self,
        image_dir: Path,
        camera_index: int = 0,
        runner: Callable[..., Any] = subprocess.run,
    ) -> None:
        self.camera_index = camera_index
        self.runner = runner
        super().__init__(image_dir, self._capture)

    def _capture(self, path: Path) -> None:
        cmd = shutil.which("rpicam-still") or shutil.which("libcamera-still") or "rpicam-still"
        self.runner(
            [
                cmd,
                "--camera",
                str(self.camera_index),
                "--nopreview",
                "--immediate",
                "--output",
                str(path),
            ],
            check=True,
            timeout=30,
        )
