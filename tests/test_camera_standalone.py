"""Standalone pure camera test script for Docker container on Raspberry Pi.

Run inside Docker on Raspberry Pi:
    docker compose run --rm edge-sensor python tests/test_camera_standalone.py
"""
import os
import sys
import subprocess
import shutil
from pathlib import Path


def test_rgb_camera() -> None:
    print("\n" + "=" * 60)
    print(" 1. TESTING RGB CAMERA CAPTURE ")
    print("=" * 60)

    test_dir = Path("/data/test_output")
    test_dir.mkdir(parents=True, exist_ok=True)
    out_path = test_dir / "test_rgb.jpg"
    if out_path.exists():
        out_path.unlink()

    cmds = [
        (
            "rpicam-still",
            [
                shutil.which("rpicam-still") or "rpicam-still",
                "--camera",
                "0",
                "--nopreview",
                "--timeout",
                "1000",
                "--output",
                str(out_path),
            ],
        ),
        (
            "libcamera-still",
            [
                shutil.which("libcamera-still") or "libcamera-still",
                "--camera",
                "0",
                "--nopreview",
                "--timeout",
                "1000",
                "--output",
                str(out_path),
            ],
        ),
        (
            "ffmpeg (/dev/video0)",
            [
                shutil.which("ffmpeg") or "ffmpeg",
                "-y",
                "-ss",
                "1",
                "-f",
                "v4l2",
                "-i",
                "/dev/video0",
                "-vframes",
                "1",
                str(out_path),
            ],
        ),
    ]

    success = False
    for name, cmd in cmds:
        if not cmd[0] or not shutil.which(cmd[0]):
            print(f"[-] {name} command ({cmd[0]}) not found in container PATH.")
            continue
        print(f"\n[*] Trying {name}: {' '.join(cmd)}")
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
            print(f"    Exit code: {res.returncode}")
            if res.stdout:
                print(f"    STDOUT:\n{res.stdout.strip()}")
            if res.stderr:
                print(f"    STDERR:\n{res.stderr.strip()}")

            if out_path.exists():
                size = out_path.stat().st_size
                print(f"    [+] Generated file: {out_path} ({size} bytes)")
                if size > 0:
                    print(f"    [SUCCESS] {name} captured a valid RGB image ({size} bytes)!")
                    success = True
                    break
                else:
                    print(f"    [FAIL] {name} created a 0-byte EMPTY file!")
                    out_path.unlink()
            else:
                print(f"    [FAIL] {name} did not generate any output file.")
        except Exception as e:
            print(f"    [EXCEPT] {name} failed with error: {e}")

    if not success:
        print("\n[!] ALL RGB Camera capture methods failed or produced 0-byte empty files.")


def test_ir_camera() -> None:
    print("\n" + "=" * 60)
    print(" 2. TESTING FLIR LEPTON IR CAMERA CAPTURE ")
    print("=" * 60)

    test_dir = Path("/data/test_output")
    test_dir.mkdir(parents=True, exist_ok=True)
    out_path = test_dir / "test_ir.jpg"
    if out_path.exists():
        out_path.unlink()

    try:
        from tests.verify_ir import (
            VoSPIReader,
            try_capture,
            render_fixed_range_frame,
            raw_to_celsius,
        )
        from PIL import Image

        print("[*] Opening SPI bus 0, device 0...")
        reader = VoSPIReader(bus=0, device=0)
        print("[*] Capturing frame from FLIR Lepton via VoSPI engine...")
        raw_frame = try_capture(reader, max_attempts=1500)
        reader.close()

        if raw_frame is None:
            print("[FAIL] VoSPI engine returned None frame.")
            return

        print(
            f"[+] Raw frame shape: {raw_frame.shape}, min raw: {raw_frame.min()}, max raw: {raw_frame.max()}"
        )
        clean_frame = raw_frame & 0x3FFF
        celsius = raw_to_celsius(clean_frame, is_tlinear=True)
        print(f"[+] Measured Temp range: {celsius.min():.2f}°C to {celsius.max():.2f}°C")

        rgb = render_fixed_range_frame(
            celsius, min_temp=18.0, max_temp=36.0, colormap="ironbow"
        )
        img = Image.fromarray(rgb, mode="RGB")
        img.save(out_path, format="JPEG", quality=90)

        if out_path.exists():
            size = out_path.stat().st_size
            print(f"    [+] Generated file: {out_path} ({size} bytes)")
            if size > 0:
                print(f"    [SUCCESS] FLIR Lepton captured a valid thermal IR image ({size} bytes)!")
            else:
                print(f"    [FAIL] FLIR Lepton created a 0-byte EMPTY file!")
    except Exception as e:
        print(f"[EXCEPT] FLIR Lepton test failed with error: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    test_rgb_camera()
    test_ir_camera()
