import json

import pytest

from src.control.ir_ac import AcRemoteError, IrAcRemote


class FakeSerial:
    """模擬 ESP32：記下送出的指令，回覆同 cmd 的 JSON（前面夾一行開機雜訊與 IR 事件）"""

    def __init__(self, replies):
        self.sent = []
        self.replies = replies
        self.lines = []

    def reset_input_buffer(self):
        pass

    def write(self, data):
        cmd = json.loads(data.decode())
        self.sent.append(cmd)
        reply = {"cmd": cmd["cmd"], "ok": True, **self.replies.get(cmd["cmd"], {})}
        self.lines = [b"ets Jul 29 2019\n", b'{"event":"ir","protocol":"NEC"}\n',
                      (json.dumps(reply) + "\n").encode()]

    def readline(self):
        return self.lines.pop(0) if self.lines else b""

    def close(self):
        pass


def make_remote(protocol, replies=None):
    remote = IrAcRemote("/dev/null", protocol)
    remote._serial = FakeSerial(replies or {})
    remote._port = "/dev/null"
    return remote


def test_set_state_sends_full_state_with_protocol():
    remote = make_remote("HITACHI_AC344")
    remote.set_state(power=True, mode="Cool", temp=25.4, fan="high", swing=True)
    assert remote._serial.sent[-1] == {
        "cmd": "ac", "power": True, "mode": "cool", "temp": 25, "fan": "high",
        "swingv": "auto", "protocol": "HITACHI_AC344",
    }


def test_status_normalises_firmware_state():
    remote = make_remote("TECO", {"status": {
        "ac_known": True,
        "ac": {"protocol": "TECO", "power": True, "mode": "Cool", "temp": 24, "fan": "Auto", "swingv": "Off"},
    }})
    status = remote.status()
    assert status["connected"] is True
    assert status["state"] == {"power": True, "mode": "cool", "temp": 24, "fan": "auto",
                               "swing": False, "protocol": "TECO"}


def test_invalid_mode_is_rejected():
    with pytest.raises(ValueError):
        make_remote("TECO").set_state(power=True, mode="turbo")


def test_firmware_error_is_raised():
    remote = make_remote("NOPE", {"ac": {"ok": False, "error": "unsupported protocol"}})
    with pytest.raises(AcRemoteError, match="unsupported protocol"):
        remote.set_state(power=True)
