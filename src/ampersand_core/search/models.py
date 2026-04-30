"""Data models for the search module."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Section:
    """A heading-bounded chunk of a markdown document."""

    title: str | None
    """The heading text. None for the preamble before the first heading."""

    level: int
    """Heading level. 0 = preamble; 1..6 = h1..h6."""

    path: list[str] = field(default_factory=list)
    """Ancestor titles, including this section's own title."""

    body: str = ""
    """Markdown body for this section, no heading line."""


@dataclass(frozen=True)
class SearchResult:
    """One hit returned by SearchIndex.search()."""

    doc_id: str
    section_title: str | None
    section_path: list[str]
    snippet: str
    """Text snippet around the match, with <mark>…</mark> around hit terms."""

    score: float
    """Raw BM25 score from FTS5; lower = better."""
