import os
import signal
import subprocess
import threading
import time
from collections import deque
from typing import Callable, Optional

FFMPEG = "ffmpeg"  # change to full path if it's not on your PATH
FFPROBE = "ffprobe"  # change to full path if it's not on your PATH

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
    """Look up the file's duration via ffprobe."""
    result = subprocess.run(
        [
            FFPROBE,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "csv=p=0",
            input_path,
        ],
        capture_output=True,
        text=True,
    )
    output = result.stdout.strip()
    try:
        duration = float(output)
    except ValueError:
        raise CompressionError(
            f"Couldn't determine video duration from ffprobe output "
            f"({output!r}). ffprobe stderr: {result.stderr.strip()}"
        )
    if duration <= 0:
        raise CompressionError("ffprobe reported a non-positive duration.")
    return duration


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
            FFPROBE,
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


def _parse_ffmpeg_timestamp(ts: str) -> float:
    hours, minutes, seconds = ts.split(":")
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def _parse_ffmpeg_progress_stream(stdout, on_progress_seconds: Callable[[float], None]):
    """Read ffmpeg's `-progress pipe:1` key=value stanzas and call
    on_progress_seconds(elapsed_seconds) once per stanza."""
    stanza: dict = {}
    for line in stdout:
        line = line.strip()
        if not line or "=" not in line:
            continue
        key, _, value = line.partition("=")
        stanza[key] = value
        if key != "progress":
            continue

        elapsed = None
        out_time = stanza.get("out_time")
        out_time_ms = stanza.get("out_time_ms")
        if out_time and out_time != "N/A":
            try:
                elapsed = _parse_ffmpeg_timestamp(out_time)
            except ValueError:
                pass
        elif out_time_ms and out_time_ms != "N/A":
            try:
                elapsed = int(out_time_ms) / 1_000_000  # microseconds, despite the name
            except ValueError:
                pass
        if elapsed is not None:
            on_progress_seconds(elapsed)
        stanza = {}


def _run_ffmpeg(
    cmd,
    cancel_token: Optional[CancelToken] = None,
    on_progress_seconds: Optional[Callable[[float], None]] = None,
):
    """Run an ffmpeg command. stdout and stderr are kept separate: stdout is
    only piped when on_progress_seconds is given (ffmpeg's `-progress pipe:1`
    output), while stderr (ffmpeg's normal human log) is always drained on a
    background thread into a bounded buffer for error diagnostics only.
    Raises CompressionCancelled if cancel_token fires, CompressionError
    (including a tail of stderr) on a non-zero exit."""
    stdout_target = subprocess.PIPE if on_progress_seconds else subprocess.DEVNULL
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if cancel_token else 0

    process = subprocess.Popen(
        cmd,
        stdout=stdout_target,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        creationflags=creationflags,
    )

    stderr_lines = deque(maxlen=40)

    def _drain_stderr():
        for line in process.stderr:
            stderr_lines.append(line.rstrip())

    stderr_thread = threading.Thread(target=_drain_stderr, daemon=True)
    stderr_thread.start()

    if cancel_token:
        cancel_token.register(process)
    try:
        if on_progress_seconds:
            _parse_ffmpeg_progress_stream(process.stdout, on_progress_seconds)
        returncode = process.wait()
    finally:
        if cancel_token:
            cancel_token.unregister()
        stderr_thread.join(timeout=2.0)

    if cancel_token and cancel_token.cancelled:
        raise CompressionCancelled()
    if returncode != 0:
        tail = "\n".join(stderr_lines)
        raise CompressionError(f"ffmpeg exited with code {returncode}:\n{tail}")


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
        _run_ffmpeg(cmd, cancel_token)
    except Exception:
        _safe_remove(mixed_path)
        raise
    return mixed_path


def _build_scale_filter(max_height: Optional[int]) -> Optional[str]:
    if not max_height:
        return None
    # Single quotes here are ffmpeg's own filtergraph escaping (needed
    # because the "," inside min(ih,N) would otherwise be parsed as a
    # filter separator) — not shell quoting, so they stay literal even
    # though Popen(list) bypasses the shell.
    return f"scale=-2:'min(ih,{max_height})'"


def _make_passlog_prefix(output_path: str) -> str:
    directory = os.path.dirname(output_path) or "."
    base = os.path.splitext(os.path.basename(output_path))[0]
    return os.path.join(directory, f".{base}_2pass")


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
    on_progress(float) is called with the overall 0-100 encode percentage.
    """

    PASS1_WEIGHT = 15  # percent of the bar allotted to pass 1 (analysis)

    def status(msg):
        if on_status:
            on_status(msg)

    encode_input = input_path
    merged_created = False
    passlog_prefix = None
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

        def progress_for(pass_start, pass_span):
            def _cb(elapsed_s):
                if on_progress:
                    pct = pass_start + min(elapsed_s / duration, 1.0) * pass_span
                    on_progress(min(pct, pass_start + pass_span))

            return _cb

        vf_string = _build_scale_filter(max_height)
        passlog_prefix = _make_passlog_prefix(output_path)

        pass1_cmd = [
            FFMPEG,
            "-y",
            "-progress",
            "pipe:1",
            "-nostats",
            "-i",
            encode_input,
            "-c:v",
            "libx264",
            "-b:v",
            f"{video_kbps}k",
            "-preset",
            "medium",
        ]
        if vf_string:
            pass1_cmd += ["-vf", vf_string]
        if framerate:
            pass1_cmd += ["-fpsmax", str(framerate)]
        pass1_cmd += [
            "-pass",
            "1",
            "-passlogfile",
            passlog_prefix,
            "-an",
            "-f",
            "mp4",
            os.devnull,
        ]

        pass2_cmd = [
            FFMPEG,
            "-y",
            "-progress",
            "pipe:1",
            "-nostats",
            "-i",
            encode_input,
            "-c:v",
            "libx264",
            "-b:v",
            f"{video_kbps}k",
            "-preset",
            "medium",
        ]
        if vf_string:
            pass2_cmd += ["-vf", vf_string]
        if framerate:
            pass2_cmd += ["-fpsmax", str(framerate)]
        pass2_cmd += [
            "-pass",
            "2",
            "-passlogfile",
            passlog_prefix,
            "-c:a",
            "aac",
            "-b:a",
            f"{AUDIO_BITRATE_KBPS}k",
            output_path,
        ]

        try:
            status("Encoding (pass 1/2)...")
            _run_ffmpeg(pass1_cmd, cancel_token, progress_for(0, PASS1_WEIGHT))

            if cancel_token and cancel_token.cancelled:
                raise CompressionCancelled()

            status("Encoding (pass 2/2)...")
            _run_ffmpeg(
                pass2_cmd, cancel_token, progress_for(PASS1_WEIGHT, 100 - PASS1_WEIGHT)
            )

            if on_progress:
                on_progress(100.0)
        except CompressionCancelled:
            _safe_remove(output_path)
            raise
    finally:
        if merged_created:
            _safe_remove(encode_input)
        if passlog_prefix:
            _safe_remove(f"{passlog_prefix}-0.log")
            _safe_remove(f"{passlog_prefix}-0.log.mbtree")

    output_mb = os.path.getsize(output_path) / (1024 * 1024)
    status(f"Done ({output_mb:.2f} MB)")
    return output_mb
