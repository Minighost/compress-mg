"""
Core video-compression logic (HandBrakeCLI + ffmpeg), extracted from
compress_to_size.py so it can be imported by both the CLI and the GUI.
"""

import os
import re
import subprocess
import sys

HANDBRAKE_CLI = "HandBrakeCLI"  # change to full path if it's not on your PATH,
# e.g. r"C:\Program Files\HandBrake\HandBrakeCLI.exe"
FFMPEG = "ffmpeg"  # change to full path if it's not on your PATH

AUDIO_BITRATE_KBPS = 128  # what we'll encode audio at


class CompressionError(Exception):
    """Raised for expected failure conditions (bad duration, size too small, etc.)."""


def get_duration_seconds(input_path: str) -> float:
    """Scan the file with HandBrake and pull the duration out of its output."""
    result = subprocess.run(
        [HANDBRAKE_CLI, "-i", input_path, "--scan"], capture_output=True, text=True
    )
    combined = result.stdout + result.stderr
    match = re.search(r"duration:\s*(\d+):(\d+):(\d+)", combined, re.IGNORECASE)
    if not match:
        raise CompressionError(
            "Couldn't determine video duration from HandBrake's scan output."
        )
    hours, minutes, seconds = map(int, match.groups())
    return hours * 3600 + minutes * 60 + seconds


def calculate_video_bitrate_kbps(
    duration_s: float, target_mb: float, margin: float
) -> int:
    target_bits = target_mb * 8 * 1024 * 1024 * margin
    total_kbps = target_bits / duration_s / 1000
    video_kbps = int(total_kbps - AUDIO_BITRATE_KBPS)
    if video_kbps < 100:
        raise CompressionError(
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


def merge_audio_tracks(input_path: str, normalize: bool = False) -> str:
    """Mix all audio tracks in the file down to one. Returns the original
    path unchanged if there's nothing to merge (0 or 1 audio streams)."""
    n = count_audio_streams(input_path)
    if n < 2:
        return input_path

    base, ext = os.path.splitext(input_path)
    mixed_path = f"{base}_mixed{ext}"

    if normalize:
        # loudnorm each track before mixing (so one loud track doesn't drown
        # out another), then loudnorm the summed output (mixing raises the
        # combined level again since the waveforms add together).
        pre = "".join(f"[0:a:{i}]loudnorm[n{i}];" for i in range(n))
        inputs = "".join(f"[n{i}]" for i in range(n))
        filter_complex = f"{pre}{inputs}amix=inputs={n}:duration=longest,loudnorm[aout]"
    else:
        inputs = "".join(f"[0:a:{i}]" for i in range(n))
        filter_complex = f"{inputs}amix=inputs={n}:duration=longest[aout]"

    subprocess.run(
        [
            FFMPEG,
            "-y",
            "-i",
            input_path,
            "-filter_complex",
            filter_complex,
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


_PROGRESS_RE = re.compile(r"Encoding:.*?(\d+(?:\.\d+)?)\s*%")


def run_handbrake(cmd, on_progress=None):
    """Run a HandBrakeCLI command, optionally reporting progress (0-100)
    via on_progress(percent) as it streams stdout/stderr."""
    if on_progress is None:
        subprocess.run(cmd, check=True)
        return

    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    for line in process.stdout:
        match = _PROGRESS_RE.search(line)
        if match:
            on_progress(float(match.group(1)))
    returncode = process.wait()
    if returncode != 0:
        raise subprocess.CalledProcessError(returncode, cmd)


def compress(
    input_path: str,
    output_path: str,
    target_mb: float,
    margin: float,
    max_height: int = None,
    merge_audio: bool = False,
    normalize_audio: bool = False,
    on_progress=None,
    on_status=None,
):
    """Compress input_path to output_path targeting target_mb.

    on_status(str) is called with human-readable stage updates.
    on_progress(float) is called with HandBrake's 0-100 encode percentage.
    """

    def status(msg):
        if on_status:
            on_status(msg)

    encode_input = input_path
    if merge_audio:
        status("Merging audio tracks...")
        encode_input = merge_audio_tracks(input_path, normalize_audio)

    status("Scanning duration...")
    duration = get_duration_seconds(encode_input)
    video_kbps = calculate_video_bitrate_kbps(duration, target_mb, margin)

    status(
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

    status("Encoding...")
    run_handbrake(cmd, on_progress)

    if encode_input != input_path:
        os.remove(encode_input)  # drop the temp merged-audio file

    output_mb = os.path.getsize(output_path) / (1024 * 1024)
    status(f"Done ({output_mb:.2f} MB)")
    return output_mb
