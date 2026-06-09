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


# HTML markers (widgets/scripts) that indicate the page is an anti-bot
# wall, regardless of the wall's display language. These are concrete
# structural elements, not localized strings.
_CHALLENGE_HTML_MARKERS = (
    "challenges.cloudflare.com",        # Cloudflare Turnstile widget
    "/cdn-cgi/challenge-platform/",     # Cloudflare challenge platform
    "g-recaptcha",                      # Google reCAPTCHA widget
    "h-captcha",                        # hCaptcha widget
    "data-sitekey=",                    # generic captcha sitekey attribute
)

# Title patterns that strongly suggest the page is a wall, not content.
# Substring match is intentional — Cloudflare/Akamai/etc localize the
# trailing text but keep these stems.
_CHALLENGE_TITLE_STEMS = (
    "just a moment",
    "attention required",
    "access denied",
    "ddos protection",
    "verify you are",
    "verifying you are",
    "checking your browser",
    "security check",
    # 404 / not-found pages — many sites serve these with HTTP 200 + a
    # standard error page body, which the extractor used to silently
    # save (friction test: Verge "404 Not Found | The Verge" landed in
    # vault as a real article). Phrases are deliberately specific to
    # avoid false-positives on legit articles whose title contains "404".
    "404 not found",
    "page not found",
    "this page could not be found",
    "page can't be found",
    "page can’t be found",  # smart-quote variant
    "this page isn't available",
    "this page isn’t available",
)


def _looks_like_challenge(text: str | None, title: str | None, html: str | None) -> bool:
    """Heuristic: does this look like a wall/challenge page rather than an
    article? Three orthogonal signals — any one trips it:

    1. Concrete HTML widget markers (Cloudflare/captcha) anywhere in the
       fetched HTML. Language-independent.
    2. Title is a known wall-page stem ("Just a Moment...", "Access Denied").
    3. Extracted text body is suspiciously short AND the title is missing
       or generic — articles with weak metadata still tend to have body.

    Real articles can be short, so we lean on combinations rather than
    length alone.
    """
    if html:
        h = html[:50_000]  # cap scan for huge SPA payloads
        for marker in _CHALLENGE_HTML_MARKERS:
            if marker in h:
                return True

    norm_title = (title or "").strip().lower()
    if norm_title:
        for stem in _CHALLENGE_TITLE_STEMS:
            if stem in norm_title:
                return True

    body = (text or "").strip()
    weak_title = norm_title in {"", "untitled"}
    if weak_title and len(body) < 400:
        return True
    return False


def _extract_pieces(html: str) -> tuple[str | None, str | None, str | None]:
    """Run trafilatura.extract + extract_metadata once on a downloaded HTML
    blob. Returns (markdown_text, title, author) — any may be None.
    """
    text = trafilatura.extract(
        html,
        output_format="markdown",
        include_links=True,
        include_images=True,
        include_tables=True,
    )
    metadata = trafilatura.extract_metadata(html)
    title = metadata.title if metadata and metadata.title else None
    author = metadata.author if metadata and metadata.author else None
    return text, title, author


def extract_article_from_html(
    url: str, html: str, *, fallback_title: str | None = None
) -> CapturedContent:
    """Run the article extraction pipeline on caller-supplied HTML.

    Used by /capture/html (browser-extension clipper) where the user's
    authenticated browser session has already fetched the page; we don't
    want to refetch from the server. Skips fetch tiers and the proxy
    retry — the caller already got the real page. Challenge detection
    still applies (the HTML may include a captcha widget if the page is
    a paywall stub).
    """
    if not html or not html.strip():
        raise ValueError(f"Empty HTML for {url}")

    text, title, author = _extract_pieces(html)
    if not text:
        raise ValueError(f"Failed to extract content from supplied HTML for: {url}")

    if not title:
        title = fallback_title

    if _looks_like_challenge(text, title, html):
        raise ValueError(
            f"Supplied HTML for {url} looks like a wall/challenge page; "
            f"capture aborted."
        )

    text = re.sub(r"!\[\]\(\)\s*\|?\s*", "", text)
    text = re.sub(r"^\|?\s*\n", "", text, flags=re.MULTILINE)
    text = text.lstrip("\n")

    return CapturedContent(
        url=url,
        title=title or "Untitled",
        content_markdown=text,
        content_type=ContentType.ARTICLE,
        author=author,
    )


def extract_article(url: str) -> CapturedContent:
    """Fetch a URL and extract its article content as markdown."""
    downloaded = _fetch_url(url)
    if not downloaded:
        raise ValueError(f"Failed to fetch URL: {url}")

    text, title, author = _extract_pieces(downloaded)

    # Retry through the residential proxy if the direct fetch came back empty,
    # trivially short, OR looking like a wall/challenge page. The proxy uses
    # a residential IP and often gets the real content where the datacenter
    # IP gets fronted by Cloudflare/captcha/etc.
    needs_retry = (
        not text
        or len(text.strip()) < 200
        or _looks_like_challenge(text, title, downloaded)
    )
    if needs_retry:
        retry_html = _fetch_via_proxy(url)
        if retry_html:
            r_text, r_title, r_author = _extract_pieces(retry_html)
            retry_is_better = (
                r_text
                and not _looks_like_challenge(r_text, r_title, retry_html)
                and len(r_text.strip()) > len((text or "").strip())
            )
            if retry_is_better:
                logger.info("extract via proxy improved content for %s", url)
                downloaded, text, title, author = retry_html, r_text, r_title, r_author

    if not text:
        raise ValueError(f"Failed to extract content from: {url}")

    if _looks_like_challenge(text, title, downloaded):
        raise ValueError(
            f"Anti-bot wall served for {url} — short body / generic title / "
            f"captcha-widget HTML. Capture aborted; try again later or via "
            f"a different network."
        )

    # Clean up empty image tags and table artifacts
    text = re.sub(r"!\[\]\(\)\s*\|?\s*", "", text)
    text = re.sub(r"^\|?\s*\n", "", text, flags=re.MULTILINE)
    text = text.lstrip("\n")

    return CapturedContent(
        url=url,
        title=title or "Untitled",
        content_markdown=text,
        content_type=ContentType.ARTICLE,
        author=author,
    )
