"""Content extraction from URLs using trafilatura."""

from __future__ import annotations

import re

import trafilatura

from ampersand_core.models import CapturedContent, ContentType

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


def extract_article(url: str) -> CapturedContent:
    """Fetch a URL and extract its article content as markdown."""
    downloaded = trafilatura.fetch_url(url)
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
