"""Find docs that share the same `source` URL and delete all but the oldest.

Caused-by: during initial setup, feed-sync ran several times before state was
persisted properly, so several PG essays got captured 2-3x with different
ULIDs. This is a one-shot cleanup — going forward state.captured prevents it.

Strategy: walk every .md file, parse frontmatter, group by source URL.
Within each group of size >1, keep the doc with the smallest ULID
(earliest captured) and delete the rest. Deletion goes through
MarkdownStore.delete() so the on_change hook removes the FTS5 entries.
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

from amperstand_core.store import MarkdownStore
from amperstand_core.store import frontmatter as fm


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--root", type=Path, required=True, help="Vault data dir")
    p.add_argument("--dry-run", action="store_true", help="Print plan, don't delete")
    args = p.parse_args()

    store = MarkdownStore(args.root)

    # Group by source URL
    groups: dict[str, list[tuple[str, str]]] = defaultdict(list)  # source → [(doc_id, path)]
    for meta in store.iter_all():
        if not meta.source:
            continue
        groups[meta.source].append((meta.id, meta.path))

    dupes = {src: ids for src, ids in groups.items() if len(ids) > 1}
    total_extra = sum(len(v) - 1 for v in dupes.values())

    print(f"docs scanned:           {sum(len(v) for v in groups.values())}", file=sys.stderr)
    print(f"distinct source URLs:   {len(groups)}", file=sys.stderr)
    print(f"sources with dupes:     {len(dupes)}", file=sys.stderr)
    print(f"extra docs to delete:   {total_extra}", file=sys.stderr)
    print("", file=sys.stderr)

    if not dupes:
        return 0

    deleted = 0
    errors = 0
    for src, entries in dupes.items():
        # Sort by ULID — earliest first (ULIDs are time-prefixed)
        entries.sort(key=lambda x: x[0])
        keep_id, keep_path = entries[0]
        rest = entries[1:]

        if args.dry_run:
            print(f"  KEEP  {keep_id}  {keep_path[:80]}")
            for doc_id, path in rest:
                print(f"  drop  {doc_id}  {path[:80]}")
            print()
            continue

        for doc_id, path in rest:
            try:
                store.delete(doc_id)
                deleted += 1
            except Exception as exc:  # noqa: BLE001
                errors += 1
                print(f"  ERROR deleting {doc_id}: {exc}", file=sys.stderr)

    print(f"deleted: {deleted}  errors: {errors}", file=sys.stderr)
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
