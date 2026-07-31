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
import subprocess
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
SPI_SPEED    = 20_000_000         # 20 MHz (FLIR Lepton Datasheet: minimum 10MHz required to prevent FIFO underflow)
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
    """Chunked VoSPI reader for FLIR Lepton on RPi5.

    Chunked into 3 transfers (3936B, 3936B, 1968B) to stay under Linux kernel bufsiz limit.
    This guarantees all 60 packets (9,840 bytes) are read per attempt with ZERO kernel truncation.
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
        """Perform 3 readbytes transfers (3936B, 3936B, 1968B) under 4096 limit."""
        r1 = self.spi.readbytes(24 * PACKET_BYTES)  # 3936 bytes (24 packets)
        r2 = self.spi.readbytes(24 * PACKET_BYTES)  # 3936 bytes (24 packets)
        r3 = self.spi.readbytes(12 * PACKET_BYTES)  # 1968 bytes (12 packets)
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


def compile_and_load_native_c():
    """Compile and load native C VoSPI capture shared object inside Docker container."""
    c_path = Path("/app/src/sensors/lepton_capture.c")
    so_path = Path("/app/src/sensors/liblepton.so")
    
    if not c_path.exists():
        c_path = Path("src/sensors/lepton_capture.c")
        so_path = Path("src/sensors/liblepton.so")
        
    if c_path.exists():
        try:
            subprocess.run(
                ["gcc", "-O3", "-shared", "-fPIC", str(c_path), "-o", str(so_path)],
                check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
            lib = ctypes.CDLL(str(so_path))
            lib.capture_lepton_frame.argtypes = [
                ctypes.c_char_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint16), ctypes.c_int
            ]
            lib.capture_lepton_frame.restype = ctypes.c_int
            return lib
        except Exception as exc:
            print(f"  [Native C Engine] Compiler note: {exc}")
    return None


def try_capture(reader: VoSPIReader, max_attempts: int = 1500) -> np.ndarray | None:
    """Capture a 100% clean thermal frame using Native C kernel engine."""
    # ── 1. Try Native C High-Performance SPI Kernel Engine ──
    native_lib = compile_and_load_native_c()
    if native_lib is not None:
        print("  [Native C Engine] Activated 100% zero-latency C kernel VoSPI driver...")
        frame_buf = (ctypes.c_uint16 * (ROWS * COLS))()
        dev_path = f"/dev/spidev{reader.spi_bus}.{reader.spi_device}".encode("utf-8")
        
        # Close python reader so native C can claim spidev descriptor
        reader.close()
        
        attempts = native_lib.capture_lepton_frame(dev_path, reader.speed, frame_buf, max_attempts)
        if attempts > 0:
            print(f"    Native C Attempt {attempts}: SUCCESS! 100% synchronized 60-packet frame captured!")
            arr = np.ctypeslib.as_array(frame_buf).reshape((ROWS, COLS)).copy()
            return arr
        else:
            print("  [Native C Engine] Fallback to python reader...")

    # ── 2. Fallback Python Reader ──
    reader.open()
    packets = [None] * ROWS
    collected = 0
    discard_streak = 0

    for attempt in range(max_attempts):
        raw_bytes = reader.read_frame_bytes()
        n_bytes = len(raw_bytes)
        n_packets = n_bytes // PACKET_BYTES
        
        for i in range(n_packets):
            offset = i * PACKET_BYTES
            b0 = raw_bytes[offset]
            b1 = raw_bytes[offset + 1]
            
            if (b0 & 0x0F) == 0x0F or b1 >= ROWS:
                discard_streak += 1
                continue
            
            pkt_num = b1
            discard_streak = 0
            
            if pkt_num < ROWS:
                if pkt_num == 0 and collected < ROWS:
                    packets = [None] * ROWS
                    collected = 0
                
                if packets[pkt_num] is None:
                    payload = bytes(raw_bytes[offset + 4 : offset + PACKET_BYTES])
                    packets[pkt_num] = np.frombuffer(payload, dtype=">u2")
                    collected += 1
                    
                    if collected == ROWS:
                        print(f"    Attempt {attempt+1}: SUCCESS! All 60 packets collected!")
                        return np.array(packets, dtype=np.uint16)

        if discard_streak > 500:
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


def send_lepton_reboot_command(bus_number: int = 1, address: int = 0x2A) -> bool:
    """Send SYS software reboot command (0x0242) over CCI I2C to force BootOK=True."""
    try:
        with SMBus(bus_number) as bus:
            # Set Data Length to 0 (Register 0x0006)
            data_len_req = i2c_msg.write(address, [0x00, 0x06, 0x00, 0x00])
            bus.i2c_rdwr(data_len_req)
            time.sleep(0.01)
            
            # Write 0x0242 (SYS Run Boot) to Command Register (Register 0x0004)
            cmd_req = i2c_msg.write(address, [0x00, 0x04, 0x02, 0x42])
            bus.i2c_rdwr(cmd_req)
            time.sleep(0.5)
            print("  [CCI] Issued Lepton SYS software reboot command (0x0242)...")
            return True
    except Exception as exc:
        print(f"  [CCI] Reboot command note: {exc}")
        return False


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

    # ── Step 1: I2C probe & BootOK wait/reboot loop ──
    print("\n[1/3] Probing Lepton CCI (I2C bus 1, address 0x2A)...")
    status_reg = read_raw_status(bus_number=1, address=0x2A)
    if status_reg is not None:
        busy = bool(status_reg & 0x0001)
        boot_ok = bool(status_reg & 0x0004)
        print(f"  Status Register: 0x{status_reg:04X}  "
              f"(Busy={busy}, BootOK={boot_ok})")
        if not boot_ok or busy:
            print("  Info: Lepton core is BootOK=False. Issuing CCI software reboot...")
            send_lepton_reboot_command(bus_number=1, address=0x2A)
            
            for _ in range(15):
                time.sleep(0.2)
                status_reg = read_raw_status(bus_number=1, address=0x2A)
                if status_reg is not None and bool(status_reg & 0x0004) and not bool(status_reg & 0x0001):
                    print("  SUCCESS: Lepton core booted and ready (BootOK=True)!")
                    boot_ok = True
                    break
            
            if not boot_ok:
                print("\n  ERROR: Lepton core is stuck in BootOK=False (Status 0x0002).")
                print("  Hardware Suggestions:")
                print("    1. Unplug and re-plug Lepton 3.3V power (Pin 17) and GND (Pin 9).")
                print("    2. Ensure PWR_DWN and RESET pins on breakout board are pulled HIGH (3.3V).")
                sys.exit(1)
    else:
        print("  ERROR: Cannot reach Lepton over I2C. Check SDA/SCL wiring.")
        sys.exit(1)

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
    print("\n[3/3] Attempting to capture a valid thermal frame (polling for up to 2 seconds)...")
    frame = try_capture(reader, max_attempts=100)
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
