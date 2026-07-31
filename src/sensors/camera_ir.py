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
    """16-bit VoSPI frame accumulator for FLIR Lepton 2.x and 3.x.

    Uses raw ioctl SPI_IOC_MESSAGE to read entire frames in a single
    kernel call, keeping CS asserted across all packets.  This avoids
    the per-packet CS toggle that causes permanent desynchronisation
    on Raspberry Pi 5 / RP1.
    """

    PACKET_WORDS = 82          # 80 pixels + 2 header words
    PACKET_BYTES = 164         # 82 * 2

    # struct spi_ioc_transfer (must match kernel layout)
    _XFER = struct.Struct("=QQIIHBBI")

    # ioctl helpers
    @staticmethod
    def _ioc(direction, itype, nr, size):
        return (direction << 30) | (itype << 8) | nr | (size << 16)

    @staticmethod
    def _iow(itype, nr, fmt):
        return LeptonVoSPI._ioc(1, itype, nr, struct.calcsize(fmt))

    _SPI_IOC_MAGIC = ord("k")
    _SPI_IOC_WR_MODE          = None  # computed in __init__
    _SPI_IOC_WR_BITS_PER_WORD = None
    _SPI_IOC_WR_MAX_SPEED_HZ  = None

    def __init__(
        self,
        spi_bus: int = 0,
        spi_device: int = 0,
        width: int = 80,
        height: int = 60,
    ) -> None:
        self.dev_path = f"/dev/spidev{spi_bus}.{spi_device}"
        self.width = width
        self.height = height
        self.rows_per_read = height if width <= 80 else 60
        self.fd = -1
        self.is_lepton3 = (width * height) > 4800
        self.spi_speed = 10_000_000   # 10 MHz
        self.spi_mode = 3             # CPOL=1, CPHA=1

        # Compute ioctl constants
        M = self._SPI_IOC_MAGIC
        self._WR_MODE  = self._iow(M, 1, "=B")
        self._WR_BPW   = self._iow(M, 3, "=B")
        self._WR_SPEED = self._iow(M, 4, "=I")
        self._IOC_MSG  = self._iow(M, 0, self._XFER.format)

        # Pre-allocate buffers
        self._tx = np.zeros(self.PACKET_WORDS, dtype=np.uint16)
        self._rx = np.zeros((self.rows_per_read, self.PACKET_WORDS), dtype=np.uint16)
        # Build multi-message ioctl buffer
        msg_sz = self._XFER.size
        self._msg_buf = np.zeros(msg_sz * self.rows_per_read, dtype=np.uint8)
        for i in range(self.rows_per_read):
            cs_change = 1 if i == (self.rows_per_read - 1) else 0
            self._XFER.pack_into(
                self._msg_buf, i * msg_sz,
                self._tx.ctypes.data,
                self._rx.ctypes.data + self.PACKET_BYTES * i,
                self.PACKET_BYTES,
                self.spi_speed,
                0,   # delay_usecs
                8,   # bits_per_word
                cs_change,   # cs_change (1 for last packet)
                0,   # pad
            )

    def open(self) -> None:
        import os as _os, fcntl as _fcntl
        self.fd = _os.open(self.dev_path, _os.O_RDWR)
        _fcntl.ioctl(self.fd, self._WR_MODE,  struct.pack("=B", self.spi_mode))
        _fcntl.ioctl(self.fd, self._WR_BPW,   struct.pack("=B", 8))
        _fcntl.ioctl(self.fd, self._WR_SPEED,  struct.pack("=I", self.spi_speed))

    def close(self) -> None:
        if self.fd >= 0:
            import os as _os
            _os.close(self.fd)
            self.fd = -1

    def _resync(self, delay: float = 0.2) -> None:
        """VoSPI resync: CS high for ≥185 ms."""
        self.close()
        time.sleep(delay)
        self.open()

    def _read_frame_raw(self) -> np.ndarray:
        """One ioctl that reads rows_per_read packets with CS held low."""
        import fcntl as _fcntl
        _fcntl.ioctl(self.fd, self._IOC_MSG, self._msg_buf)
        return self._rx

    def read_frame(self, max_retries: int = 40) -> np.ndarray:
        if np is None:
            raise ImportError("numpy is required to read Lepton frames")
        if self.fd < 0:
            self.open()

        raw_frame = np.zeros((self.height, self.width), dtype=np.uint16)

        if not self.is_lepton3:
            # ── Lepton 2.x: 60 packets per frame ──
            packets = [None] * self.height
            collected = 0
            discard_streak = 0

            for attempt in range(max_retries):
                raw = self._read_frame_raw()
                for row in range(self.rows_per_read):
                    w0 = int(raw[row, 0])
                    header_flags = w0 & 0xFF
                    pkt_num = (w0 >> 8) & 0xFF

                    if (header_flags & 0x0F) == 0x0F:
                        discard_streak += 1
                        continue

                    discard_streak = 0

                    if pkt_num < self.height:
                        if pkt_num == 0 and collected < self.height:
                            packets = [None] * self.height
                            collected = 0

                        if packets[pkt_num] is None:
                            packets[pkt_num] = raw[row, 2:2 + self.width].byteswap()
                            collected += 1

                            if collected == self.height:
                                for r in range(self.height):
                                    raw_frame[r, :] = packets[r]
                                return raw_frame

                if discard_streak > 120:
                    self._resync(0.2)
                    discard_streak = 0

            raise RuntimeError("Timed out waiting for Lepton 2.x frame")

        else:
            # ── Lepton 3.x: 4 segments × 60 packets ──
            segments_data = {}
            for attempt in range(max_retries * 4):
                raw = self._read_frame_raw()
                w0 = int(raw[0, 0])
                b0 = (w0 >> 8) & 0xFF
                if (b0 & 0x0F) == 0x0F:
                    self._resync(0.2)
                    segments_data.clear()
                    continue

                # Read segment id from packet 20
                w20 = int(raw[20, 0])
                seg_id = ((w20 >> 8) >> 4) & 0x07
                if seg_id < 1 or seg_id > 4:
                    self._resync(0.2)
                    segments_data.clear()
                    continue

                # Validate packet sequence
                valid = True
                for row in range(60):
                    w = int(raw[row, 0])
                    hb = (w >> 8) & 0xFF
                    lb = w & 0xFF
                    if (hb & 0x0F) == 0x0F or lb != row:
                        valid = False
                        break

                if not valid:
                    self._resync(0.2)
                    segments_data.clear()
                    continue

                segments_data[seg_id] = raw[:, 2:2 + self.width].copy()

                if len(segments_data) == 4:
                    for seg in range(1, 5):
                        base_row = (seg - 1) * 30
                        seg_pixels = segments_data[seg]
                        for p_idx in range(60):
                            row = base_row + (p_idx // 2)
                            col_off = 80 if (p_idx % 2 == 1) else 0
                            raw_frame[row, col_off:col_off + 80] = seg_pixels[p_idx, :]
                    return raw_frame

            raise RuntimeError("Timed out waiting for Lepton 3.x frame segments")




class PiIRCamera(RGBCamera):
    """FLIR Lepton IR Camera adapter using SPI (VoSPI) and PIL/numpy to save JPEG."""

    def __init__(
        self,
        image_dir: Path,
        spi_bus: int = 0,
        spi_device: int = 0,
        width: int = 80,
        height: int = 60,
    ) -> None:
        self.vospi = LeptonVoSPI(spi_bus, spi_device, width, height)
        super().__init__(image_dir, self._capture, image_type="ir")

    def _capture(self, path: Path) -> None:
        if np is None or Image is None:
            raise ImportError("numpy and Pillow are required for PiIRCamera")
        try:
            self.vospi.open()
            raw_frame = self.vospi.read_frame()

            f_min = raw_frame.min()
            f_max = raw_frame.max()
            if f_max > f_min:
                scaled = ((raw_frame - f_min) / (f_max - f_min) * 255.0).astype(np.uint8)
            else:
                scaled = np.zeros(raw_frame.shape, dtype=np.uint8)

            img = Image.fromarray(scaled, mode="L")
            img.save(path, format="JPEG", quality=90)
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
