"""Audio download + Whisper transcription for the YouTube extractor's
no-captions fallback path.

Two-step flow used by `extract_youtube` when transcript-api returns
nothing useful:

1. yt-dlp downloads just the audio track as MP3 (smaller than the full
   MP4, fits in Whisper's 25MB upload limit for most videos under
   ~30 min). Routes through AMPERSTAND_HTTP_PROXY when set so the cloud
   IP block doesn't bite.
2. The MP3 goes to OpenAI's Whisper API which returns plain-text
   transcript. ~$0.006/min — gated behind AMPERSTAND_YOUTUBE_AUDIO_FALLBACK
   so it can't surprise-spend.

The temp file is removed after transcription whether or not it succeeds.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

# Invoke yt-dlp via `python -m yt_dlp` so it picks up whatever venv we're
# in — the systemd service unit doesn't put /opt/amperstand/venv/bin on
# PATH and the bare `yt-dlp` binary isn't found.
_YT_DLP = [sys.executable, "-m", "yt_dlp"]

logger = logging.getLogger(__name__)

# 25 MB — Whisper API hard limit. A typical mp3 at 96 kbps fits ~30 min.
WHISPER_MAX_BYTES = 25 * 1024 * 1024

# Cap audio downloads at this many seconds by default. Overridable via
# AMPERSTAND_YOUTUBE_AUDIO_MAX_SEC. Prevents a 3-hour podcast surprise.
DEFAULT_MAX_DURATION_SEC = 3600


class AudioError(RuntimeError):
    pass


@dataclass(frozen=True)
class AudioFallbackResult:
    text: str
    duration_seconds: int | None
    audio_bytes: int


def youtube_audio_fallback_enabled() -> bool:
    """Reads AMPERSTAND_YOUTUBE_AUDIO_FALLBACK. Off unless explicitly truthy."""
    return os.environ.get("AMPERSTAND_YOUTUBE_AUDIO_FALLBACK", "").strip().lower() in {
        "1", "true", "yes", "on",
    }


def youtube_audio_max_seconds() -> int:
    raw = os.environ.get("AMPERSTAND_YOUTUBE_AUDIO_MAX_SEC", "").strip()
    try:
        return int(raw) if raw else DEFAULT_MAX_DURATION_SEC
    except ValueError:
        return DEFAULT_MAX_DURATION_SEC


def get_youtube_duration(url: str, *, proxy: str | None = None) -> int | None:
    """Return the video's duration in seconds, or None if we can't fetch it."""
    cmd = [
        *_YT_DLP, "--no-playlist", "--skip-download",
        "--print", "%(duration)s",
    ]
    if proxy:
        cmd += ["--proxy", proxy]
    cmd.append(url)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=45)
        if r.returncode != 0:
            logger.warning("yt-dlp duration probe failed: %s", r.stderr.strip()[:200])
            return None
        out = r.stdout.strip().splitlines()
        if not out:
            return None
        return int(out[0])
    except (subprocess.TimeoutExpired, ValueError):
        return None


def download_youtube_audio(
    url: str, *, proxy: str | None = None, dest_dir: Path | None = None
) -> Path:
    """Download the best-quality audio track of a YouTube video as MP3.

    Returns the path to the file. The directory is the caller's
    responsibility to clean up — `transcribe_youtube` does this for you
    when used end-to-end.
    """
    if dest_dir is None:
        dest_dir = Path(tempfile.mkdtemp(prefix="amp-yt-audio-"))
    dest_dir.mkdir(parents=True, exist_ok=True)

    out_template = str(dest_dir / "%(id)s.%(ext)s")
    # No --extract-audio / --audio-format mp3 here — that requires ffmpeg
    # on PATH which the droplet doesn't have. We download whichever audio
    # YouTube serves directly (typically m4a/AAC, ~1MB/min). Whisper
    # accepts m4a, webm, mp3, mp4, mpeg, mpga, wav so no conversion is
    # needed. The filesize<25M selector keeps us under Whisper's upload
    # limit; falls through to any bestaudio if no constrained option
    # exists (then we'll fail loudly in the size check).
    cmd = [
        *_YT_DLP,
        "-f", "bestaudio[filesize<25M]/bestaudio[ext=m4a]/bestaudio",
        "-o", out_template,
        "--no-playlist",
        "--no-warnings",
    ]
    if proxy:
        cmd += ["--proxy", proxy]
    cmd.append(url)

    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired as exc:
        raise AudioError(f"yt-dlp timed out for {url}") from exc

    if r.returncode != 0:
        raise AudioError(f"yt-dlp failed (rc={r.returncode}): {r.stderr.strip()[:500]}")

    audio_exts = {".m4a", ".webm", ".mp3", ".mp4", ".mpga", ".mpeg", ".opus", ".ogg", ".wav"}
    files = [
        p for p in dest_dir.iterdir()
        if p.is_file() and p.suffix.lower() in audio_exts
    ]
    if not files:
        raise AudioError(
            f"yt-dlp produced no audio file for {url}; stderr={r.stderr.strip()[:200]}"
        )
    return files[0]


def transcribe_audio_file(path: Path, *, api_key: str | None = None, model: str = "whisper-1") -> str:
    """Transcribe an audio file via OpenAI Whisper. Returns plain text.

    Whisper auto-detects language. For ~$0.006/min of audio.
    """
    if not path.exists():
        raise AudioError(f"audio file not found: {path}")
    size = path.stat().st_size
    if size > WHISPER_MAX_BYTES:
        raise AudioError(
            f"audio file is {size} bytes; Whisper max is {WHISPER_MAX_BYTES}"
        )
    api_key = api_key or os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise AudioError("OPENAI_API_KEY not set; cannot transcribe via Whisper")

    try:
        from openai import OpenAI
    except ImportError as exc:
        raise AudioError("openai package not installed") from exc

    client = OpenAI(api_key=api_key)
    with open(path, "rb") as f:
        resp = client.audio.transcriptions.create(model=model, file=f)
    return resp.text


def transcribe_youtube(
    url: str, *, proxy: str | None = None, max_duration_sec: int | None = None
) -> AudioFallbackResult:
    """End-to-end: probe duration → download audio → transcribe.

    Cleans up the temp dir on success or failure. Raises AudioError on
    any step.
    """
    cap = max_duration_sec if max_duration_sec is not None else youtube_audio_max_seconds()
    duration = get_youtube_duration(url, proxy=proxy)
    if duration is not None and duration > cap:
        raise AudioError(
            f"video duration {duration}s exceeds cap {cap}s; "
            f"set AMPERSTAND_YOUTUBE_AUDIO_MAX_SEC to override"
        )

    work_dir = Path(tempfile.mkdtemp(prefix="amp-yt-audio-"))
    try:
        audio_path = download_youtube_audio(url, proxy=proxy, dest_dir=work_dir)
        size = audio_path.stat().st_size
        if size > WHISPER_MAX_BYTES:
            raise AudioError(
                f"audio is {size//1024//1024}MB; Whisper limit is 25MB. "
                f"Lower bitrate or chunk the file (not yet supported)."
            )
        text = transcribe_audio_file(audio_path)
        return AudioFallbackResult(text=text, duration_seconds=duration, audio_bytes=size)
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
