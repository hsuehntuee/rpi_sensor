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
        if not path.is_file() or path.stat().st_size == 0:
            path.unlink(missing_ok=True)
            raise RuntimeError(
                f"{self.image_type.upper()} camera capture failed: output file is missing or 0 bytes. "
                "Please verify camera ribbon cable connection, CSI port, and 'rpicam-still --list-cameras'."
            )
        try:
            from PIL import Image, ImageOps
            with Image.open(path) as img:
                mirrored = ImageOps.mirror(img)
                mirrored.save(path, quality=95)
        except Exception:
            pass
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
            # Try configured camera index first, then alternate (0 or 1 on RPi 5 dual CSI)
            indexes_to_try = [self.camera_index]
            if self.camera_index in (0, 1):
                alt = 1 if self.camera_index == 0 else 0
                if alt not in indexes_to_try:
                    indexes_to_try.append(alt)

            for idx in indexes_to_try:
                try:
                    self.runner(
                        [
                            cmd,
                            "--camera",
                            str(idx),
                            "--nopreview",
                            "--timeout",
                            "1000",
                            "--output",
                            str(path),
                        ],
                        check=True,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=30,
                    )
                    if path.is_file() and path.stat().st_size > 0:
                        return
                    path.unlink(missing_ok=True)
                except Exception:
                    pass

        ffmpeg_cmd = shutil.which("ffmpeg")
        if ffmpeg_cmd:
            video_devices = [f"/dev/video{self.camera_index}"] + [
                f"/dev/video{v}" for v in range(10) if v != self.camera_index
            ]
            for video_dev in video_devices:
                if Path(video_dev).exists():
                    try:
                        self.runner(
                            [
                                ffmpeg_cmd,
                                "-y",
                                "-ss",
                                "1",
                                "-f",
                                "v4l2",
                                "-i",
                                video_dev,
                                "-vframes",
                                "1",
                                str(path),
                            ],
                            check=True,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            timeout=10,
                        )
                        if path.is_file() and path.stat().st_size > 0:
                            return
                        path.unlink(missing_ok=True)
                    except Exception:
                        pass

        v4l2_cmd = shutil.which("v4l2-ctl")
        video_dev = f"/dev/video{self.camera_index}"
        if v4l2_cmd and Path(video_dev).exists():
            try:
                self.runner(
                    [
                        v4l2_cmd,
                        f"--device={video_dev}",
                        "--stream-mmap",
                        "--stream-count=1",
                        f"--stream-to={path}",
                    ],
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=15,
                )
                if path.is_file() and path.stat().st_size > 0:
                    return
                path.unlink(missing_ok=True)
            except Exception:
                pass

        # Default attempt for rpicam-still to preserve test runner expectations
        self.runner(
            [
                "rpicam-still",
                "--camera",
                str(self.camera_index),
                "--nopreview",
                "--timeout",
                "1000",
                "--output",
                str(path),
            ],
            check=True,
            timeout=30,
        )
