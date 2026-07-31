from __future__ import annotations

import argparse
import json
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
    """16-bit VoSPI frame accumulator for FLIR Lepton 2.x and 3.x."""

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
        self.spi = None
        self.is_lepton3 = (self.width * self.height) > 4800
        self.packet_size = 164

    def open(self) -> None:
        if spidev is None:
            raise ImportError("spidev module is not available on this platform")
        self.spi = spidev.SpiDev()
        self.spi.open(self.spi_bus, self.spi_device)
        self.spi.max_speed_hz = 8000000
        self.spi.mode = 3

    def close(self) -> None:
        if self.spi is not None:
            self.spi.close()
            self.spi = None

    def read_frame(self, max_retries: int = 5000) -> np.ndarray:
        if np is None:
            raise ImportError("numpy is required to read Lepton frames")
        if self.spi is None:
            self.open()

        raw_frame = np.zeros((self.height, self.width), dtype=np.uint16)

        if not self.is_lepton3:
            packets = [None] * 60
            discard_count = 0
            retries = 0

            while retries < max_retries:
                packet = self.spi.readbytes(self.packet_size)
                if (packet[0] & 0x0F) == 0x0F:
                    discard_count += 1
                    if discard_count > 1000:
                        self.close()
                        self.open()
                        discard_count = 0
                    continue

                discard_count = 0
                packet_num = packet[1]

                if packet_num >= 60:
                    continue

                if packet_num == 0:
                    packets = [None] * 60

                packets[packet_num] = packet[4:]

                if packet_num == 59 and all(p is not None for p in packets):
                    for i, p_data in enumerate(packets):
                        raw_frame[i, :] = np.frombuffer(bytes(p_data), dtype=">u2")
                    return raw_frame

                retries += 1
            raise RuntimeError("Timed out waiting for Lepton 2.x frame")

        else:
            segments = {1: [None] * 60, 2: [None] * 60, 3: [None] * 60, 4: [None] * 60}
            current_segment = 1
            discard_count = 0
            retries = 0

            while retries < max_retries:
                packet = self.spi.readbytes(self.packet_size)
                if (packet[0] & 0x0F) == 0x0F:
                    discard_count += 1
                    if discard_count > 1000:
                        self.close()
                        self.open()
                        discard_count = 0
                        current_segment = 1
                    continue

                discard_count = 0
                packet_num = packet[1]

                if packet_num >= 60:
                    continue

                if packet_num == 20:
                    seg_id = (packet[0] >> 4) & 0x07
                    if seg_id < 1 or seg_id > 4:
                        current_segment = 1
                        continue
                    if seg_id != current_segment:
                        current_segment = 1
                        if seg_id != 1:
                            continue

                segments[current_segment][packet_num] = packet[4:]

                if packet_num == 59:
                    if all(p is not None for p in segments[current_segment]):
                        if current_segment == 4:
                            for seg in range(1, 5):
                                seg_packets = segments[seg]
                                for p_idx, p_data in enumerate(seg_packets):
                                    row = (seg - 1) * 30 + (p_idx // 2)
                                    col_offset = 80 if (p_idx % 2 == 1) else 0
                                    raw_frame[row, col_offset:col_offset + 80] = np.frombuffer(
                                        bytes(p_data), dtype=">u2"
                                    )
                            return raw_frame
                        else:
                            current_segment += 1
                    else:
                        current_segment = 1
                        segments = {1: [None] * 60, 2: [None] * 60, 3: [None] * 60, 4: [None] * 60}

                retries += 1
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
