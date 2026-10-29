import subprocess
from pathlib import Path
from unittest.mock import Mock, patch

from src.sensors.camera_rgb import PiCamera


def test_pi_camera_uses_supported_rpicam_command(tmp_path: Path):
    def fake_rpicam(command, **kwargs):
        # 模擬 rpicam-still 真的寫出照片（PiCamera 會檢查檔案存在且非 0 byte）；
        # 在 Docker 內會先試 nsenter，這裡讓它失敗以走到容器內的 rpicam-still。
        if command[0] != "nsenter":
            Path(command[command.index("--output") + 1]).write_bytes(b"jpeg")

    runner = Mock(side_effect=fake_rpicam)
    camera = PiCamera(tmp_path, camera_index=1, runner=runner)

    with patch("src.sensors.camera_rgb.shutil.which",
               side_effect=lambda name: name if name == "rpicam-still" else None):
        path = camera.capture()

    command = runner.call_args.args[0]
    assert command[0] == "rpicam-still"
    assert command[command.index("--camera") + 1] == "1"
    assert command[command.index("--output") + 1] == str(path)
    assert runner.call_args.kwargs == {
        "check": True, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL, "timeout": 30,
    }
