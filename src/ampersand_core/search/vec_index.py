"""SQLite-backed vector index for semantic section search.

Uses the sqlite-vec extension's `vec0` virtual table for cosine-distance
KNN, plus a sidecar metadata table that maps each vector rowid back to
its (doc_id, section_path, section_title, content_hash). Lives in its
own SQLite file under .store/ so it can be rebuilt or torn down without
touching the FTS5 index next door.

Each row in the index represents one section of a doc. Reindexing a doc
deletes all rows whose doc_id matches and re-inserts new ones.
"""

from __future__ import annotations

import hashlib
import json
import logging
import struct
from pathlib import Path
from typing import Sequence

from ampersand_core.embeddings import EMBED_DIM
from ampersand_core.search.errors import SearchError
from ampersand_core.search.models import Section, SearchResult

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 2  # bumped from 1: dim 1536 → 512 (Matryoshka)


def _sqlite_with_extensions():
    """Get a sqlite3 module that supports load_extension.

    macOS' python.org Python ships SQLite without extension loading
    enabled. Homebrew Python and Linux distro Python both ship it on.
    Surface a clear error message rather than a cryptic AttributeError.
    """
    import sqlite3
    if hasattr(sqlite3.Connection, "enable_load_extension"):
        return sqlite3
    raise SearchError(
        "Your sqlite3 module doesn't support load_extension — required for "
        "sqlite-vec. On macOS python.org Python this is disabled at build "
        "time; install Homebrew python (`brew install python@3.13`) or use "
        "Linux Python."
    )


def _vector_to_blob(vector: Sequence[float]) -> bytes:
    """sqlite-vec stores vectors as a tight float32 blob."""
    return struct.pack(f"{len(vector)}f", *vector)


def hash_section(section: Section) -> str:
    h = hashlib.sha256()
    h.update((section.title or "").encode("utf-8"))
    h.update(b"\x00")
    h.update(section.body.encode("utf-8"))
    return f"sha256:{h.hexdigest()}"


def section_text_for_embedding(section: Section) -> str:
    """The string we hand to the embeddings API for a section.

    Prepend the section path so the embedding context includes the
    enclosing doc title + heading hierarchy. Helps disambiguation:
    "## Pricing" inside "Stripe vs Adyen" embeds differently from
    "## Pricing" inside "Why we picked Notion".
    """
    breadcrumb = " > ".join(section.path) if section.path else ""
    head = breadcrumb if breadcrumb else (section.title or "")
    body = section.body.strip()
    if head:
        return f"{head}\n\n{body}"
    return body


