#!/usr/bin/env python3
"""
Launch LosslessCut, block until you close it, then auto-compress
whatever new video files it exported.

Usage:
    python cut_and_compress.py --losslesscut-dir "D:\Clips\cuts" --size 7
    python cut_and_compress.py --losslesscut-dir "D:\Clips\cuts" --input clip.mp4 --size 7
"""

import argparse
import os
import subprocess
import sys

LOSSLESSCUT_EXE = (
    r"C:\Program Files\LosslessCut\LosslessCut.exe"  # adjust to your install
)
VIDEO_EXTENSIONS = {".mp4", ".mkv", ".mov", ".avi", ".webm"}

COMPRESS_SCRIPT = os.path.join(os.path.dirname(__file__), "compress_to_size.py")


def snapshot(dir_path: str) -> set:
    return set(os.listdir(dir_path))


def compress(
    path: str, out_dir: str, target_mb: float, margin: float, max_height: int = None
):
    base, ext = os.path.splitext(os.path.basename(path))
    out_path = os.path.join(out_dir, f"{base}_compressed{ext}")
    print(f"Compressing {path} -> {out_path}")
    cmd = [
        sys.executable,
        COMPRESS_SCRIPT,
        path,
        out_path,
        "--size",
        str(target_mb),
        "--margin",
        str(margin),
    ]
    if max_height:
        cmd += ["--max-height", str(max_height)]
    subprocess.run(cmd, check=True)


def main(
    export_dir: str,
    input_file: str,
    target_mb: float,
    margin: float,
    max_height: int = None,
):
    before = snapshot(export_dir)

    cmd = [LOSSLESSCUT_EXE]
    if input_file:
        cmd.append(input_file)

    print("Launching LosslessCut... close it when you're done cutting.")
    subprocess.run(cmd)  # blocks here until the window is closed

    new_files = [
        f
        for f in (snapshot(export_dir) - before)
        if os.path.splitext(f)[1].lower() in VIDEO_EXTENSIONS
    ]

    if not new_files:
        print("No new exports found — nothing to compress.")
        return

    out_dir = os.path.join(export_dir, "compressed")
    os.makedirs(out_dir, exist_ok=True)

    for name in new_files:
        compress(os.path.join(export_dir, name), out_dir, target_mb, margin, max_height)

    print(f"\nDone. {len(new_files)} file(s) compressed into {out_dir}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Wrap LosslessCut and auto-compress its output on exit."
    )
    parser.add_argument(
        "--losslesscut-dir",
        required=True,
        help="The export/output folder you've set inside LosslessCut's settings",
    )
    parser.add_argument(
        "--input", default=None, help="Optional video to open directly in LosslessCut"
    )
    parser.add_argument(
        "--size", type=float, default=7.0, help="Target size in MB (default: 7)"
    )
    parser.add_argument(
        "--margin",
        type=float,
        default=0.99,
        help="Safety margin multiplier (default: 0.99)",
    )
    parser.add_argument(
        "--max-height",
        type=int,
        default=None,
        help="Cap output height in pixels (e.g. 720), passed through to compress_to_size.py",
    )
    args = parser.parse_args()

    main(args.losslesscut_dir, args.input, args.size, args.margin, args.max_height)
