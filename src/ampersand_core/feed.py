"""RSS/Atom feed parsing."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from time import mktime

import feedparser

from ampersand_core.extractor import is_youtube_url


@dataclass
class FeedEntry:
    """A single item from an RSS/Atom feed."""

    url: str
    title: str
    author: str | None = None
    published: datetime | None = None
    is_youtube: bool = False


@dataclass
class FeedInfo:
    """Metadata about a feed itself."""

    url: str
    title: str
    description: str | None = None
    entries: list[FeedEntry] | None = None


def parse_feed(feed_url: str) -> FeedInfo:
    """Parse an RSS/Atom feed and return its info and entries."""
    d = feedparser.parse(feed_url)

    if d.bozo and not d.entries:
        raise ValueError(f"Failed to parse feed: {feed_url} — {d.bozo_exception}")

    feed_title = d.feed.get("title", feed_url)
    feed_desc = d.feed.get("description") or d.feed.get("subtitle")

    entries = []
    for entry in d.entries:
        link = entry.get("link", "")
        if not link:
            continue

        published = None
        if hasattr(entry, "published_parsed") and entry.published_parsed:
            published = datetime.fromtimestamp(
                mktime(entry.published_parsed), tz=timezone.utc
            )
        elif hasattr(entry, "updated_parsed") and entry.updated_parsed:
            published = datetime.fromtimestamp(
                mktime(entry.updated_parsed), tz=timezone.utc
            )

        entries.append(
            FeedEntry(
                url=link,
                title=entry.get("title", "Untitled"),
                author=entry.get("author"),
                published=published,
                is_youtube=is_youtube_url(link),
            )
        )

    return FeedInfo(
        url=feed_url,
        title=feed_title,
        description=feed_desc,
        entries=entries,
    )
