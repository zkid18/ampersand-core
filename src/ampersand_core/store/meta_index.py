"""SQLite metadata sidecar for fast `MarkdownStore.list()` queries.

The vault is filesystem-first (each doc is a .md file with YAML frontmatter),
but listing/filtering by metadata used to require reading and parsing every
file on disk — O(N) for one page. This sidecar mirrors DocMeta into a small
SQLite table and is updated synchronously by `MarkdownStore` on every write,
turning recent-list queries into a single indexed SELECT.

The .md files remain the source of truth: this index can be deleted and
rebuilt at any time by walking the docs dir.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ampersand_core.store.errors import StoreError

SCHEMA_VERSION = 1
_TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

_INIT_SQL = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS docs (
    id            TEXT PRIMARY KEY,
    path          TEXT NOT NULL,
    title         TEXT,
    source        TEXT,
    content_type  TEXT,
    captured_at   TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    tags          TEXT NOT NULL,
    extra         TEXT NOT NULL,
    content_hash  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_docs_updated_id
    ON docs(updated_at DESC, id DESC);
"""


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime(_TS_FORMAT)


def _parse_iso(value: str) -> datetime:
    return datetime.strptime(value, _TS_FORMAT).replace(tzinfo=timezone.utc)


class MetaIndex:
    """SQLite-backed mirror of DocMeta. One row per doc, keyed by id."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._init_schema()

    def _init_schema(self) -> None:
        with self._conn:
            self._conn.executescript(_INIT_SQL)
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO meta(key, value) VALUES('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )
            elif int(row[0]) != SCHEMA_VERSION:
                self.reset()

    # ── writes ──────────────────────────────────────────────────────

    def upsert(self, meta: Any) -> None:
        """Insert-or-replace a row from a DocMeta-like object.

        Accepts duck-typed input so callers don't need to import DocMeta.
        Required attributes: id, path, title, source, content_type,
        captured_at (datetime), updated_at (datetime), tags, extra,
        content_hash.
        """
        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO docs"
                "(id, path, title, source, content_type, captured_at,"
                " updated_at, tags, extra, content_hash)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    meta.id,
                    meta.path,
                    meta.title,
                    meta.source,
                    meta.content_type,
                    _iso(meta.captured_at),
                    _iso(meta.updated_at),
                    json.dumps(list(meta.tags or []), ensure_ascii=False),
                    json.dumps(
                        dict(meta.extra or {}), ensure_ascii=False, default=str
                    ),
                    meta.content_hash,
                ),
            )

    def delete(self, doc_id: str) -> None:
        with self._conn:
            self._conn.execute("DELETE FROM docs WHERE id = ?", (doc_id,))

    def reset(self) -> None:
        with self._conn:
            self._conn.execute("DROP TABLE IF EXISTS docs")
            self._conn.execute("DROP TABLE IF EXISTS meta")
        self._init_schema()

    # ── reads ───────────────────────────────────────────────────────

    def is_empty(self) -> bool:
        return self._conn.execute("SELECT COUNT(*) FROM docs").fetchone()[0] == 0

    def count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM docs").fetchone()[0]

    def list_rows(
        self,
        *,
        since: datetime | None = None,
        cursor_updated_at: datetime | None = None,
        cursor_id: str | None = None,
        limit: int = 100,
    ) -> list[tuple]:
        """Return up to `limit` rows ordered by (updated_at DESC, id DESC).

        Cursor semantics: rows STRICTLY BEFORE (cursor_updated_at, cursor_id)
        in the same ordering. `since` filters by updated_at >= since.
        """
        clauses: list[str] = []
        params: list = []
        if since is not None:
            clauses.append("updated_at >= ?")
            params.append(_iso(since))
        if cursor_updated_at is not None and cursor_id is not None:
            cur = _iso(cursor_updated_at)
            clauses.append("(updated_at < ? OR (updated_at = ? AND id < ?))")
            params.extend([cur, cur, cursor_id])

        sql = (
            "SELECT id, path, title, source, content_type, captured_at,"
            " updated_at, tags, extra, content_hash FROM docs"
        )
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY updated_at DESC, id DESC LIMIT ?"
        params.append(limit)
        try:
            return self._conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError as exc:
            raise StoreError(f"meta_index list failed: {exc}") from exc

    def close(self) -> None:
        self._conn.close()


def row_to_kwargs(row: tuple) -> dict[str, Any]:
    """Turn a list_rows() row into kwargs suitable for DocMeta(...)."""
    (
        doc_id,
        path,
        title,
        source,
        content_type,
        captured_iso,
        updated_iso,
        tags_json,
        extra_json,
        content_hash,
    ) = row
    return {
        "id": doc_id,
        "path": path,
        "title": title,
        "source": source,
        "content_type": content_type,
        "captured_at": _parse_iso(captured_iso),
        "updated_at": _parse_iso(updated_iso),
        "tags": json.loads(tags_json) if tags_json else [],
        "extra": json.loads(extra_json) if extra_json else {},
        "content_hash": content_hash,
    }
