"""Data models for captured content."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


class ContentType(str, Enum):
    ARTICLE = "article"
    VIDEO = "video"
    NEWSLETTER = "newsletter"


@dataclass
class CapturedContent:
    """Represents a piece of captured web content."""

    url: str
    title: str
    content_markdown: str
    content_type: ContentType = ContentType.ARTICLE
    author: str | None = None
    sender_email: str | None = None
    captured_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    tags: list[str] = field(default_factory=list)
