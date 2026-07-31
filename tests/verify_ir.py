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
SPI_SPEED    = 20_000_000         # 20 MHz (Official FLIR Lepton VoSPI speed)
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
    """Rock-solid single-pass 3-struct kernel ioctl VoSPI reader for FLIR Lepton on RPi5.

    Packs 3 spi_ioc_transfer structs (3936B, 3936B, 1968B) into a 96-byte C message array.
    Executes in a single fcntl.ioctl call inside Linux C kernel space.
    - Each transfer is <= 4096 bytes (satisfies kernel bufsiz limit with ZERO Errno 90 errors).
    - cs_change=0 on structs 0 & 1 keeps CS LOW continuously (ZERO microsecond latency gaps).
    - Prevents Lepton from entering the 0x1F/0x5F Discard Error State permanently.
    """

    XFER_STRUCT = struct.Struct("=QQIIHBBI")

    def __init__(self, spi_bus: int = 0, spi_device: int = 0,
                 speed: int = SPI_SPEED, mode: int = SPI_MODE):
        self.dev_path = f"/dev/spidev{spi_bus}.{spi_device}"
        self.speed = speed
        self.mode = mode
        self.fd = -1

        # Full frame buffer (60 rows x 82 words)
        self._capture_buf = np.zeros((ROWS, PACKET_WORDS), dtype=np.uint16)

        # Build 3 transfer structs: 24 pkts (3936B), 24 pkts (3936B), 12 pkts (1968B)
        msg_size = self.XFER_STRUCT.size
        self._xmit_buf = np.zeros(msg_size * 3, dtype=np.uint8)

        chunks = [(0, 24, 0), (24, 24, 0), (48, 12, 1)]  # (start_row, num_rows, cs_change)
        for idx, (start_row, num_rows, cs_change) in enumerate(chunks):
            rx_ptr = self._capture_buf.ctypes.data + PACKET_BYTES * start_row
            chunk_bytes = PACKET_BYTES * num_rows

            self.XFER_STRUCT.pack_into(
                self._xmit_buf, idx * msg_size,
                0,                                                             # tx_buf = 0 (read-only)
                rx_ptr,                                                        # rx_buf pointer
                chunk_bytes,                                                   # len (<= 3936 bytes)
                self.speed,                                                    # speed_hz
                0,                                                             # delay_usecs
                8,                                                             # bits_per_word
                cs_change,                                                     # cs_change (0 for 0&1, 1 for 2)
                0,                                                             # pad
            )

        # ioctl command for 3 structs (3 * 32 = 96 bytes)
        self._spi_ioc_msg = _IOC(1, SPI_IOC_MAGIC, 0, msg_size * 3)

    def open(self):
        self.fd = os.open(self.dev_path, os.O_RDWR)
        fcntl.ioctl(self.fd, SPI_IOC_WR_MODE, struct.pack("=B", self.mode))
        fcntl.ioctl(self.fd, SPI_IOC_WR_BITS_PER_WORD, struct.pack("=B", 8))
        fcntl.ioctl(self.fd, SPI_IOC_WR_MAX_SPEED_HZ, struct.pack("=I", self.speed))

    def close(self):
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1

    def read_frame_raw(self) -> np.ndarray:
        """Execute all 3 transfers in ONE single C kernel ioctl pass."""
        fcntl.ioctl(self.fd, self._spi_ioc_msg, self._xmit_buf)
        return self._capture_buf.copy()


def resync(reader: VoSPIReader, delay: float = 0.5):
    """VoSPI resync: hold CS high for ≥185ms (using 500ms to guarantee hardware reset)."""
    reader.close()
    time.sleep(delay)
    reader.open()


def dump_frame_headers(raw: np.ndarray, label: str = ""):
    """Print the first few packet headers from raw uint16 buffer."""
    print(f"  {label} Packet headers (first 10 of {raw.shape[0]}):")
    for i in range(min(10, ROWS)):
        w0 = int(raw[i, 0])
        header_flags = w0 & 0xFF
        pkt_num = (w0 >> 8) & 0xFF
        is_discard = (header_flags & 0x0F) == 0x0F
        print(f"    Pkt {i:02d}: Flags=0x{header_flags:02X}  "
              f"(PktNum={pkt_num}, Discard={is_discard})")


def try_capture(reader: VoSPIReader, max_attempts: int = 1500) -> np.ndarray | None:
    """Try to capture a valid 80x60 Lepton 2.x frame."""
    packets = [None] * ROWS
    collected = 0
    discard_streak = 0

    for attempt in range(max_attempts):
        raw = reader.read_frame_raw()
        
        for row in range(ROWS):
            w0 = int(raw[row, 0])
            header_flags = w0 & 0xFF
            pkt_num = (w0 >> 8) & 0xFF
            
            # Discard packet?
            if (header_flags & 0x0F) == 0x0F:
                discard_streak += 1
                continue
            
            discard_streak = 0
            
            if pkt_num < ROWS:
                if pkt_num == 0 and collected < ROWS:
                    packets = [None] * ROWS
                    collected = 0
                
                if packets[pkt_num] is None:
                    # Payload: words 2..81, byteswapped for Lepton big-endian thermal values
                    packets[pkt_num] = raw[row, 2:PACKET_WORDS].byteswap()
                    collected += 1
                    
                    if collected == ROWS:
                        print(f"    Attempt {attempt+1}: SUCCESS! All {ROWS} packets collected!")
                        frame = np.array(packets, dtype=np.uint16)
                        return frame

        # Trigger CS-high resync if we see continuous discards for over 500 packets
        if discard_streak > 500:
            print(f"    Attempt {attempt+1}: Continuous discards ({discard_streak}), resyncing for 500ms...")
            resync(reader, 0.5)
            discard_streak = 0

    return None


