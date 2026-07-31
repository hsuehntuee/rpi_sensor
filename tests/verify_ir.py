"""
FLIR Lepton IR Camera – Hardware Verification Script
=====================================================
Uses raw ioctl SPI_IOC_MESSAGE to read entire VoSPI frames in a single
kernel call, keeping CS low across all 60 packets (matching pylepton's
proven approach).  This avoids the per-packet CS toggle that causes
permanent desynchronisation on Raspberry Pi 5 / RP1.
"""
import ctypes
import fcntl
import os
import struct
import sys
import time
from pathlib import Path

# Add project root to sys.path so we can import src
sys.path.append(str(Path(__file__).parent.parent))

try:
    from smbus2 import SMBus, i2c_msg
    from src.config import load_settings
    import numpy as np
    from PIL import Image
    import spidev
except ImportError as err:
    print(f"Error importing modules: {err}")
    sys.exit(1)

# ── SPI ioctl constants ──────────────────────────────────────────────
SPI_IOC_MAGIC = ord("k")

def _IOC(direction, itype, nr, size):
    return (direction << 30) | (itype << 8) | nr | (size << 16)

def _IOW(itype, nr, fmt):
    return _IOC(1, itype, nr, struct.calcsize(fmt))

def _IOR(itype, nr, fmt):
    return _IOC(2, itype, nr, struct.calcsize(fmt))

SPI_IOC_WR_MODE          = _IOW(SPI_IOC_MAGIC, 1, "=B")
SPI_IOC_WR_BITS_PER_WORD = _IOW(SPI_IOC_MAGIC, 3, "=B")
SPI_IOC_WR_MAX_SPEED_HZ  = _IOW(SPI_IOC_MAGIC, 4, "=I")
SPI_IOC_RD_MODE          = _IOR(SPI_IOC_MAGIC, 1, "=B")

# ── VoSPI constants ──────────────────────────────────────────────────
ROWS         = 60
COLS         = 80
PACKET_WORDS = COLS + 2           # 82 uint16 = 164 bytes
PACKET_BYTES = PACKET_WORDS * 2   # 164 bytes
FRAME_BYTES  = ROWS * PACKET_BYTES  # 9840 bytes
SPI_SPEED    = 10_000_000         # 10 MHz (conservative for RPi5)
SPI_MODE     = 3                  # CPOL=1, CPHA=1


def read_raw_status(bus_number: int = 1, address: int = 0x2A) -> int | None:
    """Read Lepton status register over I2C."""
    try:
        with SMBus(bus_number) as bus:
            register = 0x0002
            req = i2c_msg.write(address, [(register >> 8) & 0xFF, register & 0xFF])
            resp = i2c_msg.read(address, 2)
            bus.i2c_rdwr(req, resp)
            raw = list(resp)
            return (raw[0] << 8) | raw[1]
    except Exception as exc:
        print(f"  [Diag] I2C status read failed: {exc}")
        return None


class VoSPIReader:
    """Read Lepton VoSPI frames using native spidev.xfer2().

    spi.xfer2([0]*9840) performs a single continuous SPI transfer in C,
    keeping CS low across all 60 packets and pulling CS high at the end.
    """

    def __init__(self, spi_bus: int = 0, spi_device: int = 0,
                 speed: int = SPI_SPEED, mode: int = SPI_MODE):
        self.spi_bus = spi_bus
        self.spi_device = spi_device
        self.speed = speed
        self.mode = mode
        self.spi = None
        # 3 chunks of zeros: 24 pkts (3936B), 24 pkts (3936B), 12 pkts (1968B)
        # Each chunk is <= 4096 to prevent Python spidev OverflowError
        self._tx24 = [0] * (24 * PACKET_BYTES)  # 3936 bytes
        self._tx12 = [0] * (12 * PACKET_BYTES)  # 1968 bytes

    def open(self):
        self.spi = spidev.SpiDev()
        self.spi.open(self.spi_bus, self.spi_device)
        self.spi.max_speed_hz = self.speed
        self.spi.mode = self.mode

    def close(self):
        if self.spi is not None:
            self.spi.close()
            self.spi = None

    def read_frame_bytes(self) -> list[int]:
        """Perform 3 xfer2 transfers (3936B, 3936B, 1968B) to stay under 4096 limit."""
        r1 = self.spi.xfer2(self._tx24)
        r2 = self.spi.xfer2(self._tx24)
        r3 = self.spi.xfer2(self._tx12)
        return r1 + r2 + r3


def resync(reader: VoSPIReader, delay: float = 0.2):
    """VoSPI resync: hold CS high for ≥185ms by closing and reopening SPI."""
    reader.close()
    time.sleep(delay)
    reader.open()


def dump_frame_headers(raw_bytes: list[int], label: str = ""):
    """Print the first few packet headers from raw xfer2 bytes."""
    print(f"  {label} Packet headers (first 10 of 60):")
    for i in range(min(10, ROWS)):
        offset = i * PACKET_BYTES
        b0 = raw_bytes[offset]
        b1 = raw_bytes[offset + 1]
        is_discard = (b0 & 0x0F) == 0x0F
        pkt_num = b1
        print(f"    Pkt {i:02d}: Flags=0x{b0:02X}  "
              f"(PktNum={pkt_num}, Discard={is_discard})")


