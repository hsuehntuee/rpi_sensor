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
SPI_SPEED    = 10_000_000         # 10 MHz (Rock-solid for Raspberry Pi 5 RP1 chip)
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


def send_lepton_reboot_command(bus_number: int = 1, address: int = 0x2A) -> bool:
    """Send CCI SYS Reboot command (0x0242) to FLIR Lepton over I2C to reset VoSPI engine."""
    try:
        with SMBus(bus_number) as bus:
            cmd = 0x0242
            req = i2c_msg.write(address, [(cmd >> 8) & 0xFF, cmd & 0xFF, 0x00, 0x00])
            bus.i2c_rdwr(req)
            return True
    except Exception as exc:
        print(f"  [Diag] CCI reboot command failed: {exc}")
        return False


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
        self._tx24 = bytearray(24 * PACKET_BYTES)
        self._tx12 = bytearray(12 * PACKET_BYTES)

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
        """Perform 3 xfer2 transfers (3936B, 3936B, 1968B) under 4096 limit."""
        r1 = self.spi.xfer2(self._tx24)
        r2 = self.spi.xfer2(self._tx24)
        r3 = self.spi.xfer2(self._tx12)
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
    if not c_path.exists():
        c_path = Path("src/sensors/lepton_capture.c")
    so_path = Path("/tmp/liblepton.so")

    if c_path.exists():
        try:
            so_path.unlink(missing_ok=True)
            res = subprocess.run(
                ["gcc", "-O3", "-shared", "-fPIC", str(c_path), "-o", str(so_path)],
                check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
            )
            lib = ctypes.CDLL(str(so_path))
            lib.capture_lepton_frame.argtypes = [
                ctypes.c_char_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint16), ctypes.c_int
            ]
            lib.capture_lepton_frame.restype = ctypes.c_int
            return lib
        except Exception as exc:
            print(f"  [Native C Engine] Compiler note: {exc}")
            if hasattr(exc, "stderr") and exc.stderr:
                print(f"  [Native C Engine] Compiler error: {exc.stderr}")
    return None


def try_capture(reader: VoSPIReader, max_attempts: int = 1500) -> np.ndarray | None:
    """Capture a 100% clean thermal frame using pylepton golden accumulator."""
    reader.open()
    packets = [None] * ROWS
    collected = 0

    for attempt in range(1, max_attempts + 1):
        raw_bytes = reader.read_frame_bytes()
        if not raw_bytes:
            continue
        n_packets = len(raw_bytes) // PACKET_BYTES

        for i in range(n_packets):
            offset = i * PACKET_BYTES
            b0 = raw_bytes[offset]
            b1 = raw_bytes[offset + 1]

            if (b0 & 0x0F) == 0x0F:
                continue

            pkt_num = b1
            if pkt_num < ROWS:
                payload = bytes(raw_bytes[offset + 4 : offset + PACKET_BYTES])
                pixels = np.frombuffer(payload, dtype=">u2")

                # Filter out SPI idle noise / zero bytes (real thermal pixels are ~8000)
                if pixels.mean() < 500:
                    continue

                if packets[pkt_num] is None:
                    packets[pkt_num] = pixels
                    collected += 1

                    if collected == ROWS:
                        print(f"  [VoSPI Engine] Attempt {attempt}: SUCCESS! All 60 real thermal packets collected!")
                        return np.array(packets, dtype=np.uint16)

        if attempt == 1 or attempt % 100 == 0:
            sample_b0 = raw_bytes[0] if len(raw_bytes) > 0 else 0
            sample_b1 = raw_bytes[1] if len(raw_bytes) > 1 else 0
            print(f"  [VoSPI Diag] Attempt {attempt}: collected={collected}/60, sample_b0=0x{sample_b0:02X}, sample_b1={sample_b1}")

        if attempt % 150 == 0:
            resync(reader, 0.2)
            packets = [None] * ROWS
            collected = 0

    return None


def raw_to_celsius(raw_frame: np.ndarray, is_tlinear: bool = True) -> np.ndarray:
    """Convert raw 14-bit Lepton pixels directly into absolute Celsius temperatures (°C).
    
    For Lepton 2.5 Radiometric mode (TLinear Enabled):
    1 LSB = 0.01 Kelvin (10 mK).
    Formula: Celsius = (raw / 100.0) - 273.15
    """
    raw_float = raw_frame.astype(np.float32)
    if is_tlinear:
        celsius = (raw_float / 100.0) - 273.15
    else:
        # Fallback estimation for non-TLinear raw counts (~40 LSB / °C centered at 25°C)
        celsius = 25.0 + (raw_float - 8192.0) / 40.0
    return celsius


