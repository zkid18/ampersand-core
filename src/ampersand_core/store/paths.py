"""Filesystem path strategy for the markdown store."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from ampersand_core.converter import slug_filename

DOCS_DIR = "docs"
INDEX_DIR = ".store/by-id"
META_INDEX_FILE = ".store/meta.db"


def docs_root(root: Path) -> Path:
    return root / DOCS_DIR


def index_root(root: Path) -> Path:
    return root / INDEX_DIR


def meta_index_path(root: Path) -> Path:
    return root / META_INDEX_FILE


def doc_path(root: Path, doc_id: str, when: datetime, title: str | None) -> Path:
    """Return the absolute path where a new doc should be written."""
    slug = slug_filename(title or "untitled")
    return docs_root(root) / f"{when:%Y}" / f"{when:%m}" / f"{doc_id}-{slug}.md"


def index_path(root: Path, doc_id: str) -> Path:
    return index_root(root) / f"{doc_id}.path"


def asset_dir(root: Path, doc_rel_path: str) -> Path:
    """Per-doc asset folder, a sibling of the doc's .md file.

    For a doc at `docs/2026/05/{id}-slug.md` the assets live in
    `docs/2026/05/{id}-slug/`. Derived from the doc's own path so the
    folder moves and deletes alongside the doc.
    """
    doc_abs = (root / doc_rel_path).resolve()
    return doc_abs.parent / doc_abs.stem


def asset_link(doc_rel_path: str, filename: str) -> str:
    """Markdown-relative link from a doc to one of its assets.

    `docs/2026/05/{id}-slug.md` + `photo-01.jpg`
        -> `./{id}-slug/photo-01.jpg`
    Relative to the .md file's own directory so it resolves in Obsidian
    and any plain markdown viewer that reads from disk.
    """
    stem = Path(doc_rel_path).stem
    return f"./{stem}/{filename}"


def relative_to_root(root: Path, abs_path: Path) -> str:
    return str(abs_path.resolve().relative_to(root.resolve()))


def slugify(title: str) -> str:
    return slug_filename(title)
