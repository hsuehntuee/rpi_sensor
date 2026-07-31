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
    """Communication class for FLIR Lepton module on SPI.

    Chunks 60 packets into 6 ioctl calls of 10 packets each (1640B payload).
    Each ioctl payload is <= 1640 bytes, satisfying kernel bufsiz limits down to 2048B.
    CS stays low for packets 0..58, and is de-asserted (HIGH) on packet 59.
    """

    XFER_STRUCT = struct.Struct("=QQIIHBBI")

    def __init__(self, spi_bus: int = 0, spi_device: int = 0,
                 speed: int = SPI_SPEED, mode: int = SPI_MODE):
        self.dev_path = f"/dev/spidev{spi_bus}.{spi_device}"
        self.speed = speed
        self.mode = mode
        self.fd = -1
        
        # Buffer for full frame (60 x 82 uint16)
        self._capture_buf = np.zeros((ROWS, PACKET_WORDS), dtype=np.uint16)
        
        # 6 chunks of 10 packets: (0,10), (10,10), (20,10), (30,10), (40,10), (50,10)
        self.chunks = [(i * 10, 10) for i in range(6)]
        msg_size = self.XFER_STRUCT.size
        
        self._msg_bufs = []
        self._ioc_cmds = []
        
        for chunk_idx, (start_pkt, num_pkts) in enumerate(self.chunks):
            buf_bytes = msg_size * num_pkts
            buf = np.zeros(buf_bytes, dtype=np.uint8)
            is_last_chunk = (chunk_idx == len(self.chunks) - 1)
            
            for i in range(num_pkts):
                global_pkt = start_pkt + i
                is_last_pkt = is_last_chunk and (i == num_pkts - 1)
                cs_change = 1 if is_last_pkt else 0
                rx_ptr = self._capture_buf.ctypes.data + PACKET_BYTES * global_pkt
                
                self.XFER_STRUCT.pack_into(
                    buf, i * msg_size,
                    0,                                                         # tx_buf = 0 (read-only)
                    rx_ptr,                                                    # rx_buf
                    PACKET_BYTES,                                              # len (164 bytes)
                    self.speed,                                                # speed_hz
                    0,                                                         # delay_usecs
                    8,                                                         # bits_per_word
                    cs_change,                                                 # cs_change
                    0,                                                         # pad
                )
            
            self._msg_bufs.append(buf)
            cmd = _IOC(1, SPI_IOC_MAGIC, 0, buf_bytes)
            self._ioc_cmds.append(cmd)

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
        """Issue 6 chunked ioctl calls (10 pkts each). CS stays low across all 60 pkts."""
        for cmd, buf in zip(self._ioc_cmds, self._msg_bufs):
            fcntl.ioctl(self.fd, cmd, buf)
        return self._capture_buf.copy()


def resync(reader: VoSPIReader, delay: float = 0.2):
    """VoSPI resync: hold CS high for ≥185ms by closing and reopening SPI."""
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
    """Capture a synchronized 80x60 Lepton frame aligned to Packet 0."""
    discard_streak = 0

    for attempt in range(max_attempts):
        raw = reader.read_frame_raw()
        
        # Inspect first packet in transfer
        w0 = int(raw[0, 0])
        header_flags = w0 & 0xFF
        pkt_num = (w0 >> 8) & 0xFF
        
        # Discard packet?
        if (header_flags & 0x0F) == 0x0F:
            discard_streak += 1
            if discard_streak > 500:
                print(f"    Attempt {attempt+1}: Continuous discards ({discard_streak}), resyncing...")
                resync(reader, 0.25)
                discard_streak = 0
            continue
        
        discard_streak = 0
        
        # If raw[0] is not Packet 0, the read started mid-frame; skip to resync frame boundaries
        if pkt_num != 0:
            continue

        # Verify that all 60 packets in this transfer are valid and sequential 0..59
        valid_frame = True
        packets = []
        for row in range(ROWS):
            w = int(raw[row, 0])
            hf = w & 0xFF
            pn = (w >> 8) & 0xFF
            if (hf & 0x0F) == 0x0F or pn != row:
                valid_frame = False
                break
            packets.append(raw[row, 2:PACKET_WORDS].byteswap())

        if valid_frame:
            print(f"    Attempt {attempt+1}: PERFECT SYNC! Full 60-packet synchronized frame captured!")
            return np.array(packets, dtype=np.uint16)

    return None


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
    raw = reader.read_frame_raw()
    dump_frame_headers(raw, "[Diag]")

    # ── Step 3: Attempt to capture valid frame ──
    print("\n[3/3] Attempting to capture a valid thermal frame (polling for up to 5 seconds)...")
    resync(reader, 0.2)
    
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
    
    f_min = frame.min()
    f_max = frame.max()
    print(f"  Frame stats: min={f_min}, max={f_max}, shape={frame.shape}")

    if f_max > f_min:
        scaled = ((frame.astype(np.float32) - f_min) / (f_max - f_min) * 255.0).astype(np.uint8)
    else:
        scaled = np.zeros(frame.shape, dtype=np.uint8)

    # Save Grayscale image (upscaled 8x to 640x480 for clear HD viewing)
    filename_gray = f"{timestamp}_ir_gray.jpg"
    save_path_gray = image_dir / filename_gray
    img_gray = Image.fromarray(scaled, mode="L")
    img_gray_hd = img_gray.resize((640, 480), Image.Resampling.BICUBIC)
    img_gray_hd.save(save_path_gray, format="JPEG", quality=95)

    # Save Ironbow Color Thermal image (upscaled 8x to 640x480 HD)
    filename_color = f"{timestamp}_ir_color.jpg"
    save_path_color = image_dir / filename_color
    rgb_array = apply_thermal_colormap(scaled, colormap="ironbow")
    img_color = Image.fromarray(rgb_array, mode="RGB")
    img_color_hd = img_color.resize((640, 480), Image.Resampling.BICUBIC)
    img_color_hd.save(save_path_color, format="JPEG", quality=95)

    print(f"\n  SUCCESS: Saved Grayscale thermal image to {save_path_gray}")
    print(f"  SUCCESS: Saved Ironbow COLOR thermal image to {save_path_color}")

    if image_dir == Path("/data"):
        print(f"  Host path: ./data/{filename_gray}")

    print("\n" + "=" * 60)
    print("            Test Completed Successfully")
    print("=" * 60)


if __name__ == "__main__":
    main()
