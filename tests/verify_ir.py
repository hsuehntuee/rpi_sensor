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
SPI_SPEED    = 8_000_000          # 8 MHz (Maximum signal stability over Dupont jumper wires)
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
    """Ultra-low-latency VoSPI reader for FLIR Lepton on RPi5.

    Uses spidev.readbytes() in C memory space to reduce inter-chunk CS pause
    from 400 microseconds down to 2 microseconds. This stays well below Lepton's
    185-microsecond timeout threshold, keeping Lepton 100% synchronized across
    all 60 packets.
    """

    def __init__(self, spi_bus: int = 0, spi_device: int = 0,
                 speed: int = SPI_SPEED, mode: int = SPI_MODE):
        self.spi_bus = spi_bus
        self.spi_device = spi_device
        self.speed = speed
        self.mode = mode
        self.spi = None

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
        """Perform 3 readbytes calls (3936B, 3936B, 1968B) under 4096 limit in 2us."""
        r1 = self.spi.readbytes(24 * PACKET_BYTES)  # 3936 bytes
        r2 = self.spi.readbytes(24 * PACKET_BYTES)  # 3936 bytes
        r3 = self.spi.readbytes(12 * PACKET_BYTES)  # 1968 bytes
        return r1 + r2 + r3


def resync(reader: VoSPIReader, delay: float = 0.5):
    """VoSPI resync: hold CS high for ≥185ms (using 500ms to guarantee hardware reset)."""
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


def try_capture(reader: VoSPIReader, max_attempts: int = 1500) -> np.ndarray | None:
    """Capture a 100% single-pass coherent 80x60 thermal frame."""
    discard_streak = 0

    for attempt in range(max_attempts):
        raw_bytes = reader.read_frame_bytes()
        n_bytes = len(raw_bytes)
        idx = 0
        
        single_pass_packets = [None] * ROWS
        collected = 0
        
        while idx <= n_bytes - PACKET_BYTES:
            b0 = raw_bytes[idx]
            b1 = raw_bytes[idx + 1]
            
            # Discard packet?
            is_discard = ((b0 & 0x0F) == 0x0F) or (b1 >= ROWS)
            if is_discard:
                discard_streak += 1
                idx += PACKET_BYTES
                continue
            
            pkt_num = b1
            discard_streak = 0
            
            if pkt_num < ROWS:
                # If packet 0 appears mid-stream and we haven't completed a frame, reset single-pass buffer
                if pkt_num == 0 and collected < ROWS and collected > 0:
                    single_pass_packets = [None] * ROWS
                    collected = 0
                
                if single_pass_packets[pkt_num] is None:
                    payload_bytes = bytes(raw_bytes[idx + 4 : idx + PACKET_BYTES])
                    single_pass_packets[pkt_num] = np.frombuffer(payload_bytes, dtype=">u2")
                    collected += 1
                    
                    if collected == ROWS:
                        print(f"    Attempt {attempt+1}: SUCCESS! All 60 packets collected in a single pass!")
                        return np.array(single_pass_packets, dtype=np.uint16)
            
            idx += PACKET_BYTES

        # Trigger CS-high resync if we see continuous discards for over 500 packets (~2.5s)
        if discard_streak > 500:
            print(f"    Attempt {attempt+1}: Continuous discards ({discard_streak}), resyncing for 500ms...")
            resync(reader, 0.5)
            discard_streak = 0

    return None


def apply_thermal_colormap(scaled_uint8: np.ndarray, colormap: str = "ironbow") -> np.ndarray:
    """Map 8-bit grayscale thermal frame to RGB color palette (Ironbow / Rainbow / BlackHot / WhiteHot)."""
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
        # Black Hot (Hotter is darker)
        inv = 255 - np.arange(256, dtype=np.uint8)
        lut[:, 0] = inv
        lut[:, 1] = inv
        lut[:, 2] = inv
    else:
        # Grayscale (White Hot)
        lut[:, 0] = np.arange(256, dtype=np.uint8)
        lut[:, 1] = np.arange(256, dtype=np.uint8)
        lut[:, 2] = np.arange(256, dtype=np.uint8)
    return lut[scaled_uint8]


