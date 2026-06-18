"""Bridge between MarkdownStore change events and the search index."""

from __future__ import annotations

import logging

from amperstand_core.search.index import SearchIndex
from amperstand_core.search.parser import parse_sections
from amperstand_core.store.events import ChangeEvent, ChangeKind
from amperstand_core.store.store import MarkdownStore

logger = logging.getLogger(__name__)


class SearchIndexer:
    """Subscribes to a MarkdownStore's on_change hook, drives a SearchIndex."""

    def __init__(self, store: MarkdownStore, index: SearchIndex) -> None:
        self._store = store
        self._index = index

    @property
    def store(self) -> MarkdownStore:
        return self._store

    @property
    def index(self) -> SearchIndex:
        return self._index

    def handle_change(self, event: ChangeEvent) -> None:
        """Subscribe target — pass to MarkdownStore(on_change=...).

        Failures are logged, never raised. The store write is durable when this
        fires; a failed index update means the doc is searchable-stale or
        not-yet-searchable. `bootstrap(force=True)` recovers.
        """
        try:
            if event.kind in (ChangeKind.CREATED, ChangeKind.UPDATED):
                self._reindex_doc(event.id)
            elif event.kind == ChangeKind.DELETED:
                self._index.delete_doc(event.id)
        except Exception:
            logger.exception("search indexer failed for %s (%s)", event.id, event.kind)

    def bootstrap(self, force: bool = False) -> int:
        """Reindex every doc the store knows about. Returns count.

        force=False (default): no-op if the index already has data.
        force=True:           reset + rebuild.
        """
        if not force and not self._index.is_empty():
            return 0
        if force:
            self._index.reset()

        count = 0
        for meta in self._store.iter_all():
            try:
                self._reindex_doc(meta.id)
                count += 1
            except Exception:
                logger.exception("bootstrap reindex failed for %s", meta.id)
        return count

    def rebuild_in_place(self) -> int:
        """Walk every doc and upsert its sections. No table reset.

        Use when the index may be out of date (e.g. after a bulk migration that
        bypassed the change-hook). Safe to call while another process holds the
        same SQLite db open — SQLite WAL serializes the per-doc transactions.
        """
        count = 0
        for meta in self._store.iter_all():
            try:
                self._reindex_doc(meta.id)
                count += 1
            except Exception:
                logger.exception("rebuild reindex failed for %s", meta.id)
        return count

    # ── internal ────────────────────────────────────────────────────

    def _reindex_doc(self, doc_id: str) -> None:
        doc = self._store.get(doc_id)
        sections = parse_sections(doc.body, doc.meta.title)
        self._index.upsert_doc_sections(doc_id, sections)
