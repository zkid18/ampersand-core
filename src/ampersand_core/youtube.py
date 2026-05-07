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

import html as _htmllib
import os
import re
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


class YouTubeTranscriptUnavailable(ValueError):
    """Raised when no usable transcript could be fetched for a YouTube URL.

    Carries the partial metadata we did fetch (title, channel) so callers
    can still construct a stub if they choose. Used by the server's
    /capture/html dispatch to fall back to caller-supplied HTML — most
    YouTube watch pages render the description into the DOM, which is
    still useful even without a transcript.
    """

    def __init__(self, message: str, *, title: str, channel: str, channel_url: str | None) -> None:
        super().__init__(message)
        self.title = title
        self.channel = channel
        self.channel_url = channel_url


def _proxy_url() -> str | None:
    raw = os.environ.get(PROXY_ENV, "").strip()
    return raw or None


def extract_youtube(url: str) -> CapturedContent:
    """Extract transcript + metadata from a YouTube URL.

    Raises YouTubeTranscriptUnavailable when no transcript could be fetched
    — saving a stub doc that just says "no transcript" is worse than
    failing, since callers can choose a richer fallback (page HTML).
    """
    video_id = _video_id(url) or ""
    meta = _oembed(url)

    title = meta.get("title") or "Untitled Video"
    channel = meta.get("author_name") or "Unknown"
    channel_url = meta.get("author_url")

    transcript, transcript_lang, transcript_err = _transcript(video_id)

    if not transcript:
        reason = (
            "YouTube blocked this server's IP" if transcript_err == "ip_blocked"
            else "captions disabled or no track in our languages"
        )
        raise YouTubeTranscriptUnavailable(
            f"No transcript for {url} — {reason}",
            title=title,
            channel=channel,
            channel_url=channel_url,
        )

    lines: list[str] = [f"**Channel**: {channel}"]
    if channel_url:
        lines.append(f"**Channel URL**: {channel_url}")
    lines.append("")
    if transcript_lang:
        lines.append(f"## Transcript ({transcript_lang})")
    else:
        lines.append("## Transcript")
    lines.append("")
    lines.append(transcript)

    return CapturedContent(
        url=url,
        title=title,
        content_markdown="\n".join(lines),
        content_type=ContentType.VIDEO,
        author=channel,
    )


def youtube_stub_from_html(
    url: str,
    page_html: str,
    fallback: YouTubeTranscriptUnavailable | None = None,
) -> CapturedContent:
    """Build a YouTube CapturedContent from caller-supplied watch-page HTML.

    For the clipper path when no transcript is available. trafilatura
    extracts the page chrome (sidebar, comments) on YouTube — not useful.
    Instead, pull the canonical bits straight from <meta property="og:*">
    which YouTube reliably sets server-side. Falls back to whatever
    metadata oembed already gave us (carried on the exception).
    """
    title = _meta_content(page_html, "og:title") or (fallback.title if fallback else None) or "Untitled Video"
    description = _meta_content(page_html, "og:description") or _meta_content(page_html, "description") or ""
    channel = (fallback.channel if fallback else None) or "Unknown"
    channel_url = fallback.channel_url if fallback else None

    lines: list[str] = [f"**Channel**: {channel}"]
    if channel_url:
        lines.append(f"**Channel URL**: {channel_url}")
    lines.append("")
    lines.append("*Transcript unavailable for this video.*")
    if description.strip():
        lines.append("")
        lines.append("## Description")
        lines.append("")
        lines.append(description.strip())

    return CapturedContent(
        url=url,
        title=title,
        content_markdown="\n".join(lines),
        content_type=ContentType.VIDEO,
        author=channel,
    )


def _meta_content(html: str, name: str) -> str | None:
    """Extract the content of a <meta name|property="X"> tag. None if absent.

    Hand-rolled rather than via BeautifulSoup to avoid pulling in another
    dep just for two attributes. Quote handling: capture group [^Q]+ uses
    backreference \\1 to the opening quote so apostrophes inside a
    double-quoted value (and vice-versa) don't truncate the match.
    """
    if not html:
        return None
    n = re.escape(name)
    patterns = (
        # <meta property="X" content="Y">
        rf'<meta\b[^>]*?\b(?:property|name)\s*=\s*(["\']){n}\1[^>]*?\bcontent\s*=\s*(["\'])(.*?)\2',
        # <meta content="Y" property="X">  (attribute order reversed)
        rf'<meta\b[^>]*?\bcontent\s*=\s*(["\'])(.*?)\1[^>]*?\b(?:property|name)\s*=\s*(["\']){n}\3',
    )
    for pattern in patterns:
        m = re.search(pattern, html, re.IGNORECASE | re.DOTALL)
        if m:
            # Group index of the captured content varies per pattern.
            # First pattern: groups (open_q, close_q, content) → index 3.
            # Second pattern: groups (open_q, content, close_q) → index 2.
            value = m.group(3) if pattern is patterns[0] else m.group(2)
            return _htmllib.unescape(value)
    return None


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
