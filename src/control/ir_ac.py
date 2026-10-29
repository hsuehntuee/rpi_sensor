"""ESP32 紅外線冷氣遙控（USB 序列埠，一行一個 JSON）。

ESP32 韌體在 esp32_ir/firmware（指令格式見 src/main.cpp 檔頭），接線見 README「冷氣紅外線遙控」。
這裡只負責：找 port、維持一條連線、送「完整冷氣狀態」、讀回韌體狀態。
"""
from __future__ import annotations

import glob
import json
import logging
import threading
import time
from typing import Any

LOGGER = logging.getLogger(__name__)

# 常見 ESP32 開發板 USB-UART 晶片：CP210x、CH340/CH9102
_ESP_USB_IDS = {(0x10C4, 0xEA60), (0x1A86, 0x7523), (0x1A86, 0x55D4)}

MODES = ("cool", "dry", "fan", "heat", "auto")
FANS = ("auto", "low", "medium", "high")


class AcRemoteError(RuntimeError):
    pass


def find_esp32_port() -> str | None:
    """找第一個看起來像 ESP32 開發板的序列埠。"""
    try:
        from serial.tools import list_ports

        for port in list_ports.comports():
            if (port.vid, port.pid) in _ESP_USB_IDS:
                return port.device
    except Exception:
        LOGGER.debug("list_ports failed", exc_info=True)
    for pattern in ("/dev/serial/by-id/*CP210*", "/dev/serial/by-id/*1a86*",
                    "/dev/serial/by-id/*CH340*", "/dev/ttyUSB*"):
        found = sorted(glob.glob(pattern))
        if found:
            return found[0]
    return None


class IrAcRemote:
    def __init__(self, port: str = "auto", protocol: str = "", model: str = "", baudrate: int = 115200) -> None:
        self.port_setting = port
        self.protocol = protocol.upper()
        self.model = model  # 同一協定有多種遙控器時才需要，例如 HITACHI_AC1 的 R-LT0541-HTA-B
        self.baudrate = baudrate
        self._serial: Any = None
        self._port: str | None = None
        self._lock = threading.Lock()

    # --- 連線 -------------------------------------------------------------
    def _open(self) -> Any:
        if self._serial is not None:
            return self._serial
        import serial

        port = find_esp32_port() if self.port_setting.lower() == "auto" else self.port_setting
        if not port:
            raise AcRemoteError("找不到 ESP32（USB 線有接好嗎？）")
        ser = serial.Serial()
        ser.port = port
        ser.baudrate = self.baudrate
        ser.timeout = 0.2
        # 避免開 port 時拉 DTR/RTS 讓 ESP32 重開機
        ser.dtr = False
        ser.rts = False
        try:
            ser.open()
        except Exception as exc:
            raise AcRemoteError(f"無法開啟 {port}: {exc}") from exc
        self._serial, self._port = ser, port
        return ser

    def _close(self) -> None:
        if self._serial is not None:
            try:
                self._serial.close()
            except Exception:
                pass
        self._serial = None

    def _request(self, payload: dict[str, Any], timeout: float = 3.0) -> dict[str, Any]:
        with self._lock:
            for attempt in (1, 2):  # 序列埠斷過（拔插 USB）就重開一次
                try:
                    ser = self._open()
                    ser.reset_input_buffer()
                    ser.write((json.dumps(payload) + "\n").encode())
                    deadline = time.monotonic() + timeout
                    while time.monotonic() < deadline:
                        line = ser.readline().decode(errors="replace").strip()
                        if not line.startswith("{"):
                            continue  # 開機訊息等雜訊
                        try:
                            msg = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if msg.get("cmd") == payload.get("cmd"):
                            return msg
                    raise AcRemoteError("ESP32 沒有回應")
                except AcRemoteError:
                    if attempt == 2:
                        raise
                    self._close()
                except Exception as exc:
                    self._close()
                    if attempt == 2:
                        raise AcRemoteError(f"序列埠錯誤: {exc}") from exc
        raise AcRemoteError("unreachable")

    # --- 對外 -------------------------------------------------------------
    def status(self) -> dict[str, Any]:
        fw = self._request({"cmd": "status"})
        ac = fw.get("ac") or {}
        state = {
            "power": bool(ac.get("power", False)),
            "mode": str(ac.get("mode", "cool")).lower(),
            "temp": ac.get("temp", 26),
            "fan": str(ac.get("fan", "auto")).lower(),
            "swing": str(ac.get("swingv", "off")).lower() != "off",
            "protocol": ac.get("protocol"),
        }
        return {
            "connected": True,
            "port": self._port,
            "protocol": self.protocol or state.get("protocol"),
            "state": state,
            "state_known": bool(fw.get("ac_known")),
            "last_ir": fw.get("last_ir"),
            "uptime_s": fw.get("uptime_s"),
        }

    def set_state(
        self,
        power: bool,
        mode: str = "cool",
        temp: float = 26,
        fan: str = "auto",
        swing: bool = False,
    ) -> dict[str, Any]:
        mode, fan = mode.lower(), fan.lower()
        if mode not in MODES:
            raise ValueError(f"mode 必須是 {MODES}")
        if fan not in FANS:
            raise ValueError(f"fan 必須是 {FANS}")
        temp = max(16, min(31, int(round(float(temp)))))
        payload: dict[str, Any] = {"cmd": "ac", "power": power, "mode": mode,
                                   "temp": temp, "fan": fan,
                                   "swingv": "auto" if swing else "off"}
        if self.protocol:
            payload["protocol"] = self.protocol
        if self.model:
            payload["model"] = self.model
        reply = self._request(payload)
        if not reply.get("ok"):
            raise AcRemoteError(reply.get("error", "ESP32 回報失敗"))
        return reply