class VectorIndex:
    """sqlite-vec backed semantic index over heading-bounded sections."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        sqlite3 = _sqlite_with_extensions()
        try:
            import sqlite_vec
        except ImportError as exc:
            raise SearchError(
                "sqlite-vec not installed. Run: pip install sqlite-vec"
            ) from exc

        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.enable_load_extension(True)
        sqlite_vec.load(self._conn)
        self._conn.enable_load_extension(False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._init_schema()

    def _init_schema(self) -> None:
        with self._conn:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS vec_meta_kv ("
                "  key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            self._conn.execute(
                f"CREATE VIRTUAL TABLE IF NOT EXISTS vec_sections USING vec0("
                f"  embedding float[{EMBED_DIM}])"
            )
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS vec_section_meta ("
                "  rowid INTEGER PRIMARY KEY,"
                "  doc_id TEXT NOT NULL,"
                "  section_title TEXT,"
                "  section_path TEXT,"
                "  content_hash TEXT NOT NULL)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_vec_section_meta_doc_id "
                "ON vec_section_meta(doc_id)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_vec_section_meta_hash "
                "ON vec_section_meta(content_hash)"
            )
            row = self._conn.execute(
                "SELECT value FROM vec_meta_kv WHERE key='schema_version'"
            ).fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO vec_meta_kv(key,value) VALUES('schema_version',?)",
                    (str(SCHEMA_VERSION),),
                )
            elif int(row[0]) != SCHEMA_VERSION:
                self.reset()

    # ── writes ──────────────────────────────────────────────────────

    def existing_section_hashes(self, doc_id: str) -> set[str]:
        """Return the content_hashes already indexed for a given doc.

        Used by the indexer to skip re-embedding sections whose body
        didn't change between updates.
        """
        rows = self._conn.execute(
            "SELECT content_hash FROM vec_section_meta WHERE doc_id = ?",
            (doc_id,),
        ).fetchall()
        return {r[0] for r in rows}

    def upsert_doc_sections(
        self,
        doc_id: str,
        sections: list[Section],
        embeddings: list[list[float]],
    ) -> None:
        """Replace all vectors for this doc atomically.

        `embeddings[i]` corresponds to `sections[i]`. Caller is responsible
        for skipping unchanged sections (see existing_section_hashes).
        """
        if len(sections) != len(embeddings):
            raise SearchError(
                f"sections/embeddings length mismatch: "
                f"{len(sections)} vs {len(embeddings)}"
            )
        with self._conn:
            self._delete_doc_locked(doc_id)
            for section, vec in zip(sections, embeddings):
                if len(vec) != EMBED_DIM:
                    raise SearchError(
                        f"embedding has {len(vec)} dims, expected {EMBED_DIM}"
                    )
                cur = self._conn.execute(
                    "INSERT INTO vec_sections(embedding) VALUES (?)",
                    (_vector_to_blob(vec),),
                )
                self._conn.execute(
                    "INSERT INTO vec_section_meta"
                    "(rowid, doc_id, section_title, section_path, content_hash)"
                    " VALUES(?,?,?,?,?)",
                    (
                        cur.lastrowid,
                        doc_id,
                        section.title or "",
                        json.dumps(section.path, ensure_ascii=False),
                        hash_section(section),
                    ),
                )

    def delete_doc(self, doc_id: str) -> None:
        with self._conn:
            self._delete_doc_locked(doc_id)

    def _delete_doc_locked(self, doc_id: str) -> None:
        self._conn.execute(
            "DELETE FROM vec_sections WHERE rowid IN ("
            " SELECT rowid FROM vec_section_meta WHERE doc_id = ?)",
            (doc_id,),
        )
        self._conn.execute(
            "DELETE FROM vec_section_meta WHERE doc_id = ?", (doc_id,)
        )

    def reset(self) -> None:
        with self._conn:
            self._conn.execute("DROP TABLE IF EXISTS vec_section_meta")
            self._conn.execute("DROP TABLE IF EXISTS vec_sections")
            self._conn.execute("DROP TABLE IF EXISTS vec_meta_kv")
        self._init_schema()

    # ── reads ───────────────────────────────────────────────────────

    def is_empty(self) -> bool:
        return self._conn.execute(
            "SELECT COUNT(*) FROM vec_section_meta"
        ).fetchone()[0] == 0

    def count(self) -> int:
        return self._conn.execute(
            "SELECT COUNT(*) FROM vec_section_meta"
        ).fetchone()[0]

    def doc_centroid(self, doc_id: str) -> list[float] | None:
        """Mean of all section embeddings for a doc, L2-normalized.

        Used by the "find related" endpoint as a single-vector summary
        of the whole doc. Returns None if the doc has no sections in the
        index (e.g. failed to embed).
        """
        rows = self._conn.execute(
            "SELECT v.embedding FROM vec_sections v"
            " JOIN vec_section_meta m ON m.rowid = v.rowid"
            " WHERE m.doc_id = ?",
            (doc_id,),
        ).fetchall()
        if not rows:
            return None
        # Unpack each blob, sum, divide.
        n = EMBED_DIM
        accum = [0.0] * n
        for (blob,) in rows:
            vec = struct.unpack(f"{n}f", blob)
            for i in range(n):
                accum[i] += vec[i]
        count = len(rows)
        mean = [x / count for x in accum]
        # L2-normalize so cosine-style MATCH behaves consistently.
        norm = sum(x * x for x in mean) ** 0.5
        if norm == 0:
            return mean
        return [x / norm for x in mean]

    def search(self, query_embedding: list[float], *, limit: int = 20) -> list[SearchResult]:
        if not 1 <= limit <= 1000:
            raise SearchError("limit out of range (1..1000)")
        if len(query_embedding) != EMBED_DIM:
            raise SearchError(
                f"query embedding has {len(query_embedding)} dims, expected {EMBED_DIM}"
            )
        rows = self._conn.execute(
            "SELECT v.rowid, m.doc_id, m.section_title, m.section_path, v.distance "
            " FROM vec_sections v "
            " JOIN vec_section_meta m ON m.rowid = v.rowid "
            " WHERE v.embedding MATCH ? AND k = ? "
            " ORDER BY v.distance",
            (_vector_to_blob(query_embedding), limit),
        ).fetchall()
        results: list[SearchResult] = []
        for _rowid, doc_id, title, path_json, distance in rows:
            try:
                path = json.loads(path_json) if path_json else []
            except json.JSONDecodeError:
                path = []
            results.append(
                SearchResult(
                    doc_id=doc_id,
                    section_title=title or None,
                    section_path=path,
                    snippet="",  # no snippet from vec; caller can fetch from store
                    score=float(distance),
                )
            )
        return results

    def close(self) -> None:
        self._conn.close()