def try_capture(reader: VoSPIReader, max_attempts: int = 50) -> np.ndarray | None:
    """Try to capture a valid 80x60 Lepton 2.x frame."""
    packets = [None] * ROWS
    collected = 0
    discard_streak = 0

    for attempt in range(max_attempts):
        raw_bytes = reader.read_frame_bytes()
        
        for row in range(ROWS):
            offset = row * PACKET_BYTES
            b0 = raw_bytes[offset]
            b1 = raw_bytes[offset + 1]
            
            # Discard packet?
            if (b0 & 0x0F) == 0x0F:
                discard_streak += 1
                continue
            
            discard_streak = 0
            pkt_num = b1
            
            if pkt_num < ROWS:
                if pkt_num == 0 and collected < ROWS:
                    packets = [None] * ROWS
                    collected = 0
                
                if packets[pkt_num] is None:
                    # Extract 160 payload bytes -> 80 big-endian uint16 values
                    payload_bytes = bytes(raw_bytes[offset + 4 : offset + PACKET_BYTES])
                    packets[pkt_num] = np.frombuffer(payload_bytes, dtype=">u2")
                    collected += 1
                    
                    if collected == ROWS:
                        print(f"    Attempt {attempt+1}: SUCCESS! All {ROWS} packets collected!")
                        frame = np.array(packets, dtype=np.uint16)
                        return frame

        if discard_streak > 120:
            print(f"    Attempt {attempt+1}: Continuous discards ({discard_streak}), resyncing...")
            resync(reader, 0.2)
            discard_streak = 0

    return None


def main() -> None:
    print("=" * 60)
    print("      FLIR Lepton IR Camera Test Script (ioctl mode)")
    print("=" * 60)

    # Load settings
    env_width, env_height = 80, 60
    try:
        settings = load_settings()
        if settings.lepton_width:
            env_width = settings.lepton_width
        if settings.lepton_height:
            env_height = settings.lepton_height
        print(f"Config: LEPTON_WIDTH={env_width}, LEPTON_HEIGHT={env_height}")
    except Exception:
        print("Info: Using default 80x60")

    # ── Step 1: I2C probe ──
    print("\n[1/3] Probing Lepton CCI (I2C bus 1, address 0x2A)...")
    status_reg = read_raw_status(bus_number=1, address=0x2A)
    if status_reg is not None:
        busy = bool(status_reg & 0x0001)
        boot_ok = bool(status_reg & 0x0004)
        print(f"  Status Register: 0x{status_reg:04X}  "
              f"(Busy={busy}, BootOK={boot_ok})")
        if not boot_ok:
            print("  WARNING: Boot not complete. Lepton may still be starting up.")
    else:
        print("  ERROR: Cannot reach Lepton over I2C. Check SDA/SCL wiring.")

    # ── Step 2: Raw SPI frame read ──
    print("\n[2/3] Raw SPI frame read (spidev.xfer2, CS held low across 60 packets)...")
    reader = VoSPIReader(spi_bus=0, spi_device=0, speed=SPI_SPEED, mode=SPI_MODE)
    try:
        reader.open()
        print(f"  Opened /dev/spidev0.0 (Mode={SPI_MODE}, Speed={SPI_SPEED/1e6:.0f}MHz)")
    except Exception as exc:
        print(f"  ERROR opening SPI device: {exc}")
        print("  Check: dtparam=spi=on in /boot/firmware/config.txt and reboot.")
        sys.exit(1)

    # First, do a resync to clear any stale state
    print("  Performing VoSPI resync (CS high for 200ms)...")
    resync(reader, 0.2)

    # Read one frame and show headers
    raw_bytes = reader.read_frame_bytes()
    dump_frame_headers(raw_bytes, "[Diag]")

    # ── Step 3: Attempt to capture valid frame ──
    print("\n[3/3] Attempting to capture a valid thermal frame...")
    resync(reader, 0.2)
    
    frame = try_capture(reader, max_attempts=40)
    reader.close()

    if frame is None:
        print("\n  ERROR: Could not capture a valid frame after 40 attempts.")
        print("\nSuggestions:")
        print("  - Verify /boot/firmware/config.txt has dtparam=spi=on")
        print("  - Verify there is NO dtoverlay=nospi10 in config.txt")
        print("  - Check SPI wiring: MISO→Pin21, SCLK→Pin23, CS→Pin24")
        print("  - Try: sudo reboot  (after fixing config.txt)")
        sys.exit(1)

    # Save the image
    image_dir = Path("/data")
    if not image_dir.exists():
        image_dir = Path(".")

    timestamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    filename = f"{timestamp}_ir.jpg"
    save_path = image_dir / filename

    f_min = frame.min()
    f_max = frame.max()
    print(f"  Frame stats: min={f_min}, max={f_max}, shape={frame.shape}")

    if f_max > f_min:
        scaled = ((frame.astype(np.float32) - f_min) / (f_max - f_min) * 255.0).astype(np.uint8)
    else:
        scaled = np.zeros(frame.shape, dtype=np.uint8)

    img = Image.fromarray(scaled, mode="L")
    img.save(str(save_path), format="JPEG", quality=90)
    print(f"\n  SUCCESS: Saved thermal image to {save_path}")
    if image_dir == Path("/data"):
        print(f"  Host path: ./data/{filename}")

    print("\n" + "=" * 60)
    print("            Test Completed Successfully")
    print("=" * 60)


if __name__ == "__main__":
    main()
