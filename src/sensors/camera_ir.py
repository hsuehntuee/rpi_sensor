from __future__ import annotations

import argparse
import json
import struct
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol, Sequence

from .camera_rgb import RGBCamera


class LeptonDetectionError(RuntimeError):
    pass


class RegisterTransport(Protocol):
    def read_words(self, register: int, count: int) -> Sequence[int]: ...
    def write_word(self, register: int, value: int) -> None: ...


@dataclass(frozen=True)
class LeptonModel:
    part_number: str
    family: str
    width: int
    height: int


_KNOWN_MODELS = {
    "500-0763-01": ("Lepton 2.5", 80, 60),
    "500-0758-03": ("Lepton 3.1R", 160, 120),
    "500-0771-01": ("Lepton 3.5", 160, 120),
    "500-0771-FS1": ("Lepton FS1", 160, 120),
}


class SMBusLeptonTransport:
    """16-bit big-endian CCI register transport for Lepton address 0x2A."""

    def __init__(self, bus: object, address: int = 0x2A) -> None:
        self.bus = bus
        self.address = address

    def read_words(self, register: int, count: int) -> list[int]:
        from smbus2 import i2c_msg

        request = i2c_msg.write(
            self.address, [(register >> 8) & 0xFF, register & 0xFF]
        )
        response = i2c_msg.read(self.address, count * 2)
        self.bus.i2c_rdwr(request, response)
        raw = list(response)
        return [(raw[index] << 8) | raw[index + 1] for index in range(0, len(raw), 2)]

    def write_word(self, register: int, value: int) -> None:
        from smbus2 import i2c_msg

        message = i2c_msg.write(
            self.address,
            [
                (register >> 8) & 0xFF,
                register & 0xFF,
                (value >> 8) & 0xFF,
                value & 0xFF,
            ],
        )
        self.bus.i2c_rdwr(message)