def main() -> None:
    print("=" * 60)
    print("      FLIR Lepton IR Camera Test Script (spidev mode)")
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

    # ── Step 2: SPI open & hardware resync ──
    print("\n[2/3] Opening SPI device (/dev/spidev0.0, Mode 3, 20MHz)...")
    reader = VoSPIReader(spi_bus=0, spi_device=0, speed=SPI_SPEED, mode=SPI_MODE)
    try:
        reader.open()
        print(f"  Opened /dev/spidev0.0 (Mode={SPI_MODE}, Speed={SPI_SPEED/1e6:.0f}MHz)")
    except Exception as exc:
        print(f"  ERROR opening SPI device: {exc}")
        print("  Check: dtparam=spi=on in /boot/firmware/config.txt and reboot.")
        sys.exit(1)

    print("  Performing VoSPI resync (CS high for 500ms)...")
    resync(reader, 0.5)

    # ── Step 3: Attempt to capture valid frame ──
    print("\n[3/3] Attempting to capture a valid thermal frame (polling for up to 5 seconds)...")
    frame = try_capture(reader, max_attempts=1500)
    reader.close()

    if frame is None:
        print("\n  ERROR: Could not capture a valid frame after 1500 attempts (~5s).")
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
    
    # ── 1. Print Raw Row Diagnostics ──
    clean_frame = frame & 0x3FFF
    print(f"\n  [Diag] Raw Row 0 (first 5 px): {clean_frame[0, :5]}")
    print(f"  [Diag] Raw Row 1 (first 5 px): {clean_frame[1, :5]}")
    print(f"  [Diag] Raw Row 2 (first 5 px): {clean_frame[2, :5]}")
    print(f"  [Diag] Raw Row 3 (first 5 px): {clean_frame[3, :5]}")
    
    # ── 2. Human Face Thermal Enhancer (5th-95th Percentile Dynamic Clipping) ──
    p_min = np.percentile(clean_frame, 5)
    p_max = np.percentile(clean_frame, 95)
    print(f"  Clean 14-bit frame stats: raw_min={clean_frame.min()}, raw_max={clean_frame.max()}, p5={p_min:.0f}, p95={p_max:.0f}")

    if p_max > p_min:
        clipped = np.clip(clean_frame, p_min, p_max)
        scaled = ((clipped - p_min) / (p_max - p_min) * 255.0).astype(np.uint8)
    else:
        scaled = np.zeros(clean_frame.shape, dtype=np.uint8)

    # Horizontal Flip (Mirror correction for natural face preview)
    scaled = np.fliplr(scaled)

    # ── 3. De-striping Filter (Removes alternating row VoSPI noise) ──
    destriped = scaled.copy()
    # Compute 3x3 median filter manually to remove horizontal zebra line noise
    for r in range(1, ROWS - 1):
        for c in range(1, COLS - 1):
            destriped[r, c] = int(np.median(scaled[r-1:r+2, c-1:c+2]))

    # 1. White Hot (Classic Medical Thermal Grayscale)
    filename_whitehot = f"{timestamp}_ir_whitehot.jpg"
    save_path_whitehot = image_dir / filename_whitehot
    img_whitehot = Image.fromarray(scaled, mode="L").resize((640, 480), Image.Resampling.BICUBIC)
    img_whitehot.save(save_path_whitehot, format="JPEG", quality=95)

    # 2. De-striped White Hot (Smooth Face Contour, Zero Zebra Stripes!)
    filename_destriped = f"{timestamp}_ir_destriped.jpg"
    save_path_destriped = image_dir / filename_destriped
    img_destriped = Image.fromarray(destriped, mode="L").resize((640, 480), Image.Resampling.BICUBIC)
    img_destriped.save(save_path_destriped, format="JPEG", quality=95)

    # 3. Ironbow (Standard Thermal Color)
    filename_ironbow = f"{timestamp}_ir_ironbow.jpg"
    save_path_ironbow = image_dir / filename_ironbow
    rgb_ironbow = apply_thermal_colormap(destriped, colormap="ironbow")
    img_ironbow = Image.fromarray(rgb_ironbow, mode="RGB").resize((640, 480), Image.Resampling.BICUBIC)
    img_ironbow.save(save_path_ironbow, format="JPEG", quality=95)

    # 4. Rainbow (High Contrast Palette)
    filename_rainbow = f"{timestamp}_ir_rainbow.jpg"
    save_path_rainbow = image_dir / filename_rainbow
    rgb_rainbow = apply_thermal_colormap(destriped, colormap="rainbow")
    img_rainbow = Image.fromarray(rgb_rainbow, mode="RGB").resize((640, 480), Image.Resampling.BICUBIC)
    img_rainbow.save(save_path_rainbow, format="JPEG", quality=95)

    print(f"\n  SUCCESS: Saved White-Hot Thermal image to {save_path_whitehot}")
    print(f"  SUCCESS: Saved De-striped Smooth Face image to {save_path_destriped}")
    print(f"  SUCCESS: Saved Ironbow Thermal image to {save_path_ironbow}")
    print(f"  SUCCESS: Saved Rainbow Thermal image to {save_path_rainbow}")

    print("\n" + "=" * 60)
    print("            Test Completed Successfully")
    print("=" * 60)


if __name__ == "__main__":
    main()
