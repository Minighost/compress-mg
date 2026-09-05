#!/usr/bin/env python3
"""
Compress a video with HandBrakeCLI to hit a target file size.

Usage:
    python compress_to_size.py input.mp4 output.mp4 --size 7
    python compress_to_size.py input.mp4 output.mp4 --size 7 --margin 0.95
    python compress_to_size.py input.mp4 output.mp4 --size 7 --merge-audio
    python compress_to_size.py input.mp4 output.mp4 --size 7 --merge-audio --normalize-audio
    python compress_to_size.py input.mp4 output.mp4 --size 7 --open-dir
"""

import argparse
import os
import sys

from compressor import CompressionError, compress


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
    parser.add_argument(
        "--normalize-audio",
        action="store_true",
        help="Loudness-normalize each track before mixing and the mixed result "
        "after (only applies with --merge-audio)",
    )
    parser.add_argument(
        "--open-dir",
        action="store_true",
        help="Open the output file's folder in Explorer when done",
    )
    args = parser.parse_args()

    try:
        compress(
            args.input,
            args.output,
            args.size,
            args.margin,
            args.max_height,
            args.merge_audio,
            args.normalize_audio,
            on_status=print,
        )
    except CompressionError as e:
        sys.exit(str(e))

    if args.open_dir:
        os.startfile(os.path.dirname(os.path.abspath(args.output)))
