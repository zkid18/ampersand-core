"""YouTube transcript and metadata extraction.

Uses two lightweight HTTP APIs that work from datacenter IPs (where yt-dlp
gets bot-blocked by YouTube's anti-scraping):

- `oembed`             — public, no auth — for title and channel
- `youtube-transcript-api` — caption endpoint, often works without cookies

If either fails the function still returns a CapturedContent with whatever
info we got. yt-dlp is no longer used; YouTube blocks it on cloud IPs with
"Sign in to confirm you're not a bot" and that path was unreliable.
"""

from __future__ import annotations

import os
import urllib.parse

import httpx

from ampersand_core.models import CapturedContent, ContentType

OEMBED_TIMEOUT = 10
OEMBED_URL = "https://www.youtube.com/oembed"
TRANSCRIPT_LANGS = ["en", "ru", "es", "pt", "fr", "de"]

# When set, all YouTube traffic (oembed + transcript fetches) is routed
# through this proxy. Required when running on a cloud-provider IP that
# YouTube blocks. Format: http://user:pass@host:port (also supports
# socks5:// when httpx[socks] is installed).
PROXY_ENV = "AMPERSAND_YOUTUBE_PROXY"


def _proxy_url() -> str | None:
    raw = os.environ.get(PROXY_ENV, "").strip()
    return raw or None


def extract_youtube(url: str) -> CapturedContent:
    """Extract transcript + metadata from a YouTube URL."""
    video_id = _video_id(url) or ""
    meta = _oembed(url)

    title = meta.get("title") or "Untitled Video"
    channel = meta.get("author_name") or "Unknown"

    transcript, transcript_lang, transcript_err = _transcript(video_id)

    lines: list[str] = [f"**Channel**: {channel}"]
    if meta.get("author_url"):
        lines.append(f"**Channel URL**: {meta['author_url']}")
    lines.append("")

    if transcript:
        if transcript_lang:
            lines.append(f"## Transcript ({transcript_lang})")
        else:
            lines.append("## Transcript")
        lines.append("")
        lines.append(transcript)
    elif transcript_err == "ip_blocked":
        lines.append(
            "*Transcript unavailable — YouTube blocked this server's IP "
            "(typical for cloud-provider IPs). To get transcripts, route "
            "this fetch through a residential IP or use a proxy.*"
        )
    else:
        lines.append(
            "*No transcript available — captions may be disabled for this "
            "video, or no caption track exists in the languages we tried.*"
        )

    return CapturedContent(
        url=url,
        title=title,
        content_markdown="\n".join(lines),
        content_type=ContentType.VIDEO,
        author=channel,
    )


def _video_id(url: str) -> str | None:
    """Pull the video id out of a youtube.com or youtu.be URL."""
    parsed = urllib.parse.urlparse(url)
    host = parsed.netloc.lower()
    if host.endswith("youtu.be"):
        return parsed.path.lstrip("/").split("/", 1)[0] or None
    if "youtube.com" in host:
        qs = urllib.parse.parse_qs(parsed.query)
        if "v" in qs:
            return qs["v"][0]
        # /shorts/<id> and /embed/<id>
        parts = parsed.path.strip("/").split("/")
        if len(parts) >= 2 and parts[0] in ("shorts", "embed", "live"):
            return parts[1]
    return None


def _oembed(url: str) -> dict:
    """Fetch oembed metadata. Returns {} on failure (don't raise)."""
    proxy = _proxy_url()
    try:
        client_kwargs = {
            "timeout": OEMBED_TIMEOUT,
            "follow_redirects": True,
            "headers": {"User-Agent": "Mozilla/5.0 ampersand"},
        }
        if proxy:
            client_kwargs["proxy"] = proxy
        with httpx.Client(**client_kwargs) as client:
            r = client.get(OEMBED_URL, params={"url": url, "format": "json"})
        if r.status_code != 200:
            return {}
        return r.json()
    except (httpx.HTTPError, ValueError):
        return {}


def _transcript(video_id: str) -> tuple[str | None, str | None, str | None]:
    """Best-effort transcript fetch via youtube-transcript-api.

    Returns (text, language_code, error_kind). error_kind is one of:
      - None              when text is present
      - "ip_blocked"      when YouTube refused the request because of the
                          source IP (typical for cloud providers)
      - "no_captions"     when the video has no captions in our preferred
                          languages, or any other generic failure
    """
    if not video_id:
        return None, None, "no_captions"
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
    except ImportError:
        return None, None, "no_captions"

    # The lib's specific exception name varies by version; match by class name.
    try:
        api = _build_transcript_api()
        if hasattr(api, "fetch"):
            fetched = api.fetch(video_id, languages=TRANSCRIPT_LANGS)
            raw = [{"text": s.text} for s in fetched]
            lang = getattr(fetched, "language_code", None)
        else:
            raw = YouTubeTranscriptApi.get_transcript(  # type: ignore[attr-defined]
                video_id, languages=TRANSCRIPT_LANGS
            )
            lang = None
    except Exception as exc:  # noqa: BLE001
        kind = "ip_blocked" if "RequestBlocked" in type(exc).__name__ or "IpBlocked" in type(exc).__name__ else "no_captions"
        return None, None, kind

    if not raw:
        return None, lang, "no_captions"
    text = " ".join(seg.get("text", "").strip() for seg in raw if seg.get("text"))
    text = text.replace("\n", " ").strip()
    if not text:
        return None, lang, "no_captions"
    return text, lang, None


def _build_transcript_api():
    """Construct a YouTubeTranscriptApi instance, optionally with a proxy."""
    from youtube_transcript_api import YouTubeTranscriptApi

    proxy = _proxy_url()
    if not proxy:
        return YouTubeTranscriptApi()
    try:
        from youtube_transcript_api.proxies import GenericProxyConfig

        return YouTubeTranscriptApi(
            proxy_config=GenericProxyConfig(http_url=proxy, https_url=proxy)
        )
    except ImportError:
        # Older versions of the library lack proxy support — fall back to no proxy
        return YouTubeTranscriptApi()
