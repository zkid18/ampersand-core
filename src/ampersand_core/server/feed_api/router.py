"""Feed ingestion router. See feed_api/__init__.py for the surface."""

from __future__ import annotations

import logging
import os
from pathlib import Path
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

from ampersand_core.server.feed_registry import FeedRegistry
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


# ── server-side registry ──────────────────────────────────────────────
#
# Self-hoster S2/S4: the laptop's `feed add` never reached the droplet.
# These endpoints make the server the single source of truth for "which
# feeds should the periodic sync touch".


class FeedRegisterRequest(BaseModel):
    url: str = Field(..., description="RSS/Atom feed URL.")
    name: str | None = Field(default=None, description="Friendly name (optional).")
    tags: list[str] = Field(default_factory=list)


class FeedListItem(BaseModel):
    id: str
    url: str
    name: str | None = None
    tags: list[str] = Field(default_factory=list)
    enabled: bool = True
    created_at: str
    last_sync_at: str | None = None
    last_status: str | None = None
    last_error: str | None = None


class FeedListResponse(BaseModel):
    items: list[FeedListItem]


class FeedSyncFeedResult(BaseModel):
    feed_id: str
    url: str
    name: str | None = None
    status: str  # "ok" | "failed"
    captured: int = 0
    skipped: int = 0
    failed: int = 0
    error: str | None = None


class FeedSyncResponse(BaseModel):
    total_feeds: int
    results: list[FeedSyncFeedResult]


# Lazy singleton — same pattern as the JobStore in app.py. The registry
# lives next to the vault on disk so it shares the data-dir hygiene
# (one DB per droplet, survives restarts).
_registry_singleton: FeedRegistry | None = None


def _registry() -> FeedRegistry:
    global _registry_singleton
    if _registry_singleton is None:
        data_dir = Path(os.environ.get("AMPERSAND_DATA_DIR", "/var/lib/ampersand/vault"))
        _registry_singleton = FeedRegistry(data_dir / ".store" / "feed_registry.db")
    return _registry_singleton


def reset_registry_cache() -> None:
    """Drop the cached FeedRegistry so the next call picks up a new
    AMPERSAND_DATA_DIR. Used by tests."""
    global _registry_singleton
    _registry_singleton = None


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


# ── registry endpoints ───────────────────────────────────────────────


def _ingest_one_feed_into_registry(
    feed_row: dict,
    store: MarkdownStore,
    *,
    limit: int = 25,
) -> FeedSyncFeedResult:
    """Helper: run the same per-feed extract-and-capture loop that POST
    /feeds/ingest does, but driven from a registry row. Updates the
    registry's last_sync_at / last_status after."""
    feed_id = feed_row["id"]
    url = feed_row["url"]
    extra_tags = list(feed_row.get("tags") or [])
    try:
        info = _parse(url)
    except HTTPException as exc:
        _registry().record_sync(feed_id, status="failed", error=str(exc.detail))
        return FeedSyncFeedResult(
            feed_id=feed_id, url=url, name=feed_row.get("name"),
            status="failed", error=str(exc.detail),
        )

    all_entries = info.entries or []
    considered = all_entries[:limit]
    captured = skipped = failed = 0

    for entry in considered:
        existing = store.find_by_source(entry.url)
        if existing is not None:
            skipped += 1
            continue
        try:
            content = _capture_entry(entry.url)
        except Exception as exc:
            log.warning("registry-sync extract failed for %s: %s", entry.url, exc)
            failed += 1
            continue
        fm: dict = {
            "title": content.title,
            "source": content.url,
            "type": content.content_type.value,
            "feed_url": url,
        }
        author = getattr(content, "author", None) or entry.author
        if author:
            fm["author"] = author
        if extra_tags:
            fm["tags"] = extra_tags
        if entry.published is not None:
            fm["captured_at"] = entry.published
        try:
            store.create(to_markdown(content), fm)
        except Exception as exc:
            log.exception("registry-sync store.create failed for %s", entry.url)
            failed += 1
            continue
        captured += 1

    status = "failed" if (failed > 0 and captured == 0) else "ok"
    _registry().record_sync(feed_id, status=status, error=None)
    return FeedSyncFeedResult(
        feed_id=feed_id, url=url, name=feed_row.get("name"),
        status=status, captured=captured, skipped=skipped, failed=failed,
    )


@router.post("/register", response_model=FeedListItem, status_code=201)
def register_feed(req: FeedRegisterRequest) -> FeedListItem:
    """Add a feed to the persistent registry. Idempotent on URL — registering
    an existing URL merges any new name/tags into the existing row instead of
    creating a duplicate."""
    row = _registry().add(req.url, name=req.name, tags=req.tags)
    return FeedListItem(**row)


@router.get("", response_model=FeedListResponse)
def list_feeds(enabled_only: bool = False) -> FeedListResponse:
    """List every registered feed. `enabled_only=true` skips ones that have
    been paused with /feeds/{id}/disable."""
    items = [FeedListItem(**r) for r in _registry().list(enabled_only=enabled_only)]
    return FeedListResponse(items=items)


@router.delete("/{feed_id}", status_code=204)
def remove_feed(feed_id: str) -> None:
    if not _registry().remove(feed_id):
        raise HTTPException(status_code=404, detail="feed not found")


@router.post("/{feed_id}/disable", response_model=FeedListItem)
def disable_feed(feed_id: str) -> FeedListItem:
    if not _registry().set_enabled(feed_id, False):
        raise HTTPException(status_code=404, detail="feed not found")
    return FeedListItem(**_registry().get(feed_id))


@router.post("/{feed_id}/enable", response_model=FeedListItem)
def enable_feed(feed_id: str) -> FeedListItem:
    if not _registry().set_enabled(feed_id, True):
        raise HTTPException(status_code=404, detail="feed not found")
    return FeedListItem(**_registry().get(feed_id))


@router.post("/sync", response_model=FeedSyncResponse)
def sync_all_feeds(
    store: Annotated[MarkdownStore, Depends(_store_dep)],
    limit: int = 25,
) -> FeedSyncResponse:
    """Iterate every enabled feed in the registry and run the extract+capture
    loop on each. Called by `ampersand-feed-sync.timer` on the droplet —
    that systemd unit used to invoke the CLI's `ampersand feed sync` which
    read a client-side state.json that never reached the server (S2/S4)."""
    results: list[FeedSyncFeedResult] = []
    enabled_feeds = _registry().list(enabled_only=True)
    for feed_row in enabled_feeds:
        results.append(_ingest_one_feed_into_registry(feed_row, store, limit=limit))
    return FeedSyncResponse(total_feeds=len(enabled_feeds), results=results)