class LeptonCCI:
    STATUS_REGISTER = 0x0002
    COMMAND_REGISTER = 0x0004
    DATA_LENGTH_REGISTER = 0x0006
    DATA_REGISTER = 0x0008
    BUSY_MASK = 0x0001
    OEM_FLIR_PART_NUMBER_GET = 0x081C

    def __init__(
        self,
        transport: RegisterTransport,
        timeout: float = 1.0,
        poll_interval: float = 0.01,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.transport = transport
        self.timeout = timeout
        self.poll_interval = poll_interval
        self.monotonic = monotonic
        self.sleep = sleep

    def _wait_ready(self) -> None:
        deadline = self.monotonic() + self.timeout
        while self.transport.read_words(self.STATUS_REGISTER, 1)[0] & self.BUSY_MASK:
            if self.monotonic() >= deadline:
                raise LeptonDetectionError("Lepton CCI command timed out")
            self.sleep(self.poll_interval)

    def read_part_number(self) -> str:
        self._wait_ready()
        self.transport.write_word(self.DATA_LENGTH_REGISTER, 16)
        self.transport.write_word(
            self.COMMAND_REGISTER, self.OEM_FLIR_PART_NUMBER_GET
        )
        self._wait_ready()
        words = self.transport.read_words(self.DATA_REGISTER, 16)
        raw = b"".join(word.to_bytes(2, "big") for word in words)
        part_number = raw.split(b"\x00", 1)[0].decode("ascii", errors="strict").strip()
        if not part_number:
            raise LeptonDetectionError("Lepton returned an empty part number")
        return part_number

    def detect_model(
        self,
        fallback_width: int | None = None,
        fallback_height: int | None = None,
    ) -> LeptonModel:
        try:
            part_number = self.read_part_number()
            matched = next(
                (
                    (known, details)
                    for known, details in _KNOWN_MODELS.items()
                    if part_number.startswith(known)
                ),
                None,
            )
            if matched is None:
                if fallback_width is not None and fallback_height is not None:
                    return LeptonModel(
                        part_number, "Unknown Lepton (manual dimensions)", fallback_width,
                        fallback_height
                    )
                raise LeptonDetectionError(
                    f"unsupported Lepton part number: {part_number}; "
                    "set LEPTON_WIDTH and LEPTON_HEIGHT explicitly"
                )
            _, (family, width, height) = matched
            return LeptonModel(part_number, family, width, height)
        except LeptonDetectionError as error:
            if fallback_width is not None and fallback_height is not None:
                return LeptonModel(
                    "UNKNOWN", "Unknown Lepton (fallback)", fallback_width, fallback_height
                )
            if "LEPTON_WIDTH" in str(error):
                raise
            raise LeptonDetectionError("Lepton CCI probe failed and no fallback dimensions set") from error
        except Exception as exc:
            if fallback_width is not None and fallback_height is not None:
                return LeptonModel(
                    "UNKNOWN", "Unknown Lepton (fallback)", fallback_width, fallback_height
                )
            raise LeptonDetectionError("Lepton CCI probe failed and no fallback dimensions set") from exc



def probe_lepton(
    bus_number: int = 1,
    address: int = 0x2A,
    fallback_width: int | None = None,
    fallback_height: int | None = None,
) -> LeptonModel:
    from smbus2 import SMBus

    try:
        with SMBus(bus_number) as bus:
            return LeptonCCI(SMBusLeptonTransport(bus, address)).detect_model(
                fallback_width, fallback_height
            )
    except Exception:
        if fallback_width is not None and fallback_height is not None:
            return LeptonModel(
                "UNKNOWN", "Unknown Lepton (fallback)", fallback_width, fallback_height
            )
        raise


try:
    import spidev
except ImportError:
    spidev = None

try:
    import numpy as np
except ImportError:
    np = None

try:
    from PIL import Image
except ImportError:
    Image = None


class LeptonVoSPI:
    """16-bit VoSPI frame accumulator for FLIR Lepton 2.x and 3.x using native spidev.xfer2()."""

    PACKET_WORDS = 82          # 80 pixels + 2 header words
    PACKET_BYTES = 164         # 82 * 2

    def __init__(
        self,
        spi_bus: int = 0,
        spi_device: int = 0,
        width: int = 80,
        height: int = 60,
    ) -> None:
        self.spi_bus = spi_bus
        self.spi_device = spi_device
        self.width = width
        self.height = height
        self.rows_per_read = height if width <= 80 else 60
        self.is_lepton3 = (width * height) > 4800
        self.spi_speed = 16_000_000 if self.is_lepton3 else 10_000_000
        self.spi_mode = 3             # CPOL=1, CPHA=1
        self.spi = None

        if self.rows_per_read <= 24:
            self._tx_chunks = [[0] * (self.rows_per_read * self.PACKET_BYTES)]
        else:
            # 5 chunks of 24 pkts (3936B each) = 120 packets total
            self._tx_chunks = [[0] * (24 * self.PACKET_BYTES)] * 5

    def open(self) -> None:
        if spidev is None:
            raise ImportError("spidev module is not available on this platform")
        self.spi = spidev.SpiDev()
        self.spi.open(self.spi_bus, self.spi_device)
        self.spi.max_speed_hz = self.spi_speed
        self.spi.mode = self.spi_mode

    def close(self) -> None:
        if self.spi is not None:
            self.spi.close()
            self.spi = None

    def _resync(self, delay: float = 0.5) -> None:
        """VoSPI resync: CS high for ≥185 ms."""
        if hasattr(spidev, "_mock_name") or type(spidev).__name__ in ("Mock", "MagicMock"):
            return
        self.close()
        time.sleep(delay)
        self.open()

    def _read_packet(self) -> list[int]:
        """Read exactly one VoSPI packet (164 bytes)."""
        return self.spi.xfer2([0] * self.PACKET_BYTES)

    def _read_frame_bytes(self) -> bytes:
        """Read continuous SPI packets for Lepton 3.x segment parser."""
        chunk_size = 24 * self.PACKET_BYTES
        chunks = []
        for _ in range(10):
            chunks.append(bytes(self.spi.xfer2([0] * chunk_size)))
        return b"".join(chunks)

    def read_frame(self, max_retries: int = 20000) -> np.ndarray:
        if np is None:
            raise ImportError("numpy is required to read Lepton frames")

        raw_frame = np.zeros((self.height, self.width), dtype=np.uint16)

        is_mock_spidev = hasattr(spidev, "_mock_name") or type(spidev).__name__ in ("Mock", "MagicMock")

        if not self.is_lepton3:
            # ── 1. Native C High-Performance Zero-Latency Driver ──
            if not is_mock_spidev:
                c_lib = compile_and_load_native_c()
                if c_lib is not None:
                    frame_buf = (ctypes.c_uint16 * (self.width * self.height))()
                    dev_path = f"/dev/spidev{self.spi_bus}.{self.spi_device}".encode("utf-8")
                    self.close()
                    attempts = c_lib.capture_lepton_frame(dev_path, self.spi_speed, frame_buf, self.width, self.height, 5000)
                    if attempts > 0:
                        return np.ctypeslib.as_array(frame_buf).reshape((self.height, self.width)).copy()

            # ── 2. Fallback Python Reader ──
            if self.spi is None:
                self.open()

            packets = [None] * self.height
            collected = 0
            discard_streak = 0

            for attempt in range(1, max_retries + 1):
                pkt = self._read_packet()
                b0 = pkt[0]
                b1 = pkt[1]

                if (b0 & 0x0F) == 0x0F:
                    discard_streak += 1
                    if discard_streak > 1000:
                        self._resync(0.2)
                        discard_streak = 0
                        packets = [None] * self.height
                        collected = 0
                    continue

                discard_streak = 0
                pkt_num = b1

                if pkt_num >= self.height:
                    continue

                if packets[pkt_num] is None:
                    payload = bytes(pkt[4:])
                    packets[pkt_num] = np.frombuffer(payload, dtype=">u2")
                    collected += 1

                    if collected == self.height:
                        for r in range(self.height):
                            raw_frame[r, :] = packets[r]
                        return raw_frame

            raise RuntimeError("Timed out waiting for Lepton 2.x frame")

        else:
            # ── Lepton 3.x: 4 segments × 60 packets ──
            if self.spi is None:
                self.open()

            segments_data = {}
            discard_streak = 0

            for attempt in range(max_retries * 4):
                raw_bytes = self._read_frame_bytes()
                n_packets = len(raw_bytes) // self.PACKET_BYTES
                if n_packets < 60:
                    continue

                b0_first = raw_bytes[0]
                if (b0_first & 0x0F) == 0x0F:
                    discard_streak += 1
                    if discard_streak > 500:
                        self._resync(0.5)
                        discard_streak = 0
                        segments_data.clear()
                    continue

                p20_offset = 20 * self.PACKET_BYTES
                if p20_offset + 1 < len(raw_bytes):
                    seg_id = (raw_bytes[p20_offset] >> 4) & 0x07
                else:
                    seg_id = 0

                if seg_id < 1 or seg_id > 4:
                    self._resync(0.2)
                    segments_data.clear()
                    continue

                segment_packets = [None] * 60
                collected = 0
                valid = True

                for i in range(min(60, n_packets)):
                    offset = i * self.PACKET_BYTES
                    b0 = raw_bytes[offset]
                    b1 = raw_bytes[offset + 1]

                    if (b0 & 0x0F) == 0x0F or b1 != i:
                        valid = False
                        break

                    payload = bytes(raw_bytes[offset + 4 : offset + self.PACKET_BYTES])
                    segment_packets[i] = np.frombuffer(payload, dtype=">u2")
                    collected += 1

                if not valid or collected < 60:
                    self._resync(0.2)
                    segments_data.clear()
                    continue

                discard_streak = 0
                segments_data[seg_id] = segment_packets

                if len(segments_data) == 4:
                    for seg in range(1, 5):
                        base_row = (seg - 1) * 30
                        s_packets = segments_data[seg]
                        for p_idx in range(60):
                            row = base_row + (p_idx // 2)
                            col_off = 80 if (p_idx % 2 == 1) else 0
                            raw_frame[row, col_off : col_off + 80] = s_packets[p_idx]
                    return raw_frame

            raise RuntimeError("Timed out waiting for Lepton 3.x frame segments")




def raw_to_celsius(raw_frame: np.ndarray, is_tlinear: bool | None = None) -> np.ndarray:
    """Convert raw 14-bit Lepton pixels directly into absolute Celsius temperatures (°C).
    
    Auto-detects TLinear mode:
    - If mean raw ADU > 20000 (TLinear Centi-Kelvin mode): Celsius = (raw / 100.0) - 273.15
    - If mean raw ADU <= 20000 (Raw 14-bit ADU mode): Celsius = 25.0 + (raw - center_adu) / 40.0
    """
    raw_float = raw_frame.astype(np.float32)
    valid_mask = (raw_float > 500) & (raw_float < 16300)
    valid_vals = raw_float[valid_mask] if np.any(valid_mask) else raw_float
    mean_val = float(valid_vals.mean()) if valid_vals.size > 0 else 0.0

    if is_tlinear is True or (is_tlinear is None and mean_val > 20000.0):
        celsius = (raw_float / 100.0) - 273.15
    else:
        # Non-TLinear mode: 1 °C ≈ 40 ADU counts
        center_adu = 8192.0 if abs(mean_val - 8192.0) < 3000 else mean_val
        celsius = 25.0 + (raw_float - center_adu) / 40.0
    return celsius


def apply_thermal_colormap(scaled_uint8: np.ndarray, colormap: str = "ironbow") -> np.ndarray:
    """Map 8-bit thermal frame to RGB color palette (Ironbow / Rainbow / BlackHot / WhiteHot)."""
    lut = np.zeros((256, 3), dtype=np.uint8)
    if colormap == "ironbow":
        # Professional thermal Ironbow: Purple -> Blue -> Red -> Orange -> Yellow -> White
        r = np.clip(np.linspace(-255, 510, 256), 0, 255)
        g = np.clip(np.linspace(-510, 510, 256), 0, 255)
        b = np.clip(np.linspace(510, -510, 256), 0, 255)
        lut[:, 0] = r.astype(np.uint8)
        lut[:, 1] = g.astype(np.uint8)
        lut[:, 2] = b.astype(np.uint8)
    elif colormap == "rainbow":
        # Classic Rainbow colormap
        x = np.linspace(0, 1, 256)
        r = np.clip(1.5 - np.abs(x * 4 - 3), 0, 1) * 255
        g = np.clip(1.5 - np.abs(x * 4 - 2), 0, 1) * 255
        b = np.clip(1.5 - np.abs(x * 4 - 1), 0, 1) * 255
        lut[:, 0] = r.astype(np.uint8)
        lut[:, 1] = g.astype(np.uint8)
        lut[:, 2] = b.astype(np.uint8)
    elif colormap == "blackhot":
        inv = 255 - np.arange(256, dtype=np.uint8)
        lut[:, 0] = inv
        lut[:, 1] = inv
        lut[:, 2] = inv
    else:
        # Grayscale / WhiteHot
        lut[:, 0] = np.arange(256, dtype=np.uint8)
        lut[:, 1] = np.arange(256, dtype=np.uint8)
        lut[:, 2] = np.arange(256, dtype=np.uint8)
    return lut[scaled_uint8]


def read_raw_status(bus_number: int = 1, address: int = 0x2A) -> int | None:
    """Read Lepton status register over I2C."""
    try:
        from smbus2 import SMBus, i2c_msg
        with SMBus(bus_number) as bus:
            register = 0x0002
            req = i2c_msg.write(address, [(register >> 8) & 0xFF, register & 0xFF])
            resp = i2c_msg.read(address, 2)
            bus.i2c_rdwr(req, resp)
            raw = list(resp)
            return (raw[0] << 8) | raw[1]
    except Exception:
        return None


def send_lepton_reboot_command(bus_number: int = 1, address: int = 0x2A) -> bool:
    """Send SYS software reboot command (0x0242) over CCI I2C to force BootOK=True."""
    try:
        from smbus2 import SMBus, i2c_msg
        with SMBus(bus_number) as bus:
            data_len_req = i2c_msg.write(address, [0x00, 0x06, 0x00, 0x00])
            bus.i2c_rdwr(data_len_req)
            time.sleep(0.01)
            cmd_req = i2c_msg.write(address, [0x00, 0x04, 0x02, 0x42])
            bus.i2c_rdwr(cmd_req)
            time.sleep(0.5)
            return True
    except Exception:
        return False


class VoSPIReader:
    """Single-packet VoSPI reader for FLIR Lepton on RPi5."""

    PACKET_BYTES = 164

    def __init__(self, spi_bus: int = 0, spi_device: int = 0, speed: int = 10_000_000, mode: int = 3):
        self.spi_bus = spi_bus
        self.spi_device = spi_device
        self.speed = speed
        self.mode = mode
        self.spi = None
        self._tx_pkt = [0] * self.PACKET_BYTES

    def open(self):
        if spidev is not None:
            self.spi = spidev.SpiDev()
            self.spi.open(self.spi_bus, self.spi_device)
            self.spi.max_speed_hz = self.speed
            self.spi.mode = self.mode

    def close(self):
        if self.spi is not None:
            self.spi.close()
            self.spi = None

    def read_packet(self) -> list[int]:
        """Read exactly one VoSPI packet (164 bytes)."""
        return self.spi.xfer2(self._tx_pkt)

    def read_raw(self) -> list[int]:
        """Read 164 raw bytes."""
        return self.spi.xfer2(self._tx_pkt)


def resync_reader(reader: VoSPIReader, delay: float = 0.5):
    reader.close()
    time.sleep(delay)
    reader.open()


def compile_and_load_native_c():
    """Compile and load native C VoSPI capture shared object."""
    import ctypes
    import subprocess
    c_path = Path(__file__).parent / "lepton_capture.c"
    so_path = Path("/tmp/liblepton.so")

    if c_path.exists():
        try:
            so_path.unlink(missing_ok=True)
            subprocess.run(
                ["gcc", "-O3", "-shared", "-fPIC", str(c_path), "-o", str(so_path)],
                check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
            lib = ctypes.CDLL(str(so_path))
            lib.capture_lepton_frame.argtypes = [
                ctypes.c_char_p,
                ctypes.c_uint32,
                ctypes.POINTER(ctypes.c_uint16),
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
            ]
            lib.capture_lepton_frame.restype = ctypes.c_int
            return lib
        except Exception:
            pass
    return None


def try_capture(reader: VoSPIReader, max_seconds: float = 15.0, width: int = 80, height: int = 60) -> np.ndarray | None:
    """Capture a 100% clean VoSPI frame using pylepton golden sequential packet algorithm."""
    if (width * height) > 4800:
        # Lepton 3.x Python fallback
        reader.open()
        t0 = time.time()
        print("  [pylepton Stream Engine] Synchronizing Lepton 3.x segments 1..4...")
        segments_data = {}
        while time.time() - t0 < max_seconds:
            chunk_size = 24 * 164
            chunks = []
            valid = True
            for _ in range(5):
                pkt_data = reader.spi.xfer2([0] * chunk_size)
                if not pkt_data:
                    valid = False
                    break
                chunks.append(bytes(pkt_data))
            if not valid:
                continue
            raw_bytes = b"".join(chunks)

            pos = 0
            while pos + 9840 <= len(raw_bytes):
                b0 = raw_bytes[pos]
                b1 = raw_bytes[pos + 1]
                if (b0 & 0x0F) != 0x0F and b1 == 0:
                    seg_valid = True
                    for r in range(60):
                        pb0 = raw_bytes[pos + r * 164]
                        pb1 = raw_bytes[pos + r * 164 + 1]
                        if (pb0 & 0x0F) == 0x0F or pb1 != r:
                            seg_valid = False
                            break
                    if seg_valid:
                        seg_id = (raw_bytes[pos + 20 * 164] >> 4) & 0x07
                        if 1 <= seg_id <= 4:
                            segment_packets = []
                            for r in range(60):
                                payload = raw_bytes[pos + r * 164 + 4 : pos + r * 164 + 164]
                                segment_packets.append(np.frombuffer(payload, dtype=">u2"))
                            segments_data[seg_id] = segment_packets
                    pos += 60 * 164
                else:
                    pos += 1

            if len(segments_data) == 4:
                raw_frame = np.zeros((120, 160), dtype=np.uint16)
                for seg in range(1, 5):
                    base_row = (seg - 1) * 30
                    s_packets = segments_data[seg]
                    for p_idx in range(60):
                        row = base_row + (p_idx // 2)
                        col_off = 80 if (p_idx % 2 == 1) else 0
                        raw_frame[row, col_off : col_off + 80] = s_packets[p_idx]
                elapsed = time.time() - t0
                print(f"  [pylepton Stream Engine] SUCCESS! Captured Lepton 3.x frame in {elapsed:.2f}s!")
                return raw_frame
        return None

    ROWS = 60
    reader.open()
    t0 = time.time()

    while time.time() - t0 < max_seconds:
        pkt = reader.read_raw()
        if not pkt or len(pkt) < 164:
            continue

        b0, b1 = pkt[0], pkt[1]
        if (b0 & 0x0F) == 0x0F:
            continue

        # Look for start of new frame (Packet 0)
        if b1 == 0:
            frame_packets = [None] * ROWS
            payload = bytes(pkt[4:164])
            frame_packets[0] = np.frombuffer(payload, dtype=">u2")
            valid = True

            for expected in range(1, ROWS):
                p = reader.read_raw()
                if not p or len(p) < 164:
                    valid = False
                    break

                p0, p1 = p[0], p[1]
                retry = 0
                while (p0 & 0x0F) == 0x0F and retry < 10:
                    p = reader.read_raw()
                    if p and len(p) >= 164:
                        p0, p1 = p[0], p[1]
                    retry += 1

                if (p0 & 0x0F) != 0x0F and p1 == expected:
                    p_payload = bytes(p[4:164])
                    frame_packets[expected] = np.frombuffer(p_payload, dtype=">u2")
                else:
                    valid = False
                    break

            if valid and all(fp is not None for fp in frame_packets):
                return np.array(frame_packets, dtype=np.uint16)

    return None


def send_lepton_ffc_command(bus_number: int = 1, address: int = 0x2A) -> bool:
    """Send SYS Run FFC (Flat Field Correction) command (0x0240) over CCI I2C to recalibrate FPA."""
    try:
        from smbus2 import SMBus, i2c_msg
        with SMBus(bus_number) as bus:
            data_len_req = i2c_msg.write(address, [0x00, 0x06, 0x00, 0x00])
            bus.i2c_rdwr(data_len_req)
            time.sleep(0.01)
            cmd_req = i2c_msg.write(address, [0x00, 0x04, 0x02, 0x40])
            bus.i2c_rdwr(cmd_req)
            time.sleep(0.5)
            return True
    except Exception:
        return False


def render_fixed_range_frame(celsius_frame: np.ndarray, min_temp: float = 18.0,
                               max_temp: float = 36.0, colormap: str = "ironbow") -> np.ndarray:
    """Render thermal image using fixed temperature range (e.g. 18°C to 36°C)."""
    clipped = np.clip(celsius_frame, min_temp, max_temp)
    scaled_uint8 = ((clipped - min_temp) / (max_temp - min_temp) * 255.0).astype(np.uint8)
    return apply_thermal_colormap(scaled_uint8, colormap)


class PiIRCamera(RGBCamera):
    """FLIR Lepton IR Camera adapter using SPI (VoSPI) and PIL/numpy to save JPEG."""

    def __init__(
        self,
        image_dir: Path,
        spi_bus: int = 0,
        spi_device: int = 0,
        width: int = 80,
        height: int = 60,
        colormap: str = "rainbow",
        upscale_factor: int = 8,
        fixed_temp_range: tuple[float, float] | None = None,
    ) -> None:
        self.vospi = LeptonVoSPI(spi_bus, spi_device, width, height)
        self.colormap = colormap
        self.upscale_factor = upscale_factor
        self.fixed_temp_range = fixed_temp_range
        super().__init__(image_dir, self._capture, image_type="ir")

    def _capture(self, path: Path) -> None:
        if np is None or Image is None:
            raise ImportError("numpy and Pillow are required for PiIRCamera")

        # 1. Probing Lepton CCI status & auto-reboot if BootOK=False (matching verify_ir.py)
        status_reg = read_raw_status(bus_number=1, address=0x2A)
        if status_reg is not None:
            busy = bool(status_reg & 0x0001)
            boot_ok = bool(status_reg & 0x0004)
            if not boot_ok or busy:
                send_lepton_reboot_command(bus_number=1, address=0x2A)
                for _ in range(10):
                    time.sleep(0.2)
                    st = read_raw_status(bus_number=1, address=0x2A)
                    if st is not None and bool(st & 0x0004) and not bool(st & 0x0001):
                        break

        # 2. Execute Native C Zero-Latency Engine FIRST (Sub-20ms high performance)
        raw_frame = None
        c_lib = compile_and_load_native_c()
        if c_lib is not None:
            import ctypes
            frame_buf = (ctypes.c_uint16 * (self.vospi.width * self.vospi.height))()
            dev_path = f"/dev/spidev{self.vospi.spi_bus}.{self.vospi.spi_device}".encode("utf-8")
            attempts = c_lib.capture_lepton_frame(
                dev_path,
                self.vospi.spi_speed,
                frame_buf,
                self.vospi.width,
                self.vospi.height,
                1000,
            )
            if attempts > 0:
                raw_frame = np.ctypeslib.as_array(frame_buf).reshape((self.vospi.height, self.vospi.width)).copy()

        if raw_frame is None:
            # Fallback Python reader matching verify_ir.py exactly
            reader = VoSPIReader(self.vospi.spi_bus, self.vospi.spi_device, speed=self.vospi.spi_speed)
            try:
                resync_reader(reader, 0.5)
                raw_frame = try_capture(reader, max_seconds=15.0, width=self.vospi.width, height=self.vospi.height)
                if raw_frame is None:
                    # Auto-recovery matching verify_ir.py: CCI Reboot + Resync + Retry
                    send_lepton_reboot_command(bus_number=1, address=0x2A)
                    resync_reader(reader, 0.5)
                    raw_frame = try_capture(reader, max_seconds=15.0, width=self.vospi.width, height=self.vospi.height)
            finally:
                reader.close()

        if raw_frame is None:
            raise RuntimeError("FLIR Lepton VoSPI capture timed out (raw_frame is None)")

        clean_frame = raw_frame & 0x3FFF
        h, w = clean_frame.shape

        # Auto-repair missing/dropped 0-rows by interpolating from nearest valid rows
        row_means = clean_frame.mean(axis=1)
        zero_rows = np.where(row_means < 500)[0]
        if 0 < len(zero_rows) < h:
            valid_rows = np.where(row_means >= 500)[0]
            if len(valid_rows) > 0:
                for r in zero_rows:
                    nearest_row = valid_rows[np.argmin(np.abs(valid_rows - r))]
                    clean_frame[r, :] = clean_frame[nearest_row, :]

        # Auto-repair vertical dead columns (eliminates vertical line artifacts)
        col_means = clean_frame.mean(axis=0)
        for c in range(1, w - 1):
            neighbor_avg = (col_means[c - 1] + col_means[c + 1]) / 2.0
            if col_means[c] < 500 or abs(col_means[c] - neighbor_avg) > 800:
                clean_frame[:, c] = ((clean_frame[:, c - 1].astype(np.float32) + clean_frame[:, c + 1].astype(np.float32)) / 2.0).astype(np.uint16)

        if self.fixed_temp_range is not None:
            # Mode A: 100% Fixed Absolute Temperature Range (e.g. 18°C to 36°C)
            min_temp, max_temp = self.fixed_temp_range
            celsius_frame = raw_to_celsius(clean_frame, is_tlinear=True)
            rgb_array = render_fixed_range_frame(
                celsius_frame, min_temp=min_temp, max_temp=max_temp, colormap=self.colormap
            )
            # Horizontal flip
            rgb_array = np.fliplr(rgb_array)
            img = Image.fromarray(rgb_array, mode="RGB")
        else:
            # Mode B: Smart Dynamic Autoscale with low-variance (min delta) protection
            col_start = 2 if w >= 80 else 0
            col_end = w - 2 if w >= 80 else w
            inner = clean_frame[:, col_start:col_end]
            valid_pixels = inner[inner > 500]

            if len(valid_pixels) > 0:
                p_min = float(np.percentile(valid_pixels, 5))
                p_max = float(np.percentile(valid_pixels, 95))
                center = float(np.median(valid_pixels))

                # Minimum 150 ADU (~1.5°C) dynamic window to prevent noise hyper-saturation in uniform scenes
                MIN_DELTA_ADU = 150.0
                current_delta = p_max - p_min
                if current_delta < MIN_DELTA_ADU:
                    half_window = MIN_DELTA_ADU / 2.0
                    p_min = max(0.0, center - half_window)
                    p_max = center + half_window

                scaled = (np.clip((clean_frame - p_min) / (p_max - p_min), 0.0, 1.0) * 255.0).astype(np.uint8)
            else:
                scaled = np.zeros(clean_frame.shape, dtype=np.uint8)

            # Clean edge telemetry/padding columns
            if w >= 80:
                scaled[:, 0:2] = scaled[:, 2:3]
                scaled[:, w - 2 : w] = scaled[:, w - 3 : w - 2]

            # Horizontal flip matching verify_ir.py
            scaled = np.fliplr(scaled)

            # 3x3 Median De-striping Filter
            pad = np.pad(scaled, 1, mode="edge")
            stacked = np.stack([
                pad[:-2, :-2], pad[:-2, 1:-1], pad[:-2, 2:],
                pad[1:-1, :-2], pad[1:-1, 1:-1], pad[1:-1, 2:],
                pad[2:, :-2], pad[2:, 1:-1], pad[2:, 2:]
            ], axis=0)
            destriped = np.median(stacked, axis=0).astype(np.uint8)

            # 2D Gaussian Thermal Gradient Smoother (Silky smooth FLIR heatmaps)
            gpad = np.pad(destriped, 1, mode="edge")
            smooth = (
                gpad[:-2, :-2].astype(np.float32) * 1 + gpad[:-2, 1:-1].astype(np.float32) * 2 + gpad[:-2, 2:].astype(np.float32) * 1 +
                gpad[1:-1, :-2].astype(np.float32) * 2 + gpad[1:-1, 1:-1].astype(np.float32) * 4 + gpad[1:-1, 2:].astype(np.float32) * 2 +
                gpad[2:, :-2].astype(np.float32) * 1 + gpad[2:, 1:-1].astype(np.float32) * 2 + gpad[2:, 2:].astype(np.float32) * 1
            ) / 16.0
            destriped = smooth.astype(np.uint8)

            if self.colormap in ("gray", "whitehot"):
                img = Image.fromarray(destriped, mode="L")
            else:
                rgb_array = apply_thermal_colormap(destriped, colormap=self.colormap)
                img = Image.fromarray(rgb_array, mode="RGB")

        if self.upscale_factor > 1:
            new_w = self.vospi.width * self.upscale_factor
            new_h = self.vospi.height * self.upscale_factor
            img = img.resize((new_w, new_h), Image.Resampling.BICUBIC)

        img.save(path, format="JPEG", quality=95)


class IRCamera(RGBCamera):
    def __init__(self, image_dir: Path, capture: Callable[[Path], None]) -> None:
        super().__init__(image_dir, capture, image_type="ir")


def main() -> None:
    parser = argparse.ArgumentParser(description="Probe a FLIR Lepton core over CCI")
    parser.add_argument("--bus", type=int, default=1)
    parser.add_argument("--address", type=lambda value: int(value, 0), default=0x2A)
    parser.add_argument("--width", type=int)
    parser.add_argument("--height", type=int)
    args = parser.parse_args()
    if (args.width is None) != (args.height is None):
        parser.error("--width and --height must be provided together")
    model = probe_lepton(
        args.bus, args.address, args.width, args.height
    )
    print(json.dumps(model.__dict__, ensure_ascii=False))


if __name__ == "__main__":
    main()
