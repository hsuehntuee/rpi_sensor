"""Standalone pure camera test script for Docker container on Raspberry Pi.

Run inside Docker on Raspberry Pi:
    docker compose run --build --rm edge-sensor python tests/test_camera_standalone.py
"""
import os
import sys
import subprocess
import shutil
from pathlib import Path


def test_rgb_camera() -> None:
    print("\n" + "=" * 60)
    print(" 1. DIAGNOSING V4L2 VIDEO DEVICES ")
    print("=" * 60)

    # List all V4L2 devices
    v4l2_ctl = shutil.which("v4l2-ctl")
    if v4l2_ctl:
        print("[*] Running v4l2-ctl --list-devices:")
        res = subprocess.run([v4l2_ctl, "--list-devices"], capture_output=True, text=True)
        print(res.stdout if res.stdout else res.stderr)
    else:
        print("[-] v4l2-ctl not found.")

    test_dir = Path("/data/test_output")
    test_dir.mkdir(parents=True, exist_ok=True)
    out_path = test_dir / "test_rgb.jpg"

    print("\n" + "=" * 60)
    print(" 2. PROBING V4L2 VIDEO NODES (/dev/video0 ~ /dev/video31) ")
    print("=" * 60)

    ffmpeg_cmd = shutil.which("ffmpeg")
    if not ffmpeg_cmd:
        print("[!] ffmpeg is not installed inside Docker.")
        return

    # Find existing /dev/video* devices
    dev_nodes = sorted([str(p) for p in Path("/dev").glob("video*")])
    print(f"[*] Detected video nodes in /dev: {dev_nodes}")

    formats = ["mjpeg", "yuyv422", "nv12", "h264", None]
    success = False

    for node in dev_nodes:
        for fmt in formats:
            if out_path.exists():
                out_path.unlink()

            cmd = [ffmpeg_cmd, "-y"]
            if fmt:
                cmd.extend(["-input_format", fmt])
            cmd.extend([
                "-f", "v4l2",
                "-i", node,
                "-vframes", "1",
                str(out_path),
            ])

            fmt_str = f"format={fmt}" if fmt else "default format"
            print(f"\n[*] Testing node: {node} ({fmt_str})")
            try:
                res = subprocess.run(cmd, capture_output=True, text=True, timeout=8)
                if out_path.exists():
                    size = out_path.stat().st_size
                    if size > 0:
                        print(f"    [SUCCESS] Captured valid image on {node} ({fmt_str})! Size: {size} bytes")
                        print(f"    Saved to: {out_path}")
                        success = True
                        break
                    else:
                        print(f"    [FAIL] {node} ({fmt_str}) generated 0-byte EMPTY file.")
                        out_path.unlink()
                else:
                    err_lines = [l for l in res.stderr.splitlines() if "Error" in l or "Invalid" in l or "failed" in l or "cannot" in l]
                    err_msg = err_lines[-1] if err_lines else "Failed to open device"
                    print(f"    [-] {node} ({fmt_str}): {err_msg}")
            except Exception as e:
                print(f"    [EXCEPT] {node} ({fmt_str}): {e}")

        if success:
            break

    if not success:
        print("\n[!] Could not capture RGB frame from any V4L2 node.")


def test_ir_camera() -> None:
    print("\n" + "=" * 60)
    print(" 3. TESTING FLIR LEPTON IR CAMERA CAPTURE ")
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
