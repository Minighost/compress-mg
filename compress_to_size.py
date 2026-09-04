#!/usr/bin/env python3
"""
Compress a video with HandBrakeCLI to hit a target file size.

Usage:
    python compress_to_size.py input.mp4 output.mp4 --size 7
    python compress_to_size.py input.mp4 output.mp4 --size 7 --margin 0.95
    python compress_to_size.py input.mp4 output.mp4 --size 7 --merge-audio
"""

import argparse
import os
import re
import subprocess
import sys

HANDBRAKE_CLI = "HandBrakeCLI"  # change to full path if it's not on your PATH,
# e.g. r"C:\Program Files\HandBrake\HandBrakeCLI.exe"
FFMPEG = "ffmpeg"  # change to full path if it's not on your PATH

AUDIO_BITRATE_KBPS = 128  # what we'll encode audio at


def get_duration_seconds(input_path: str) -> float:
    """Scan the file with HandBrake and pull the duration out of its output."""
    result = subprocess.run(
        [HANDBRAKE_CLI, "-i", input_path, "--scan"], capture_output=True, text=True
    )
    combined = result.stdout + result.stderr
    match = re.search(r"duration:\s*(\d+):(\d+):(\d+)", combined, re.IGNORECASE)
    if not match:
        sys.exit("Couldn't determine video duration from HandBrake's scan output.")
    hours, minutes, seconds = map(int, match.groups())
    return hours * 3600 + minutes * 60 + seconds


def calculate_video_bitrate_kbps(
    duration_s: float, target_mb: float, margin: float
) -> int:
    target_bits = target_mb * 8 * 1024 * 1024 * margin
    total_kbps = target_bits / duration_s / 1000
    video_kbps = int(total_kbps - AUDIO_BITRATE_KBPS)
    if video_kbps < 100:
        sys.exit(
            f"Target size too small for this duration — computed video "
            f"bitrate ({video_kbps} kbps) is unreasonably low."
        )
    return video_kbps


def count_audio_streams(input_path: str) -> int:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "a",
            "-show_entries",
            "stream=index",
            "-of",
            "csv=p=0",
            input_path,
        ],
        capture_output=True,
        text=True,
    )
    return len([line for line in result.stdout.strip().splitlines() if line])


def merge_audio_tracks(input_path: str) -> str:
    """Mix all audio tracks in the file down to one. Returns the original
    path unchanged if there's nothing to merge (0 or 1 audio streams)."""
    n = count_audio_streams(input_path)
    if n < 2:
        return input_path

    base, ext = os.path.splitext(input_path)
    mixed_path = f"{base}_mixed{ext}"
    inputs = "".join(f"[0:a:{i}]" for i in range(n))

    print(f"Merging {n} audio tracks -> {mixed_path}")
    subprocess.run(
        [
            FFMPEG,
            "-y",
            "-i",
            input_path,
            "-filter_complex",
            f"{inputs}amix=inputs={n}:duration=longest[aout]",
            "-map",
            "0:v",
            "-map",
            "[aout]",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            mixed_path,
        ],
        check=True,
    )
    return mixed_path


def compress(
    input_path: str,
    output_path: str,
    target_mb: float,
    margin: float,
    max_height: int = None,
    merge_audio: bool = False,
):
    encode_input = merge_audio_tracks(input_path) if merge_audio else input_path

    duration = get_duration_seconds(encode_input)
    video_kbps = calculate_video_bitrate_kbps(duration, target_mb, margin)

    print(
        f"Duration: {duration:.1f}s | Target: {target_mb}MB | Margin: {margin} | "
        f"Video bitrate: {video_kbps} kbps | Audio: {AUDIO_BITRATE_KBPS} kbps"
        + (f" | Max height: {max_height}p" if max_height else "")
    )

    cmd = [
        HANDBRAKE_CLI,
        "-i",
        encode_input,
        "-o",
        output_path,
        "--encoder",
        "x264",
        "--vb",
        str(video_kbps),
        "--multi-pass",
        "--turbo",
        "--aencoder",
        "av_aac",
        "--ab",
        str(AUDIO_BITRATE_KBPS),
    ]
    if max_height:
        cmd += ["--maxHeight", str(max_height)]

    subprocess.run(cmd, check=True)

    if encode_input != input_path:
        os.remove(encode_input)  # drop the temp merged-audio file

    print(f"\nDone. Output written to {output_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Compress a video to a target file size with HandBrake."
    )
    parser.add_argument("input", help="Path to input video")
    parser.add_argument("output", help="Path to output video")
    parser.add_argument(
        "--size", type=float, default=7.0, help="Target size in MB (default: 7)"
    )
    parser.add_argument(
        "--margin",
        type=float,
        default=0.99,
        help="Safety margin multiplier for container overhead (default: 0.99)",
    )
    parser.add_argument(
        "--max-height",
        type=int,
        default=None,
        help="Cap output height in pixels (e.g. 720). Only downscales; aspect ratio preserved.",
    )
    parser.add_argument(
        "--merge-audio",
        action="store_true",
        help="Mix all audio tracks into one before encoding (requires ffmpeg on PATH)",
    )
    args = parser.parse_args()

    compress(
        args.input,
        args.output,
        args.size,
        args.margin,
        args.max_height,
        args.merge_audio,
    )
