import sys
import time
from pathlib import Path

# Add project root to sys.path so we can import src
sys.path.append(str(Path(__file__).parent.parent))

try:
    from src.sensors.camera_ir import probe_lepton, PiIRCamera
    from smbus2 import SMBus, i2c_msg
    from src.config import load_settings
    import spidev
except ImportError as err:
    print(f"Error importing modules: {err}")
    print("Please make sure you are running this from the project root directory.")
    sys.exit(1)

def read_raw_status(bus_number: int = 1, address: int = 0x2A) -> int | None:
    try:
        with SMBus(bus_number) as bus:
            # Register 0x0002 is the Status Register
            register = 0x0002
            request = i2c_msg.write(address, [(register >> 8) & 0xFF, register & 0xFF])
            response = i2c_msg.read(address, 2)
            bus.i2c_rdwr(request, response)
            raw = list(response)
            return (raw[0] << 8) | raw[1]
    except Exception as exc:
        print(f"  [Diag] Failed to read raw status register over I2C: {exc}")
        return None

def debug_spi(spi_bus: int = 0, spi_device: int = 0) -> None:
    print(f"\n[Diag] Starting raw SPI debug on /dev/spidev{spi_bus}.{spi_device}...")
    try:
        spi = spidev.SpiDev()
        spi.open(spi_bus, spi_device)
        spi.max_speed_hz = 8000000
        spi.mode = 3
        
        samples = []
        for _ in range(20):
            packet = spi.readbytes(164)
            samples.append(packet[:4])
        spi.close()
        
        print("  [Diag] First 4 bytes of 20 consecutive SPI packets:")
        for idx, s in enumerate(samples):
            header = f"0x{s[0]:02X} 0x{s[1]:02X} 0x{s[2]:02X} 0x{s[3]:02X}"
            is_discard = (s[0] & 0x0F) == 0x0F
            status = "DISCARD" if is_discard else f"Packet Num: {s[1]}"
            if s[0] == 0 and s[1] == 0 and s[2] == 0 and s[3] == 0:
                status = "ALL ZEROS (MISO low / disconnected)"
            elif s[0] == 255 and s[1] == 255 and s[2] == 255 and s[3] == 255:
                status = "ALL ONES (MISO high / disconnected)"
            print(f"    Packet {idx:02d}: {header} ({status})")
    except Exception as exc:
        print(f"  [Diag] Raw SPI read failed: {exc}")

def main() -> None:
    print("==================================================")
    print("      FLIR Lepton IR Camera Test Script           ")
    print("==================================================")
    
    # Load settings from .env
    env_width, env_height = None, None
    try:
        settings = load_settings()
        env_width = settings.lepton_width
        env_height = settings.lepton_height
        if env_width is not None and env_height is not None:
            print(f"Loaded config from .env: LEPTON_WIDTH={env_width}, LEPTON_HEIGHT={env_height}")
    except Exception as exc:
        print(f"Info: Could not load .env settings file: {exc}")

    # 1. Probe the camera over I2C CCI
    print("\n[1/2] Probing Lepton CCI (I2C bus 1, address 0x2A)...")
    
    # Perform raw diagnostics
    status_reg = read_raw_status(bus_number=1, address=0x2A)
    if status_reg is not None:
        print(f"  [Diag] Raw Status Register: 0x{status_reg:04X}")
        busy = bool(status_reg & 0x0001)
        boot_mode = bool(status_reg & 0x0002)
        boot_status = bool(status_reg & 0x0004)
        print(f"  [Diag] Busy Bit (0x0001): {busy}")
        print(f"  [Diag] Boot Success Bit (0x0004): {boot_status}")
    else:
        print(f"  [Diag] No response or error reading status register.")

    model = None
    try:
        model = probe_lepton(
            bus_number=1,
            address=0x2A,
            fallback_width=env_width,
            fallback_height=env_height
        )
        print(f"  SUCCESS: Detected {model.family}")
        print(f"  Details: Part Number = {model.part_number}")
        print(f"  Resolution: {model.width}x{model.height}")
    except Exception as exc:
        print(f"  WARNING: Could not fetch Lepton part number over I2C.")
        print(f"  Error details: {exc}")
        print("  We will attempt to test the SPI camera capture using fallback resolutions.")

    # Run SPI Raw Diagnostics
    debug_spi(spi_bus=0, spi_device=0)

    # 2. Capture a test image using SPI VoSPI
    print("\n[2/2] Attempting to capture a frame over SPI (SPI0 CE0)...")
    image_dir = Path("/data")
    if not image_dir.exists():
        print(f"  Info: /data does not exist, falling back to local directory.")
        image_dir = Path(".")
    
    # Determine resolutions to try
    test_resolutions = []
    if model is not None:
        test_resolutions = [(model.width, model.height, model.family)]
    elif env_width is not None and env_height is not None:
        # Prioritize the user's configured dimensions from .env
        test_resolutions = [(env_width, env_height, "Configured in .env")]
    else:
        # Fallback list if nothing is set
        test_resolutions = [
            (160, 120, "Lepton 3.x (Fallback)"),
            (80, 60, "Lepton 2.x (Fallback)")
        ]

    spi_success = False
    for width, height, name in test_resolutions:
        # Give the Lepton SPI interface time (CS high) to reset between attempts
        print(f"\n  Waiting 250ms for Lepton SPI interface to settle...")
        time.sleep(0.25)
        print(f"  Trying SPI capture with resolution {width}x{height} ({name})...")
        try:
            cam = PiIRCamera(
                image_dir=image_dir,
                spi_bus=0,
                spi_device=0,
                width=width,
                height=height
            )
            saved_path = cam.capture()
            print(f"  SUCCESS: Captured IR image using resolution {width}x{height}!")
            print(f"  Saved to container path: {saved_path.resolve()}")
            if image_dir == Path("/data"):
                print(f"  Saved to host directory: ./data/{saved_path.name}")
            else:
                print(f"  Saved to host directory: ./{saved_path.name}")
            spi_success = True
            break
        except Exception as exc:
            print(f"  FAILED with resolution {width}x{height}: {exc}")

    if not spi_success:
        print("\n  ERROR: SPI capture failed for all test resolutions.")
        print("\nSuggestions:")
        print("  - Check SPI wiring (MISO Pin 21, SCLK Pin 23, CS Pin 24).")
        print("  - Ensure SPI is enabled in /boot/firmware/config.txt (dtparam=spi=on).")
        print("  - Check if another process is holding the SPI device.")
        sys.exit(1)

    print("\n==================================================")
    print("            Test Completed Successfully           ")
    print("==================================================")

if __name__ == "__main__":
    main()