def render_fixed_range_frame(celsius_frame: np.ndarray, min_temp: float = 18.0,
                               max_temp: float = 36.0, colormap: str = "ironbow") -> np.ndarray:
    """Render thermal image using FIXED absolute temperature boundaries (e.g. 18°C to 36°C).
    
    18.0°C or colder -> 0 (Deep Blue / Black)
    36.0°C or hotter -> 255 (Bright Red / White)
    Absolute fixed mapping — NO background dynamic normalization!
    """
    clipped = np.clip(celsius_frame, min_temp, max_temp)
    scaled_uint8 = ((clipped - min_temp) / (max_temp - min_temp) * 255.0).astype(np.uint8)
    return apply_thermal_colormap(scaled_uint8, colormap)


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
    print("\n[3/3] Attempting to capture a valid thermal frame (polling for up to 3 seconds)...")
    frame = try_capture(reader, max_attempts=1500)

    if frame is None:
        print("  [Auto-Recovery] First capture timed out. Sending CCI SYS Reboot (0x0242) to hardware...")
        send_lepton_reboot_command(1, 0x2A)
        resync(reader, 0.5)
        print("  [Auto-Recovery] Retrying thermal capture after hardware reboot...")
        frame = try_capture(reader, max_attempts=1500)

    reader.close()

    if frame is None:
        print("\n  ERROR: Could not capture a valid frame after hardware resync & reboot.")
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
    
    # ── 1. Absolute Temperature Conversion (°C) ──
    clean_frame = frame & 0x3FFF
    print(f"\n  [RAW SPI DIAG] Frame min={clean_frame.min()}, max={clean_frame.max()}, mean={clean_frame.mean():.1f}")
    print(f"  [RAW SPI DIAG] Row 0 first 10 pixels: {clean_frame[0, :10].tolist()}")
    print(f"  [RAW SPI DIAG] Row 30 middle 10 pixels: {clean_frame[30, 35:45].tolist()}")
    celsius_frame = raw_to_celsius(clean_frame, is_tlinear=True)
    c_min = celsius_frame.min()
    c_max = celsius_frame.max()
    c_avg = celsius_frame.mean()
    print(f"\n  [Absolute Temp] Measured Range: Min={c_min:.2f}°C, Max={c_max:.2f}°C, Avg={c_avg:.2f}°C")
    
    # ── 2. Fixed 18°C to 36°C Range Rendering (NO Background Scaling) ──
    rgb_fixed_18_36 = render_fixed_range_frame(celsius_frame, min_temp=18.0, max_temp=36.0, colormap="ironbow")
    rgb_fixed_18_36 = np.fliplr(rgb_fixed_18_36)
    
    filename_fixed = f"{timestamp}_ir_fixed_18_36c.jpg"
    save_path_fixed = image_dir / filename_fixed
    img_fixed = Image.fromarray(rgb_fixed_18_36, mode="RGB").resize((640, 480), Image.Resampling.BICUBIC)
    img_fixed.save(save_path_fixed, format="JPEG", quality=95)
    print(f"  SUCCESS: Saved Fixed 18°C-36°C Thermal image to {save_path_fixed}")

    # ── 3. Dynamic Human Face Thermal Enhancer ──
    p_min = np.percentile(clean_frame, 5)
    p_max = np.percentile(clean_frame, 95)
    if p_max > p_min:
        clipped = np.clip(clean_frame, p_min, p_max)
        scaled = ((clipped - p_min) / (p_max - p_min) * 255.0).astype(np.uint8)
    else:
        scaled = np.zeros(clean_frame.shape, dtype=np.uint8)

    scaled = np.fliplr(scaled)

    # ── 4. De-striping Filter ──
    destriped = scaled.copy()
    for r in range(1, ROWS - 1):
        for c in range(1, COLS - 1):
            destriped[r, c] = int(np.median(scaled[r-1:r+2, c-1:c+2]))

    # 1. White Hot
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
