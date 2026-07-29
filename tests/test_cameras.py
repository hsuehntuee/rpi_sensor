from pathlib import Path
from unittest.mock import Mock

from src.sensors.camera_rgb import PiCamera


def test_pi_camera_uses_supported_rpicam_command(tmp_path: Path):
    runner = Mock()
    camera = PiCamera(tmp_path, camera_index=1, runner=runner)

    path = camera.capture()

    command = runner.call_args.args[0]
    assert command[0] == "rpicam-still"
    assert command[command.index("--camera") + 1] == "1"
    assert command[command.index("--output") + 1] == str(path)
    assert runner.call_args.kwargs == {"check": True, "timeout": 30}