def apply_thermal_colormap(scaled_uint8: np.ndarray, colormap: str = "ironbow") -> np.ndarray:
    """Map 8-bit grayscale thermal frame to RGB color palette (Ironbow / Rainbow / Plasma / WhiteHot)."""
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
        # Grayscale (White Hot - Hotter is brighter)
        lut[:, 0] = np.arange(256, dtype=np.uint8)
        lut[:, 1] = np.arange(256, dtype=np.uint8)
        lut[:, 2] = np.arange(256, dtype=np.uint8)
    return lut[scaled_uint8]


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
    print("\n[2/3] Raw SPI frame read (3-struct kernel ioctl, CS held low across 60 packets)...")
    reader = VoSPIReader(spi_bus=0, spi_device=0, speed=SPI_SPEED, mode=SPI_MODE)
    try:
        reader.open()
        print(f"  Opened /dev/spidev0.0 (Mode={SPI_MODE}, Speed={SPI_SPEED/1e6:.0f}MHz)")
    except Exception as exc:
        print(f"  ERROR opening SPI device: {exc}")
        print("  Check: dtparam=spi=on in /boot/firmware/config.txt and reboot.")
        sys.exit(1)

    # First, do a 500ms resync to guarantee clean Lepton state reset on every run
    print("  Performing VoSPI resync (CS high for 500ms)...")
    resync(reader, 0.5)

    # Read one frame and show headers
    raw = reader.read_frame_raw()
    dump_frame_headers(raw, "[Diag]")

    # ── Step 3: Attempt to capture valid frame ──
    print("\n[3/3] Attempting to capture a valid thermal frame (polling for up to 5 seconds)...")
    resync(reader, 0.5)
    
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
    
    # ── 1. 14-Bit Masking (Remove hardware flag bits on bits 14-15) ──
    clean_frame = frame & 0x3FFF
    
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

    # 1. White Hot (Classic Medical Thermal Grayscale)
    filename_whitehot = f"{timestamp}_ir_whitehot.jpg"
    save_path_whitehot = image_dir / filename_whitehot
    img_whitehot = Image.fromarray(scaled, mode="L").resize((640, 480), Image.Resampling.BICUBIC)
    img_whitehot.save(save_path_whitehot, format="JPEG", quality=95)

    # 2. Black Hot (Hotter skin is darker)
    filename_blackhot = f"{timestamp}_ir_blackhot.jpg"
    save_path_blackhot = image_dir / filename_blackhot
    rgb_blackhot = apply_thermal_colormap(scaled, colormap="blackhot")
    img_blackhot = Image.fromarray(rgb_blackhot, mode="RGB").resize((640, 480), Image.Resampling.BICUBIC)
    img_blackhot.save(save_path_blackhot, format="JPEG", quality=95)

    # 3. Ironbow (Standard Thermal Color)
    filename_ironbow = f"{timestamp}_ir_ironbow.jpg"
    save_path_ironbow = image_dir / filename_ironbow
    rgb_ironbow = apply_thermal_colormap(scaled, colormap="ironbow")
    img_ironbow = Image.fromarray(rgb_ironbow, mode="RGB").resize((640, 480), Image.Resampling.BICUBIC)
    img_ironbow.save(save_path_ironbow, format="JPEG", quality=95)

    # 4. Rainbow (High Contrast Palette)
    filename_rainbow = f"{timestamp}_ir_rainbow.jpg"
    save_path_rainbow = image_dir / filename_rainbow
    rgb_rainbow = apply_thermal_colormap(scaled, colormap="rainbow")
    img_rainbow = Image.fromarray(rgb_rainbow, mode="RGB").resize((640, 480), Image.Resampling.BICUBIC)
    img_rainbow.save(save_path_rainbow, format="JPEG", quality=95)

    print(f"\n  SUCCESS: Saved White-Hot Thermal image to {save_path_whitehot}")
    print(f"  SUCCESS: Saved Black-Hot Thermal image to {save_path_blackhot}")
    print(f"  SUCCESS: Saved Ironbow Thermal image to {save_path_ironbow}")
    print(f"  SUCCESS: Saved Rainbow Thermal image to {save_path_rainbow}")

    print("\n" + "=" * 60)
    print("            Test Completed Successfully")
    print("=" * 60)


if __name__ == "__main__":
    main()
