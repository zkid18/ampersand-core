"""Content extraction from URLs using trafilatura."""

from __future__ import annotations

import logging
import re

import httpx
import trafilatura

from ampersand_core.models import CapturedContent, ContentType
from ampersand_core.proxy import get_proxy

logger = logging.getLogger(__name__)

_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

_YOUTUBE_PATTERNS = [
    re.compile(r"(?:youtube\.com/watch\?v=|youtu\.be/)([\w-]{11})"),
    re.compile(r"youtube\.com/embed/([\w-]{11})"),
    re.compile(r"youtube\.com/shorts/([\w-]{11})"),
]


def is_youtube_url(url: str) -> bool:
    return any(p.search(url) for p in _YOUTUBE_PATTERNS)


def extract_youtube_id(url: str) -> str | None:
    for p in _YOUTUBE_PATTERNS:
        m = p.search(url)
        if m:
            return m.group(1)
    return None


_LINKEDIN_VIDEO_PATTERNS = [
    re.compile(r"://(?:www\.)?linkedin\.com/posts/[^/]+.*\b(ugcPost|activity)\b"),
    re.compile(r"://(?:www\.)?linkedin\.com/feed/update/urn:li:(ugcPost|activity):\d+"),
    re.compile(r"://(?:www\.)?linkedin\.com/video/[^/]+"),
    re.compile(r"://(?:www\.)?linkedin\.com/embed/feed/update/urn:li:(ugcPost|activity):\d+"),
]


def is_linkedin_url(url: str) -> bool:
    return any(p.search(url) for p in _LINKEDIN_VIDEO_PATTERNS)


def _playwright_proxy_kwargs() -> dict | None:
    """Translate AMPERSAND_HTTP_PROXY (user:pass@host:port URL) into the
    shape playwright expects for chromium.launch(proxy=...). None if no
    proxy is configured.
    """
    from urllib.parse import urlparse

    raw = get_proxy()
    if not raw:
        return None
    parsed = urlparse(raw)
    if not parsed.hostname or not parsed.port:
        return None
    kwargs: dict = {"server": f"{parsed.scheme or 'http'}://{parsed.hostname}:{parsed.port}"}
    if parsed.username:
        kwargs["username"] = parsed.username
    if parsed.password:
        kwargs["password"] = parsed.password
    return kwargs


def _fetch_with_playwright(url: str) -> str:
    """Fetch a page using a headless Chromium browser (JS-rendered).

    Routes through the residential proxy if one is configured — JS-heavy
    sites often also enforce per-ASN blocks, and a datacenter Chromium hit
    fails the same way trafilatura would.
    """
    from playwright.sync_api import sync_playwright

    launch_kwargs: dict = {"headless": True}
    proxy_kwargs = _playwright_proxy_kwargs()
    if proxy_kwargs:
        launch_kwargs["proxy"] = proxy_kwargs
        logger.info("playwright launching with proxy=%s", proxy_kwargs["server"])

    with sync_playwright() as p:
        browser = p.chromium.launch(**launch_kwargs)
        page = browser.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        # Give JS a moment to render dynamic content
        page.wait_for_timeout(3000)
        html = page.content()
        browser.close()
    return html


def _fetch_url(url: str) -> str:
    """Fetch URL HTML with multi-tier fallback.

    Order: trafilatura direct → httpx direct → httpx via proxy (if configured)
    → playwright. The proxy tier is skipped entirely when no proxy is set —
    direct fetches don't burn proxy bandwidth.
    """
    # Tier 1: trafilatura (fast, lightweight, urllib internally — no proxy)
    downloaded = trafilatura.fetch_url(url)
    if downloaded:
        return downloaded
    logger.debug("trafilatura failed for %s, trying httpx", url)

    # Tier 2: httpx with browser headers, direct
    try:
        resp = httpx.get(url, headers=_BROWSER_HEADERS, follow_redirects=True, timeout=30)
        resp.raise_for_status()
        if resp.text and len(resp.text.strip()) > 200:
            return resp.text
        logger.debug("httpx (direct) returned thin content for %s", url)
    except httpx.HTTPError as exc:
        logger.debug("httpx (direct) failed for %s: %s", url, exc)

    # Tier 3: httpx via proxy (residential, escapes datacenter IP blocks)
    via_proxy = _fetch_via_proxy(url)
    if via_proxy:
        return via_proxy

    # Tier 4: headless browser (handles JS-rendered pages)
    logger.info("Using playwright for %s", url)
    return _fetch_with_playwright(url)


def _fetch_via_proxy(url: str) -> str | None:
    """Fetch via the configured residential proxy. None if no proxy is set
    or the request fails. Used both as a tier in _fetch_url and as a retry
    in extract_article when direct fetch returned a block-page that
    trafilatura.extract couldn't get content out of.
    """
    proxy = get_proxy()
    if not proxy:
        return None
    try:
        resp = httpx.get(
            url,
            headers=_BROWSER_HEADERS,
            follow_redirects=True,
            timeout=45,
            proxy=proxy,
        )
        resp.raise_for_status()
        if resp.text and len(resp.text.strip()) > 200:
            logger.info("Fetched %s via proxy (status=%d, %d bytes)",
                        url, resp.status_code, len(resp.text))
            return resp.text
        logger.debug("httpx (proxy) returned thin content for %s", url)
    except httpx.HTTPError as exc:
        logger.debug("httpx (proxy) failed for %s: %s", url, exc)
    return None


def extract_article(url: str) -> CapturedContent:
    """Fetch a URL and extract its article content as markdown."""
    downloaded = _fetch_url(url)
    if not downloaded:
        raise ValueError(f"Failed to fetch URL: {url}")

    # Extract main text as markdown
    text = trafilatura.extract(
        downloaded,
        output_format="markdown",
        include_links=True,
        include_images=True,
        include_tables=True,
    )

    # If extraction came up empty or trivially short, the direct fetch may
    # have been served a block / captcha / cookie wall page. Retry through
    # the residential proxy and re-extract.
    if (not text or len(text.strip()) < 200):
        retry_html = _fetch_via_proxy(url)
        if retry_html:
            retry_text = trafilatura.extract(
                retry_html,
                output_format="markdown",
                include_links=True,
                include_images=True,
                include_tables=True,
            )
            if retry_text and len(retry_text.strip()) > len((text or "").strip()):
                logger.info("extract via proxy improved content for %s", url)
                downloaded = retry_html
                text = retry_text

    if not text:
        raise ValueError(f"Failed to extract content from: {url}")

    # Clean up empty image tags and table artifacts
    text = re.sub(r"!\[\]\(\)\s*\|?\s*", "", text)
    text = re.sub(r"^\|?\s*\n", "", text, flags=re.MULTILINE)
    text = text.lstrip("\n")

    # Extract metadata
    metadata = trafilatura.extract_metadata(downloaded)

    title = "Untitled"
    author = None
    if metadata:
        title = metadata.title or title
        author = metadata.author

    return CapturedContent(
        url=url,
        title=title,
        content_markdown=text,
        content_type=ContentType.ARTICLE,
        author=author,
    )
