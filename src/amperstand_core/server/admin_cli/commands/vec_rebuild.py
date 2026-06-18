"""amperstand-admin vec-rebuild — backfill the semantic search index.

Walks every doc in the vault, parses sections, embeds them via the
configured embeddings provider (OpenAI by default), and inserts vectors
into .store/vec.db. Skips docs whose section content_hashes already
match (incremental).

Use --force to reset the vector index first and re-embed everything.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from amperstand_core.embeddings import EMBED_DIM, Embedder
from amperstand_core.search import VectorIndex, VectorIndexer
from amperstand_core.search.parser import parse_sections
from amperstand_core.search.vec_index import (
    hash_section,
    section_text_for_embedding,
)
from amperstand_core.store import MarkdownStore

from amperstand_core.server.admin_cli.config import AdminConfig

log = logging.getLogger(__name__)


def run(config: AdminConfig, *, force: bool = False, dry_run: bool = False) -> None:
    data_dir = Path(config.data_dir)
    if not data_dir.exists():
        raise SystemExit(f"data dir not found: {data_dir}")

    if not config.openai_api_key:
        raise SystemExit("OPENAI_API_KEY not set in env-file. Cannot embed.")

    print(f"Vector rebuild starting (dim={EMBED_DIM}, force={force}, dry_run={dry_run})")
    print(f"Data dir: {data_dir}")

    # Build a store with no on_change hook — we don't want this run to re-fire
    # the live indexers (they'd duplicate work). We're populating the vec index
    # directly via the indexer.
    store = MarkdownStore(data_dir, on_change=None)

    if dry_run:
        # Just count work to be done.
        total = sum(1 for _ in store.iter_all())
        print(f"would scan {total} docs")
        return

    import os
    os.environ["OPENAI_API_KEY"] = config.openai_api_key
    embedder = Embedder.from_env()
    vec_index = VectorIndex(data_dir / ".store" / "vec.db")
    indexer = VectorIndexer(store=store, index=vec_index, embedder=embedder)

    if force:
        print("--force: dropping existing vec index")
        vec_index.reset()

    indexed = 0
    skipped_unchanged = 0
    failed = 0
    started = time.time()
    embedded_sections = 0

    for meta in store.iter_all():
        try:
            doc = store.get(meta.id)
        except Exception:
            failed += 1
            log.exception("could not load doc %s", meta.id)
            continue

        sections = parse_sections(doc.body, doc.meta.title)
        if not sections:
            vec_index.delete_doc(meta.id)
            continue

        new_hashes = {hash_section(s) for s in sections}
        existing = vec_index.existing_section_hashes(meta.id)
        if new_hashes == existing and existing:
            skipped_unchanged += 1
            continue

        try:
            texts = [section_text_for_embedding(s) for s in sections]
            embeddings = embedder.embed_batch(texts)
            vec_index.upsert_doc_sections(meta.id, sections, embeddings)
            indexed += 1
            embedded_sections += len(sections)
        except Exception:
            failed += 1
            log.exception("embed failed for doc %s", meta.id)
            continue

        if indexed % 50 == 0 and indexed > 0:
            elapsed = time.time() - started
            rate = indexed / elapsed if elapsed else 0
            print(
                f"  indexed={indexed} skipped_unchanged={skipped_unchanged} "
                f"failed={failed} sections={embedded_sections} "
                f"rate={rate:.1f} docs/s"
            )

    elapsed = time.time() - started
    print(
        f"\nDone in {elapsed:.0f}s. indexed={indexed} skipped_unchanged={skipped_unchanged} "
        f"failed={failed} sections={embedded_sections}"
    )
    print(f"vec index now holds {vec_index.count()} section vectors")
