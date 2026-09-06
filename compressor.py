import os
import re
import signal
import subprocess
import threading
import time
from typing import Optional

HANDBRAKE_CLI = "HandBrakeCLI"  # change to full path if it's not on your PATH,
# e.g. r"C:\Program Files\HandBrake\HandBrakeCLI.exe"
FFMPEG = "ffmpeg"  # change to full path if it's not on your PATH

AUDIO_BITRATE_KBPS = 128

# This is the amount of time (in seconds) before sending a SIGKILL to a running
# subprocess after a "Ctrl+C" is sent.
CANCEL_GRACE_PERIOD_S = 2.0


class CompressionError(Exception):
    """Raised for expected failure conditions (bad duration, size too small, etc.)."""


class CompressionCancelled(Exception):
    """Raised when a CancelToken aborts an in-progress compression."""


class CancelToken:
    """Lets a GUI thread cooperatively cancel a compression running on a worker
    thread, including killing whatever subprocess is currently active."""

    def __init__(self):
        self._lock = threading.Lock()
        self.cancelled = False
        self._process: Optional[subprocess.Popen] = None

    def register(self, process: subprocess.Popen):
        with self._lock:
            self._process = process
            already_cancelled = self.cancelled
        if already_cancelled:
            self._terminate(process)

    def unregister(self):
        with self._lock:
            self._process = None

    def cancel(self):
        with self._lock:
            self.cancelled = True
            process = self._process
        if process is not None:
            self._terminate(process)

    def _terminate(self, process: subprocess.Popen):
        try:
            process.send_signal(signal.CTRL_BREAK_EVENT)
        except (OSError, ValueError):
            # No console attached (e.g. launched via pythonw) — nothing to
            # signal gracefully, just force it.
            self._escalate(process)
            return
        timer = threading.Timer(CANCEL_GRACE_PERIOD_S, self._escalate, args=(process,))
        timer.daemon = True
        timer.start()

    def _escalate(self, process: subprocess.Popen):
        try:
            if process.poll() is None:
                process.kill()
        except OSError:
            pass


def _safe_remove(path: str, attempts: int = 3, delay: float = 0.2):
    """Best-effort delete — a just-killed process may not have released its
    file handle on Windows yet, so tolerate a few failed attempts."""
    for i in range(attempts):
        try:
            os.remove(path)
            return
        except FileNotFoundError:
            return
        except OSError:
            if i == attempts - 1:
                return
            time.sleep(delay)


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


_PROGRESS_RE = re.compile(r"Encoding:.*?(\d+(?:\.\d+)?)\s*%")


def _run_cancellable(cmd, cancel_token: Optional[CancelToken], on_progress=None):
    """Run cmd, optionally reporting progress and/or supporting cancellation
    via cancel_token. Raises CompressionCancelled if the token fires,
    CalledProcessError on a non-zero exit."""
    if on_progress is None and cancel_token is None:
        subprocess.run(cmd, check=True)
        return

    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if cancel_token else 0
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        creationflags=creationflags,
    )
    if cancel_token:
        cancel_token.register(process)
    try:
        for line in process.stdout:
            if on_progress:
                match = _PROGRESS_RE.search(line)
                if match:
                    on_progress(float(match.group(1)))
        returncode = process.wait()
    finally:
        if cancel_token:
            cancel_token.unregister()

    if cancel_token and cancel_token.cancelled:
        raise CompressionCancelled()
    if returncode != 0:
        raise subprocess.CalledProcessError(returncode, cmd)


def merge_audio_tracks(
    input_path: str, normalize: bool = False, cancel_token: Optional[CancelToken] = None
) -> str:
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

    cmd = [
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
    ]

    try:
        _run_cancellable(cmd, cancel_token)
    except Exception:
        _safe_remove(mixed_path)
        raise
    return mixed_path


def run_handbrake(cmd, on_progress=None, cancel_token: Optional[CancelToken] = None):
    """Run a HandBrakeCLI command, optionally reporting progress (0-100)
    via on_progress(percent) as it streams stdout/stderr, and optionally
    killable mid-run via cancel_token."""
    _run_cancellable(cmd, cancel_token, on_progress)


def compress(
    input_path: str,
    output_path: str,
    target_mb: float,
    margin: float,
    max_height: int = None,
    framerate: Optional[str] = None,
    merge_audio: bool = False,
    normalize_audio: bool = False,
    on_progress=None,
    on_status=None,
    cancel_token: Optional[CancelToken] = None,
):
    """Compress input_path to output_path targeting target_mb.

    on_status(str) is called with human-readable stage updates.
    on_progress(float) is called with HandBrake's 0-100 encode percentage.
    """

    def status(msg):
        if on_status:
            on_status(msg)

    encode_input = input_path
    merged_created = False
    try:
        if merge_audio:
            status("Merging audio tracks...")
            encode_input = merge_audio_tracks(input_path, normalize_audio, cancel_token)
            merged_created = encode_input != input_path

        status("Scanning duration...")
        duration = get_duration_seconds(encode_input)
        video_kbps = calculate_video_bitrate_kbps(duration, target_mb, margin)

        if cancel_token and cancel_token.cancelled:
            raise CompressionCancelled()

        status(
            f"Duration: {duration:.1f}s | Target: {target_mb}MB | Margin: {margin} | "
            f"Video bitrate: {video_kbps} kbps | Audio: {AUDIO_BITRATE_KBPS} kbps"
            + (f" | Max height: {max_height}p" if max_height else "")
            + (f" | Framerate: {framerate}" if framerate else "")
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
        if framerate:
            cmd += ["--rate", str(framerate), "--pfr"]

        status("Encoding...")
        try:
            run_handbrake(cmd, on_progress, cancel_token)
        except CompressionCancelled:
            _safe_remove(output_path)
            raise
    finally:
        if merged_created:
            _safe_remove(encode_input)

    output_mb = os.path.getsize(output_path) / (1024 * 1024)
    status(f"Done ({output_mb:.2f} MB)")
    return output_mb
