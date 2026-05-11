"""Feed ingestion router. See feed_api/__init__.py for the surface."""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ampersand_core.converter import to_markdown
from ampersand_core.extractor import (
    extract_article,
    is_linkedin_url,
    is_youtube_url,
)
from ampersand_core.feed import parse_feed
from ampersand_core.linkedin import extract_linkedin
from ampersand_core.store import MarkdownStore
from ampersand_core.youtube import extract_youtube

from ampersand_core.server.vault_api.auth import require_api_key
from ampersand_core.server.vault_api.store_factory import get_store

log = logging.getLogger(__name__)

router = APIRouter(
    prefix="/feeds",
    tags=["feeds"],
    dependencies=[Depends(require_api_key)],
)


class FeedRequest(BaseModel):
    url: str = Field(..., description="RSS/Atom feed URL.")
    limit: int = Field(
        default=25, ge=1, le=500,
        description="Max entries to consider, in feed order.",
    )
    tags: list[str] = Field(
        default_factory=list,
        description="Tags to attach to every captured doc.",
    )


class FeedEntryStatus(BaseModel):
    url: str
    title: str
    status: str  # "new" | "skip" (skip = already captured)
    existing_doc_id: str | None = None


class FeedPreviewResponse(BaseModel):
    feed_title: str
    total_entries: int
    considered: int  # min(total, limit)
    new_count: int
    skip_count: int
    entries: list[FeedEntryStatus]


class FeedCapturedItem(BaseModel):
    url: str
    title: str
    doc_id: str


class FeedFailedItem(BaseModel):
    url: str
    title: str
    error: str


class FeedIngestResponse(BaseModel):
    feed_title: str
    total_entries: int
    considered: int
    captured: list[FeedCapturedItem]
    skipped: list[FeedEntryStatus]
    failed: list[FeedFailedItem]


def _store_dep() -> MarkdownStore:
    return get_store()


def _capture_entry(entry_url: str):
    """Route an entry URL through the same extractor stack as POST /capture."""
    if is_youtube_url(entry_url):
        return extract_youtube(entry_url)
    if is_linkedin_url(entry_url):
        return extract_linkedin(entry_url)
    return extract_article(entry_url)


def _parse(url: str):
    try:
        return parse_feed(url)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"feed parse failed: {exc}")


@router.post("/preview", response_model=FeedPreviewResponse)
def preview(
    req: FeedRequest,
    store: Annotated[MarkdownStore, Depends(_store_dep)],
) -> FeedPreviewResponse:
    """Inspect a feed and report which entries are new vs. already captured.

    No writes — safe to call repeatedly. Use this to scope an ingest before
    committing to long extractor runs.
    """
    info = _parse(req.url)
    all_entries = info.entries or []
    considered = all_entries[: req.limit]

    statuses: list[FeedEntryStatus] = []
    new_count = 0
    skip_count = 0
    for entry in considered:
        existing = store.find_by_source(entry.url)
        if existing is not None:
            statuses.append(FeedEntryStatus(
                url=entry.url, title=entry.title, status="skip",
                existing_doc_id=existing.id,
            ))
            skip_count += 1
        else:
            statuses.append(FeedEntryStatus(
                url=entry.url, title=entry.title, status="new",
            ))
            new_count += 1

    return FeedPreviewResponse(
        feed_title=info.title,
        total_entries=len(all_entries),
        considered=len(considered),
        new_count=new_count,
        skip_count=skip_count,
        entries=statuses,
    )


@router.post("/ingest", response_model=FeedIngestResponse)
def ingest(
    req: FeedRequest,
    store: Annotated[MarkdownStore, Depends(_store_dep)],
) -> FeedIngestResponse:
    """Fetch the feed and capture entries we haven't seen before.

    Synchronous — extractors run inline. With limit=25 and a typical feed,
    this can take ~1–2 minutes (one HTTP round trip + extraction per entry).
    Failures on individual entries are logged and reported in `failed`; the
    rest still get captured.
    """
    info = _parse(req.url)
    all_entries = info.entries or []
    considered = all_entries[: req.limit]

    captured: list[FeedCapturedItem] = []
    skipped: list[FeedEntryStatus] = []
    failed: list[FeedFailedItem] = []

    for entry in considered:
        existing = store.find_by_source(entry.url)
        if existing is not None:
            skipped.append(FeedEntryStatus(
                url=entry.url, title=entry.title, status="skip",
                existing_doc_id=existing.id,
            ))
            continue

        try:
            content = _capture_entry(entry.url)
        except Exception as exc:
            log.warning("feed ingest extract failed for %s: %s", entry.url, exc)
            failed.append(FeedFailedItem(
                url=entry.url, title=entry.title, error=str(exc),
            ))
            continue

        # Feed metadata fills in gaps the extractor missed. captured_at gets
        # the feed's published timestamp so timeline views show essays in
        # publication order, not import order.
        fm: dict = {
            "title": content.title,
            "source": content.url,
            "type": content.content_type.value,
            "feed_url": req.url,
        }
        author = getattr(content, "author", None) or entry.author
        if author:
            fm["author"] = author
        if req.tags:
            fm["tags"] = list(req.tags)
        if entry.published is not None:
            fm["captured_at"] = entry.published

        try:
            doc = store.create(to_markdown(content), fm)
        except Exception as exc:
            log.exception("feed ingest store.create failed for %s", entry.url)
            failed.append(FeedFailedItem(
                url=entry.url, title=entry.title, error=str(exc),
            ))
            continue

        captured.append(FeedCapturedItem(
            url=entry.url, title=content.title or entry.title, doc_id=doc.meta.id,
        ))

    return FeedIngestResponse(
        feed_title=info.title,
        total_entries=len(all_entries),
        considered=len(considered),
        captured=captured,
        skipped=skipped,
        failed=failed,
    )
