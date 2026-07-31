import sys
from pathlib import Path

# Add project root to sys.path so we can import src
sys.path.append(str(Path(__file__).parent.parent))

try:
    from src.sensors.camera_ir import probe_lepton, PiIRCamera
except ImportError as err:
    print(f"Error importing modules: {err}")
    print("Please make sure you are running this from the project root directory.")
    sys.exit(1)

def main() -> None:
    print("==================================================")
    print("      FLIR Lepton IR Camera Test Script           ")
    print("==================================================")
    
    # 1. Probe the camera over I2C CCI
    print("\n[1/2] Probing Lepton CCI (I2C bus 1, address 0x2A)...")
    try:
        model = probe_lepton(bus_number=1, address=0x2A)
        print(f"  SUCCESS: Detected {model.family}")
        print(f"  Details: Part Number = {model.part_number}")
        print(f"  Resolution: {model.width}x{model.height}")
    except Exception as exc:
        print(f"  FAILED: Could not communicate with Lepton over I2C.")
        print(f"  Error details: {exc}")
        print("\nSuggestions:")
        print("  - Check your physical connections (SDA1/SCL1 pins 3 and 5).")
        print("  - Ensure I2C is enabled on your Raspberry Pi.")
        sys.exit(1)

    # 2. Capture a test image using SPI VoSPI
    print("\n[2/2] Attempting to capture a frame over SPI (SPI0 CE0)...")
    image_dir = Path("/data")
    if not image_dir.exists():
        print(f"  Info: /data does not exist, falling back to local directory.")
        image_dir = Path(".")
    
    try:
        cam = PiIRCamera(
            image_dir=image_dir,
            spi_bus=0,
            spi_device=0,
            width=model.width,
            height=model.height
        )
        saved_path = cam.capture()
        print(f"  SUCCESS: Captured IR image!")
        print(f"  Saved to container path: {saved_path.resolve()}")
        # Check if we are running in docker by looking for /data mount
        if image_dir == Path("/data"):
            print(f"  Saved to host directory: ./data/{saved_path.name}")
        else:
            print(f"  Saved to host directory: ./{saved_path.name}")
    except Exception as exc:
        print(f"  FAILED: Could not capture image over SPI.")
        print(f"  Error details: {exc}")
        print("\nSuggestions:")
        print("  - Check SPI wiring (MISO Pin 21, SCLK Pin 23, CS Pin 24).")
        print("  - Ensure SPI is enabled in /boot/firmware/config.txt.")
        sys.exit(1)

    print("\n==================================================")
    print("            Test Completed Successfully           ")
    print("==================================================")

if __name__ == "__main__":
    main()
