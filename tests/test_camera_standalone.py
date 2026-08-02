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
    print(" 1. TESTING OFFICIAL RASPBERRY PI CAMERA (rpicam-still / libcamera-still) ")
    print("=" * 60)

    test_dir = Path("/data/test_output")
    test_dir.mkdir(parents=True, exist_ok=True)
    out_path = test_dir / "test_rgb.jpg"
    if out_path.exists():
        out_path.unlink()

    # 1. Test rpicam-still / libcamera-still
    rpi_cmds = [
        ("rpicam-still", shutil.which("rpicam-still") or "/usr/bin/rpicam-still"),
        ("libcamera-still", shutil.which("libcamera-still") or "/usr/bin/libcamera-still"),
    ]

    success = False

    for name, cmd_path in rpi_cmds:
        print(f"\n[*] Checking {name} at: {cmd_path}")
        if not Path(cmd_path).exists():
            print(f"[-] {name} executable not found.")
            continue

        # List cameras first
        print(f"[*] Running {name} --list-cameras:")
        try:
            list_res = subprocess.run([cmd_path, "--list-cameras"], capture_output=True, text=True, timeout=5)
            print(list_res.stdout if list_res.stdout else list_res.stderr)
        except Exception as e:
            print(f"[-] List cameras failed: {e}")

        # Try capturing
        cmd = [
            cmd_path,
            "--camera", "0",
            "--nopreview",
            "--timeout", "1000",
            "--output", str(out_path),
        ]
        print(f"[*] Executing capture: {' '.join(cmd)}")
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
            print(f"    Exit code: {res.returncode}")
            if res.stdout:
                print(f"    STDOUT: {res.stdout.strip()}")
            if res.stderr:
                print(f"    STDERR: {res.stderr.strip()}")

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
                print(f"    [FAIL] {name} did not produce an output file.")
        except Exception as e:
            print(f"    [EXCEPT] {name} capture failed: {e}")

    if success:
        return

    print("\n" + "=" * 60)
    print(" 2. DIAGNOSING V4L2 VIDEO DEVICES ")
    print("=" * 60)

    v4l2_ctl = shutil.which("v4l2-ctl")
    if v4l2_ctl:
        print("[*] Running v4l2-ctl --list-devices:")
        res = subprocess.run([v4l2_ctl, "--list-devices"], capture_output=True, text=True)
        print(res.stdout if res.stdout else res.stderr)

    ffmpeg_cmd = shutil.which("ffmpeg")
    if not ffmpeg_cmd:
        print("[!] ffmpeg is not installed inside Docker.")
        return

    dev_nodes = sorted([str(p) for p in Path("/dev").glob("video*")])
    print(f"[*] Detected video nodes in /dev: {dev_nodes}")

    formats = ["mjpeg", "yuyv422", "nv12", None]

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
            try:
                res = subprocess.run(cmd, capture_output=True, text=True, timeout=8)
                if out_path.exists():
                    size = out_path.stat().st_size
                    if size > 0:
                        print(f"    [SUCCESS] Captured valid image on {node} ({fmt_str})! Size: {size} bytes")
                        success = True
                        break
                    else:
                        out_path.unlink()
            except Exception:
                pass
        if success:
            break

    if not success:
        print("\n[!] Could not capture RGB frame from any method.")


def test_ir_camera() -> None:
    print("\n" + "=" * 60)
    print(" 3. RUNNING GOLDEN SOURCE verify_ir.py FOR FLIR LEPTON ")
    print("=" * 60)

    try:
        from tests.verify_ir import main as verify_ir_main
        print("[*] Directly executing tests/verify_ir.py golden engine...")
        verify_ir_main()
    except Exception as e:
        print(f"[EXCEPT] FLIR Lepton verify_ir.py failed: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    test_rgb_camera()
    test_ir_camera()
