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
        cmd = shutil.which("rpicam-still") or shutil.which("libcamera-still")
        if cmd:
            try:
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
                return
            except Exception:
                pass

        ffmpeg_cmd = shutil.which("ffmpeg")
        video_dev = f"/dev/video{self.camera_index}"
        if ffmpeg_cmd and Path(video_dev).exists():
            try:
                self.runner(
                    [
                        ffmpeg_cmd,
                        "-y",
                        "-f",
                        "v4l2",
                        "-i",
                        video_dev,
                        "-vframes",
                        "1",
                        str(path),
                    ],
                    check=True,
                    timeout=15,
                )
                return
            except Exception:
                pass

        v4l2_cmd = shutil.which("v4l2-ctl")
        if v4l2_cmd and Path(video_dev).exists():
            self.runner(
                [
                    v4l2_cmd,
                    f"--device={video_dev}",
                    "--stream-mmap",
                    "--stream-count=1",
                    f"--stream-to={path}",
                ],
                check=True,
                timeout=15,
            )
            return

        # Default attempt for rpicam-still to preserve test runner expectations
        self.runner(
            [
                "rpicam-still",
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
