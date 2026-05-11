"""Build the (store, FTS indexer, vec indexer) graph and bootstrap them.

The MarkdownStore takes a single on_change hook, so we fan out internally:
each indexer (FTS, vec) sees every event but failures in one don't block
the other. Vec is optional — if OPENAI_API_KEY isn't set or sqlite-vec
isn't loadable, the rest of the system runs unchanged and semantic search
just isn't available.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from ampersand_core.search import (
    SearchIndex,
    SearchIndexer,
    VectorIndex,
    VectorIndexer,
)
from ampersand_core.store import MarkdownStore

DEFAULT_DATA_DIR = Path.home() / ".ampersand" / "vault"

log = logging.getLogger(__name__)


def _resolve_data_dir() -> Path:
    raw = os.environ.get("AMPERSAND_DATA_DIR")
    return Path(raw).expanduser() if raw else DEFAULT_DATA_DIR


@dataclass
class Indexers:
    store: MarkdownStore
    fts: SearchIndexer
    vec: VectorIndexer | None  # None when no OPENAI_API_KEY / sqlite-vec missing


@lru_cache(maxsize=1)
def get_indexers() -> Indexers:
    root = _resolve_data_dir()
    fts_index = SearchIndex(root / ".store" / "search.db")

    def _on_change(evt):
        # FTS first — keyword search is the baseline. Vec is best-effort.
        bundle = get_indexers()
        try:
            bundle.fts.handle_change(evt)
        except Exception:
            log.exception("FTS indexer failed handling change")
        if bundle.vec is not None:
            try:
                bundle.vec.handle_change(evt)
            except Exception:
                log.exception("vec indexer failed handling change")

    store = MarkdownStore(root, on_change=_on_change)
    fts_indexer = SearchIndexer(store=store, index=fts_index)

    fts_added = fts_indexer.bootstrap(force=False)
    if fts_added:
        log.info("FTS bootstrap reindexed %d docs", fts_added)

    vec_indexer = _maybe_build_vec_indexer(root, store)
    # Don't auto-bootstrap embeddings — costs $ and time. Use the admin
    # CLI: `ampersand-admin vec-rebuild`.

    return Indexers(store=store, fts=fts_indexer, vec=vec_indexer)


def _maybe_build_vec_indexer(root: Path, store: MarkdownStore) -> VectorIndexer | None:
    """Construct the vector indexer if both prerequisites are present.

    - OPENAI_API_KEY env var (otherwise we can't embed)
    - sqlite-vec extension loadable (otherwise the index can't open)

    Either missing logs a one-line warning and returns None; the rest of
    the system continues to work with FTS-only search.
    """
    if not os.environ.get("OPENAI_API_KEY", "").strip():
        log.info("OPENAI_API_KEY not set — semantic search disabled")
        return None
    try:
        from ampersand_core.embeddings import Embedder
        embedder = Embedder.from_env()
    except Exception as exc:  # noqa: BLE001
        log.warning("could not build embedder, vec search disabled: %s", exc)
        return None
    try:
        vec_index = VectorIndex(root / ".store" / "vec.db")
    except Exception as exc:  # noqa: BLE001
        log.warning("could not open vec_index, vec search disabled: %s", exc)
        return None
    return VectorIndexer(store=store, index=vec_index, embedder=embedder)


def get_indexer() -> SearchIndexer:
    """Back-compat shim — many call sites still expect the FTS indexer."""
    return get_indexers().fts


def get_store() -> MarkdownStore:
    return get_indexers().store


def get_vec_indexer() -> VectorIndexer | None:
    return get_indexers().vec


def reset_store_cache() -> None:
    """Clear the cached singleton. Tests use this between cases."""
    get_indexers.cache_clear()
