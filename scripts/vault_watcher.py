"""Watch the vault dir for direct file changes and keep FTS5 in sync.

Runs alongside ampersand-server when files in /var/lib/ampersand/vault/docs are
edited *outside* the API path — e.g. when an Obsidian client mounts the dir
over sshfs and writes directly. Also keeps the .store/by-id/ index up to date.

Triggers:
- Created / modified .md files     -> parse, upsert sections to FTS5
- Deleted .md files                -> look up doc_id via by-id, remove from FTS5
- Renames (move events)            -> delete old + reindex new

Files without an `id:` frontmatter field are skipped (no doc_id to reference).

Usage:
    /opt/ampersand/venv/bin/python3 scripts/vault_watcher.py /var/lib/ampersand/vault
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from pathlib import Path
from threading import Timer

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from ampersand_core.search import SearchIndex
from ampersand_core.search.parser import parse_sections
from ampersand_core.store import frontmatter as fm
from ampersand_core.store import paths as store_paths
from ampersand_core.store.meta_index import MetaIndex
from ampersand_core.store.store import _meta_from_frontmatter

log = logging.getLogger("ampersand-vault-watcher")

DEBOUNCE_SEC = 1.5  # collapse rapid events from atomic-save sequences


class VaultWatcher(FileSystemEventHandler):
    def __init__(
        self, root: Path, index: SearchIndex, meta_index: MetaIndex
    ) -> None:
        self._root = root
        self._index = index
        self._meta_index = meta_index
        self._idx_dir = root / ".store" / "by-id"
        self._timers: dict[str, Timer] = {}
        self._lock = threading.Lock()

    # ── watchdog event hooks ────────────────────────────────────────

    def on_created(self, event: FileSystemEvent) -> None:
        if not _is_md(event):
            return
        self._schedule(event.src_path, self._reindex_path)

    def on_modified(self, event: FileSystemEvent) -> None:
        if not _is_md(event):
            return
        self._schedule(event.src_path, self._reindex_path)

    def on_deleted(self, event: FileSystemEvent) -> None:
        if not _is_md(event):
            return
        self._schedule(event.src_path, self._handle_delete)

    def on_moved(self, event: FileSystemEvent) -> None:
        # Treat as delete-old + reindex-new
        src = getattr(event, "src_path", "")
        dest = getattr(event, "dest_path", "")
        if src.endswith(".md"):
            self._schedule(src, self._handle_delete)
        if dest.endswith(".md"):
            self._schedule(dest, self._reindex_path)

    # ── debouncer ───────────────────────────────────────────────────

    def _schedule(self, path: str, action) -> None:
        with self._lock:
            if path in self._timers:
                self._timers[path].cancel()
            t = Timer(DEBOUNCE_SEC, action, args=(path,))
            self._timers[path] = t
            t.start()

    # ── handlers ────────────────────────────────────────────────────

    def _reindex_path(self, path_str: str) -> None:
        path = Path(path_str)
        if not path.exists():
            return  # might have been deleted between debounce and run

        try:
            text = path.read_text(encoding="utf-8")
            meta, body = fm.parse(text)
        except Exception as exc:
            log.warning("parse failed for %s: %s", path, exc)
            return

        doc_id = meta.get("id")
        if not doc_id:
            log.info("file without id — skipping: %s", path)
            return

        try:
            sections = parse_sections(body, meta.get("title"))
            self._index.upsert_doc_sections(doc_id, sections)

            # keep by-id pointing at the current location
            rel: str | None = None
            try:
                rel = str(path.resolve().relative_to(self._root.resolve()))
                idx_path = self._idx_dir / f"{doc_id}.path"
                if not idx_path.exists() or idx_path.read_text(encoding="utf-8").strip() != rel:
                    idx_path.parent.mkdir(parents=True, exist_ok=True)
                    idx_path.write_text(rel, encoding="utf-8")
            except Exception as exc:  # noqa: BLE001
                log.warning("by-id update failed for %s: %s", doc_id, exc)

            # mirror frontmatter into the metadata sidecar so list() reflects
            # external edits without going through the API.
            if rel is not None:
                try:
                    self._meta_index.upsert(_meta_from_frontmatter(meta, rel))
                except Exception as exc:  # noqa: BLE001
                    log.warning("meta_index upsert failed for %s: %s", doc_id, exc)

            log.info("reindexed %s (id=%s, sections=%d)", path.name, doc_id, len(sections))
        except Exception as exc:
            log.exception("reindex failed for %s: %s", path, exc)

    def _handle_delete(self, path_str: str) -> None:
        path = Path(path_str)
        try:
            rel = str(path.resolve().relative_to(self._root.resolve()))
        except ValueError:
            log.warning("delete event outside root: %s", path)
            return

        # Walk by-id to find the matching doc_id (file is gone, can't read frontmatter)
        if not self._idx_dir.exists():
            return
        for idx_file in self._idx_dir.glob("*.path"):
            try:
                if idx_file.read_text(encoding="utf-8").strip() == rel:
                    doc_id = idx_file.stem
                    self._index.delete_doc(doc_id)
                    try:
                        self._meta_index.delete(doc_id)
                    except Exception as exc:  # noqa: BLE001
                        log.warning("meta_index delete failed for %s: %s", doc_id, exc)
                    idx_file.unlink(missing_ok=True)
                    log.info("removed %s (id=%s)", rel, doc_id)
                    return
            except Exception:  # noqa: BLE001
                continue
        log.warning("deleted file not found in by-id index: %s", rel)


def _is_md(event: FileSystemEvent) -> bool:
    if event.is_directory:
        return False
    p = getattr(event, "src_path", "")
    if p.endswith(".tmp") or "/.tmp-" in p or p.endswith(".swp"):
        return False
    return p.endswith(".md")


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/var/lib/ampersand/vault")
    docs_dir = root / "docs"
    if not docs_dir.exists():
        log.error("docs dir does not exist: %s", docs_dir)
        return 1

    index = SearchIndex(root / ".store" / "search.db")
    meta_index = MetaIndex(store_paths.meta_index_path(root))
    handler = VaultWatcher(root, index, meta_index)
    observer = Observer()
    observer.schedule(handler, str(docs_dir), recursive=True)
    observer.start()
    log.info("watching %s for direct file changes", docs_dir)

    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        log.info("stopping")
    observer.stop()
    observer.join()
    return 0


if __name__ == "__main__":
    sys.exit(main())
