"""LinkedIn video transcript extraction via Playwright + faster-whisper."""

from __future__ import annotations

import logging
import re
import tempfile
from pathlib import Path

import httpx

from ampersand_core.models import CapturedContent, ContentType

logger = logging.getLogger(__name__)

_VIDEO_CDN_PATTERN = re.compile(r"dms\.licdn\.com/playlist/vid/.*mp4")
_CAPTIONS_PATTERN = re.compile(r"dms\.licdn\.com/playlist/vid/.*captions.*webvtt", re.IGNORECASE)


def extract_linkedin(url: str) -> CapturedContent:
    """Extract from a LinkedIn post.

    Video posts (with a stream or captions): pull the transcript via WebVTT
    or faster-whisper. Text/image posts (most common shape): fall back to
    an og:meta stub so the user at least gets the title, author, and
    (LinkedIn-truncated) preview of the post text. Beats a 422.
    """
    video_url, captions_url, title, author, description = _inspect_post(url)

    # Path A: no video stream — text/image/article post. Save the og:meta
    # stub. LinkedIn truncates og:description to ~150 chars so the body is
    # always a teaser, never the full post. Still useful as a bookmark.
    if not video_url and not captions_url:
        return _stub_from_meta(url, title, author, description)

    # Path B: video stream found — transcribe it.
    transcript = ""
    if captions_url:
        logger.info("Using LinkedIn captions (WebVTT)")
        transcript = _fetch_captions(captions_url)

    if not transcript and video_url:
        logger.info("No captions, falling back to ASR transcription")
        with tempfile.TemporaryDirectory() as tmpdir:
            video_path = Path(tmpdir) / "video.mp4"
            _download_video(video_url, video_path)
            transcript = _transcribe_video(video_path)

    lines = []
    if author:
        lines.append(f"**Author**: {author}")
        lines.append("")

    if transcript:
        lines.append("## Transcript")
        lines.append("")
        lines.append(transcript)
    else:
        lines.append("*No speech detected in video.*")

    return CapturedContent(
        url=url,
        title=title,
        content_markdown="\n".join(lines),
        content_type=ContentType.VIDEO,
        author=author,
    )


def _stub_from_meta(
    url: str, title: str, author: str | None, description: str | None,
) -> CapturedContent:
    """Build a stub doc for a LinkedIn text/image post from og:meta only."""
    lines = []
    if author:
        lines.append(f"**Author**: {author}")
        lines.append("")
    if description:
        lines.append(description)
    else:
        lines.append(
            "*LinkedIn post content not visible without authentication. "
            "Open the source link to view the full post.*"
        )
    return CapturedContent(
        url=url,
        title=title,
        content_markdown="\n".join(lines),
        content_type=ContentType.ARTICLE,
        author=author,
    )


def _inspect_post(url: str) -> tuple[str | None, str | None, str, str | None, str | None]:
    """Open a LinkedIn post with Playwright and collect everything we need.

    Returns (video_url, captions_url, title, author, description). Any field
    may be None — caller decides whether to transcribe a video or fall back
    to a meta stub.
    """
    from playwright.sync_api import sync_playwright

    video_url: str | None = None
    captions_url: str | None = None

    def _on_response(response):
        nonlocal video_url, captions_url
        resp_url = response.url
        if captions_url is None and _CAPTIONS_PATTERN.search(resp_url):
            captions_url = resp_url
        if video_url is None and _VIDEO_CDN_PATTERN.search(resp_url):
            # Avoid matching the captions URL as a video URL
            if not _CAPTIONS_PATTERN.search(resp_url):
                video_url = resp_url

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.on("response", _on_response)
        page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        page.wait_for_timeout(5000)

        # If video didn't auto-play, try clicking the play button
        if video_url is None and captions_url is None:
            try:
                play_btn = page.locator("button.vjs-big-play-button, [aria-label='Play']")
                if play_btn.count() > 0:
                    play_btn.first.click()
                    page.wait_for_timeout(3000)
            except Exception:
                pass

        # Extract metadata from og:title + og:description
        title = "LinkedIn post"
        author = None
        description: str | None = None
        try:
            og_title = page.locator('meta[property="og:title"]').get_attribute("content")
            if og_title:
                title = og_title
                # LinkedIn og:title is often "Author Name on LinkedIn: post text..."
                m = re.match(r"^(.+?)\s+on\s+LinkedIn", og_title)
                if m:
                    author = m.group(1)
        except Exception:
            pass
        try:
            og_desc = page.locator('meta[property="og:description"]').get_attribute("content")
            if og_desc and og_desc.strip():
                description = og_desc.strip()
        except Exception:
            pass

        browser.close()

    return video_url, captions_url, title, author, description


def _fetch_captions(captions_url: str) -> str:
    """Download WebVTT captions and convert to plain text."""
    resp = httpx.get(captions_url, follow_redirects=True, timeout=30)
    resp.raise_for_status()
    return _parse_webvtt(resp.text)


def _parse_webvtt(vtt_text: str) -> str:
    """Convert WebVTT subtitle format to plain text, removing timestamps and duplicates."""
    lines = []
    prev_line = ""
    for line in vtt_text.strip().split("\n"):
        line = line.strip()
        # Skip WEBVTT header, sequence numbers, timestamps, and empty lines
        if not line or line.startswith("WEBVTT") or line.startswith("NOTE"):
            continue
        if "-->" in line:
            continue
        if line.isdigit():
            continue
        # Remove HTML-like tags
        clean = re.sub(r"<[^>]+>", "", line).strip()
        # Deduplicate consecutive lines (common in auto-captions)
        if clean and clean != prev_line:
            lines.append(clean)
            prev_line = clean

    return " ".join(lines)


def _download_video(video_url: str, dest: Path) -> None:
    """Download a video file via streaming HTTP."""
    with httpx.stream("GET", video_url, follow_redirects=True, timeout=120) as resp:
        resp.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in resp.iter_bytes(chunk_size=65_536):
                f.write(chunk)


def _transcribe_video(video_path: Path) -> str:
    """Transcribe video audio using faster-whisper (local ASR)."""
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        raise ImportError(
            "faster-whisper is required for LinkedIn video transcription. "
            "Install it with: pip install 'ampersand-core[linkedin]'"
        )

    model = WhisperModel("base", device="cpu", compute_type="int8")
    segments, _info = model.transcribe(str(video_path))
    texts = [segment.text.strip() for segment in segments if segment.text.strip()]

    return " ".join(texts)
