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


def relative_to_root(root: Path, abs_path: Path) -> str:
    return str(abs_path.resolve().relative_to(root.resolve()))


def slugify(title: str) -> str:
    return slug_filename(title)
