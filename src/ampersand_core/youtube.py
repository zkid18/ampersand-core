"""YouTube transcript and metadata extraction using yt-dlp."""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

from ampersand_core.models import CapturedContent, ContentType


def extract_youtube(url: str) -> CapturedContent:
    """Extract transcript and metadata from a YouTube video."""
    # Get video metadata
    result = subprocess.run(
        [
            "yt-dlp",
            "--dump-json",
            "--no-download",
            "--skip-download",
            url,
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise ValueError(f"yt-dlp failed: {result.stderr}")

    info = json.loads(result.stdout)
    title = info.get("title", "Untitled Video")
    channel = info.get("channel", info.get("uploader", "Unknown"))
    duration = info.get("duration", 0)

    # Try to get subtitles/transcript
    transcript = _get_transcript(url, info)

    # Build content
    duration_str = _format_duration(duration)
    lines = [
        f"**Channel**: {channel}",
        f"**Duration**: {duration_str}",
        "",
    ]

    if transcript:
        lines.append("## Transcript")
        lines.append("")
        lines.append(transcript)
    else:
        description = info.get("description", "")
        if description:
            lines.append("## Description")
            lines.append("")
            lines.append(description)
        else:
            lines.append("*No transcript or description available.*")

    return CapturedContent(
        url=url,
        title=title,
        content_markdown="\n".join(lines),
        content_type=ContentType.VIDEO,
        author=channel,
    )


def _get_transcript(url: str, info: dict) -> str | None:
    """Try to extract subtitles from the video."""
    with tempfile.TemporaryDirectory() as tmpdir:
        sub_path = Path(tmpdir) / "subs"
        result = subprocess.run(
            [
                "yt-dlp",
                "--skip-download",
                "--write-subs",
                "--write-auto-subs",
                "--sub-lang", "en",
                "--sub-format", "vtt",
                "--convert-subs", "srt",
                "-o", str(sub_path),
                url,
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )

        # Look for any .srt file in the temp dir
        srt_files = list(Path(tmpdir).glob("*.srt"))
        if not srt_files:
            return None

        srt_text = srt_files[0].read_text(encoding="utf-8", errors="replace")
        return _parse_srt(srt_text)


def _parse_srt(srt_text: str) -> str:
    """Convert SRT subtitle format to plain text, removing timestamps and duplicates."""
    lines = []
    prev_line = ""
    for line in srt_text.strip().split("\n"):
        line = line.strip()
        # Skip sequence numbers, timestamps, and empty lines
        if not line or line.isdigit() or "-->" in line:
            continue
        # Remove HTML-like tags
        clean = line.replace("<i>", "").replace("</i>", "").strip()
        # Deduplicate consecutive lines (common in auto-subs)
        if clean and clean != prev_line:
            lines.append(clean)
            prev_line = clean

    return " ".join(lines)


def _format_duration(seconds: int) -> str:
    h, remainder = divmod(seconds, 3600)
    m, s = divmod(remainder, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"
