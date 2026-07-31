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

    with SMBus(bus_number) as bus:
        return LeptonCCI(SMBusLeptonTransport(bus, address)).detect_model(
            fallback_width, fallback_height
        )


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
        self.spi_speed = 20_000_000   # 20 MHz (Official FLIR Lepton speed)
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
        self.close()
        time.sleep(delay)
        self.open()

    def _read_frame_bytes(self) -> list[int]:
        """Single 29,520-byte transfer (180 packets): CS remains LOW continuously."""
        return self.spi.readbytes(self.height * self.PACKET_BYTES * 3)

    def read_frame(self, max_retries: int = 1500) -> np.ndarray:
        if np is None:
            raise ImportError("numpy is required to read Lepton frames")
        if self.spi is None:
            self.open()

        raw_frame = np.zeros((self.height, self.width), dtype=np.uint16)

        if not self.is_lepton3:
            # ── Lepton 2.x: 60 packets per frame ──
            for attempt in range(max_retries):
                raw_bytes = self._read_frame_bytes()
                packets = [None] * self.height
                collected = 0
                discard_count = 0

                for i in range(self.height * 3):
                    offset = i * self.PACKET_BYTES
                    b0 = raw_bytes[offset]
                    b1 = raw_bytes[offset + 1]

                    is_discard = (b0 & 0x0F) == 0x0F or b1 >= self.height
                    if is_discard:
                        discard_count += 1
                        continue

                    pkt_num = b1
                    if pkt_num < self.height:
                        if pkt_num == 0:
                            packets = [None] * self.height
                            collected = 0

                        if packets[pkt_num] is None:
                            payload = bytes(raw_bytes[offset + 4 : offset + self.PACKET_BYTES])
                            packets[pkt_num] = np.frombuffer(payload, dtype=">u2")
                            collected += 1

                            if collected == self.height:
                                for r in range(self.height):
                                    raw_frame[r, :] = packets[r]
                                return raw_frame

                if discard_count >= (self.height * 3) - 10:
                    self._resync(0.5)

            raise RuntimeError("Timed out waiting for Lepton 2.x frame")

        else:
            # ── Lepton 3.x: 4 segments × 60 packets ──
            segments_data = {}
            for attempt in range(max_retries * 4):
                w0 = int(raw[0, 0])
                header_flags = w0 & 0xFF
                if (header_flags & 0x0F) == 0x0F:
                    self._resync(0.2)
                    segments_data.clear()
                    continue

                w20 = int(raw[20, 0])
                seg_id = ((w20 >> 8) >> 4) & 0x07
                if seg_id < 1 or seg_id > 4:
                    self._resync(0.2)
                    segments_data.clear()
                    continue

                valid = True
                for row in range(60):
                    w = int(raw[row, 0])
                    hb = w & 0xFF
                    lb = (w >> 8) & 0xFF
                    if (hb & 0x0F) == 0x0F or lb != row:
                        valid = False
                        break

                if not valid:
                    self._resync(0.2)
                    segments_data.clear()
                    continue

                segments_data[seg_id] = raw[:, 2:2 + self.width].byteswap()

                if len(segments_data) == 4:
                    for seg in range(1, 5):
                        base_row = (seg - 1) * 30
                        s_pixels = segments_data[seg]
                        for p_idx in range(60):
                            row = base_row + (p_idx // 2)
                            col_off = 80 if (p_idx % 2 == 1) else 0
                            raw_frame[row, col_off:col_off + 80] = s_pixels[p_idx, :]
                    return raw_frame

            raise RuntimeError("Timed out waiting for Lepton 3.x frame segments")




def apply_thermal_colormap(scaled_uint8: np.ndarray, colormap: str = "ironbow") -> np.ndarray:
    """Map 8-bit grayscale thermal frame to RGB color palette (Ironbow / Rainbow)."""
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
        # Classic Rainbow/Jet colormap
        x = np.linspace(0, 1, 256)
        r = np.clip(1.5 - np.abs(x * 4 - 3), 0, 1) * 255
        g = np.clip(1.5 - np.abs(x * 4 - 2), 0, 1) * 255
        b = np.clip(1.5 - np.abs(x * 4 - 1), 0, 1) * 255
        lut[:, 0] = r.astype(np.uint8)
        lut[:, 1] = g.astype(np.uint8)
        lut[:, 2] = b.astype(np.uint8)
    else:
        # Grayscale
        lut[:, 0] = np.arange(256, dtype=np.uint8)
        lut[:, 1] = np.arange(256, dtype=np.uint8)
        lut[:, 2] = np.arange(256, dtype=np.uint8)
    return lut[scaled_uint8]


class PiIRCamera(RGBCamera):
    """FLIR Lepton IR Camera adapter using SPI (VoSPI) and PIL/numpy to save JPEG."""

    def __init__(
        self,
        image_dir: Path,
        spi_bus: int = 0,
        spi_device: int = 0,
        width: int = 80,
        height: int = 60,
        colormap: str = "ironbow",
        upscale_factor: int = 8,
    ) -> None:
        self.vospi = LeptonVoSPI(spi_bus, spi_device, width, height)
        self.colormap = colormap
        self.upscale_factor = upscale_factor
        super().__init__(image_dir, self._capture, image_type="ir")

    def _capture(self, path: Path) -> None:
        if np is None or Image is None:
            raise ImportError("numpy and Pillow are required for PiIRCamera")
        try:
            self.vospi.open()
            raw_frame = self.vospi.read_frame()

            clean_frame = raw_frame & 0x3FFF
            f_min = clean_frame.min()
            f_max = clean_frame.max()

            if f_max > f_min:
                scaled_minmax = ((clean_frame.astype(np.float32) - f_min) / (f_max - f_min) * 255.0).astype(np.uint8)
                hist, bins = np.histogram(scaled_minmax.flatten(), 256, [0, 256])
                cdf = hist.cumsum()
                cdf_m = np.ma.masked_equal(cdf, 0)
                cdf_m = (cdf_m - cdf_m.min()) * 255 / (cdf_m.max() - cdf_m.min())
                cdf_final = np.ma.filled(cdf_m, 0).astype('uint8')
                scaled = cdf_final[scaled_minmax]
            else:
                scaled = np.zeros(clean_frame.shape, dtype=np.uint8)

            if self.colormap == "gray":
                img = Image.fromarray(scaled, mode="L")
            else:
                rgb_array = apply_thermal_colormap(scaled, colormap=self.colormap)
                img = Image.fromarray(rgb_array, mode="RGB")

            if self.upscale_factor > 1:
                new_w = self.vospi.width * self.upscale_factor
                new_h = self.vospi.height * self.upscale_factor
                img = img.resize((new_w, new_h), Image.Resampling.BICUBIC)

            img.save(path, format="JPEG", quality=95)
        finally:
            self.vospi.close()


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
