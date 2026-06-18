"""SQLite FTS5 full-text index for vault sections."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from pathlib import Path
from typing import Iterable

from amperstand_core.search.errors import SearchError
from amperstand_core.search.models import Section, SearchResult

SCHEMA_VERSION = 1

_INIT_SQL = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS sections USING fts5(
    doc_id        UNINDEXED,
    section_path  UNINDEXED,
    section_title,
    section_body,
    content_hash  UNINDEXED,
    tokenize='unicode61 remove_diacritics 2'
);

CREATE TABLE IF NOT EXISTS doc_sections (
    doc_id TEXT NOT NULL,
    rowid  INTEGER NOT NULL,
    PRIMARY KEY (doc_id, rowid)
);

CREATE INDEX IF NOT EXISTS doc_sections_doc_id ON doc_sections(doc_id);
"""

# Chars that have (or can have) meaning in FTS5 query syntax. We strip them
# from each whitespace-/dash-split token before passing to MATCH. The original
# regex covered the common ones (quotes, parens, *, +, :, ^); `,` `{` `}` were
# added after a real prod incident (query "brazilian funk, miami bass" 400'd
# with `fts5: syntax error near ","`). `?` `!` `~` `<` `>` `=` `&` `|` `\` are
# defensive — none should appear in a natural-language search, and any of them
# can surprise the FTS5 parser depending on context.
_FTS_OPERATOR_RE = re.compile(r'["\'()*+:^,{}?!~<>=&|\\]')


class SearchIndex:
    """SQLite FTS5 index over heading-bounded markdown sections."""

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
            existing = self._conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()
            if existing is None:
                self._conn.execute(
                    "INSERT INTO meta(key, value) VALUES('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )
            elif int(existing[0]) != SCHEMA_VERSION:
                # Version mismatch — drop and rebuild on next bootstrap.
                self.reset()

    # ── writes ──────────────────────────────────────────────────────

    def upsert_doc_sections(self, doc_id: str, sections: list[Section]) -> None:
        """Replace all sections for this doc atomically."""
        with self._conn:
            self._conn.execute(
                "DELETE FROM sections WHERE rowid IN ("
                "  SELECT rowid FROM doc_sections WHERE doc_id = ?"
                ")",
                (doc_id,),
            )
            self._conn.execute("DELETE FROM doc_sections WHERE doc_id = ?", (doc_id,))
            for section in sections:
                content_hash = _hash_section(section)
                cur = self._conn.execute(
                    "INSERT INTO sections "
                    "(doc_id, section_path, section_title, section_body, content_hash) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        doc_id,
                        json.dumps(section.path, ensure_ascii=False),
                        section.title or "",
                        section.body,
                        content_hash,
                    ),
                )
                self._conn.execute(
                    "INSERT INTO doc_sections(doc_id, rowid) VALUES (?, ?)",
                    (doc_id, cur.lastrowid),
                )

    def delete_doc(self, doc_id: str) -> None:
        with self._conn:
            self._conn.execute(
                "DELETE FROM sections WHERE rowid IN ("
                "  SELECT rowid FROM doc_sections WHERE doc_id = ?"
                ")",
                (doc_id,),
            )
            self._conn.execute("DELETE FROM doc_sections WHERE doc_id = ?", (doc_id,))

    # ── reads ───────────────────────────────────────────────────────

    def search(
        self, query: str, *, limit: int = 20, mode: str = "fts"
    ) -> list[SearchResult]:
        if not query or not query.strip():
            raise SearchError("query cannot be empty")
        if mode not in {"fts", "substring", "any"}:
            raise SearchError(f"unknown mode: {mode!r}")
        if not 1 <= limit <= 1000:
            raise SearchError("limit out of range (1..1000)")

        match_query = _build_match(query, mode)

        try:
            # FTS5 bm25 weights are positional, one per column INCLUDING
            # UNINDEXED ones (their weight is ignored but the slot is needed).
            # Column order: doc_id, section_path, section_title, section_body,
            # content_hash. We weight section_title 5× to make title hits
            # dominate over body-density hits — a query like "how to make
            # wealth" should rank an essay literally titled that above an
            # email that just mentions Paul Graham a lot.
            rows = self._conn.execute(
                "SELECT doc_id, section_path, section_title, "
                "       snippet(sections, 3, '<mark>', '</mark>', '…', 32) AS snip, "
                "       bm25(sections, 0.0, 0.0, 5.0, 1.0, 0.0) AS score "
                "FROM sections "
                "WHERE sections MATCH ? "
                "ORDER BY score "
                "LIMIT ?",
                (match_query, limit),
            ).fetchall()
        except sqlite3.OperationalError as exc:
            raise SearchError(f"invalid query: {exc}") from exc

        results: list[SearchResult] = []
        for doc_id, path_json, title, snip, score in rows:
            try:
                path = json.loads(path_json) if path_json else []
            except json.JSONDecodeError:
                path = []
            results.append(
                SearchResult(
                    doc_id=doc_id,
                    section_title=title or None,
                    section_path=path,
                    snippet=snip or "",
                    score=float(score),
                )
            )
        return results

    def is_empty(self) -> bool:
        row = self._conn.execute("SELECT COUNT(*) FROM doc_sections").fetchone()
        return (row[0] if row else 0) == 0

    def schema_version(self) -> int:
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()
        return int(row[0]) if row else 0

    def reset(self) -> None:
        with self._conn:
            self._conn.execute("DROP TABLE IF EXISTS sections")
            self._conn.execute("DROP TABLE IF EXISTS doc_sections")
            self._conn.execute("DROP TABLE IF EXISTS meta")
        self._init_schema()

    def close(self) -> None:
        self._conn.close()


