"""Force a full FTS5 search index rebuild from the markdown vault on disk.

Runs on the droplet against the same data dir the server uses. Safe to run
while the server is up — SQLite WAL mode handles concurrent reads.

Usage:
    sudo -u amperstand /opt/amperstand/venv/bin/python3 \
        /opt/amperstand/amperstand-server/scripts/reindex_search.py \
        --root /var/lib/amperstand/vault
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from amperstand_core.search import SearchIndex, SearchIndexer
from amperstand_core.store import MarkdownStore


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--root", type=Path, required=True, help="Vault data dir")
    p.add_argument(
        "--reset",
        action="store_true",
        help="Drop and rebuild from scratch (NOT safe with a live server)",
    )
    args = p.parse_args()

    index = SearchIndex(args.root / ".store" / "search.db")
    store = MarkdownStore(args.root)  # no on_change — read-only walk
    indexer = SearchIndexer(store=store, index=index)

    print(f"root:  {args.root}", file=sys.stderr)
    print(f"index: {args.root / '.store' / 'search.db'}", file=sys.stderr)
    print(f"mode:  {'reset+rebuild' if args.reset else 'rebuild-in-place'}", file=sys.stderr)

    started = time.time()
    if args.reset:
        count = indexer.bootstrap(force=True)
    else:
        count = indexer.rebuild_in_place()
    elapsed = time.time() - started

    rate = count / elapsed if elapsed > 0 else 0
    print(f"reindexed {count} docs in {elapsed:.1f}s ({rate:.1f}/s)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