# ── helpers ─────────────────────────────────────────────────────────


def _hash_section(section: Section) -> str:
    h = hashlib.sha256()
    h.update((section.title or "").encode("utf-8"))
    h.update(b"\x00")
    h.update(section.body.encode("utf-8"))
    return f"sha256:{h.hexdigest()}"


_STOPWORDS = frozenset({
    "a", "an", "and", "or", "the", "of", "in", "on", "at", "to", "for", "is",
    "are", "be", "with", "as", "by", "from", "this", "that", "it", "i", "you",
})


def _build_match(query: str, mode: str) -> str:
    query = query.strip()
    if mode == "fts":
        # Power-user mode — pass through verbatim, supports `foo OR bar`,
        # `"phrase"`, `prefix*`, etc.
        return query

    # Tokenize on whitespace AND on hyphens. FTS5 treats `-` as NOT and
    # `:` as column-scope, so a token like "go-to-market" gets parsed as
    # `go - to:market` and 400s. Splitting on the dash up front lets each
    # chunk become its own bare token, which matches user intent anyway.
    raw_tokens = [t for t in re.split(r"[\s\-‐-―]+", query) if t.strip()]
    cleaned: list[str] = []
    for raw in raw_tokens:
        c = _FTS_OPERATOR_RE.sub(" ", raw).strip()
        if not c:
            continue
        cleaned.append(c)

    if mode == "any":
        # OR-join every non-stopword token. Bare-word matches let FTS5 do its
        # default (prefix+stem) behavior; stopwords filtered to avoid noise.
        meaningful = [c for c in cleaned if c.lower() not in _STOPWORDS] or cleaned
        if not meaningful:
            raise SearchError("query has no usable tokens")
        return " OR ".join(meaningful)

    # substring mode — quote each token so FTS5 treats it as a literal phrase.
    if not cleaned:
        raise SearchError("query has no usable tokens")
    return " ".join(f'"{c}"' for c in cleaned)


def iter_doc_ids(index: SearchIndex) -> Iterable[str]:
    """Distinct doc_ids currently in the index. Useful for diagnostics."""
    cur = index._conn.execute("SELECT DISTINCT doc_id FROM doc_sections")
    for row in cur.fetchall():
        yield row[0]
