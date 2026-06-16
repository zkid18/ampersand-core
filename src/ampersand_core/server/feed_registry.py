"""Server-side feed registry — the persistent list of feeds the server
should sync periodically.

Self-hoster's S2/S4 finding: feeds historically lived in `~/.ampersand/state.json`
on whichever client ran `ampersand feed add`. A laptop's `feed add` never
reached the droplet that actually ran the sync timer, so adding feeds was
*always* an SSH-into-the-server task. This module makes the server the single
source of truth: every client (laptop CLI, bot, the web UI later) talks to
`POST /feeds`; `ampersand-feed-sync.timer` pulls the list from the same table.

Shape mirrors `capture_jobs.JobStore` — one SQLite file under the data dir,
write-locked from Python, schema versioned for future migrations.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ampersand_core.store.ids import new_id

_SCHEMA_VERSION = 1

_INIT_SQL = """
CREATE TABLE IF NOT EXISTS feeds (
    id           TEXT PRIMARY KEY,
    url          TEXT NOT NULL UNIQUE,
    name         TEXT,
    tags         TEXT NOT NULL DEFAULT '[]',
    enabled      INTEGER NOT NULL DEFAULT 1,
    created_at   TEXT NOT NULL,
    last_sync_at TEXT,
    last_status  TEXT,
    last_error   TEXT
);

CREATE INDEX IF NOT EXISTS idx_feeds_enabled
    ON feeds(enabled);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class FeedRegistry:
    """SQLite-backed persistent feed list. Thread-safe for our single-process
    deployment via a write lock; reads use short-lived connections."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.Lock()
        with self._conn() as conn:
            conn.executescript(_INIT_SQL)
            row = conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO meta(key, value) VALUES('schema_version', ?)",
                    (str(_SCHEMA_VERSION),),
                )

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path, isolation_level=None, timeout=30)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        return c

    # ── writes ──────────────────────────────────────────────────────

    def add(
        self,
        url: str,
        *,
        name: str | None = None,
        tags: list[str] | None = None,
    ) -> dict:
        """Add a feed. Idempotent on URL — re-adding returns the existing row
        with any name/tags merged in. Returns the row."""
        with self._write_lock, self._conn() as conn:
            existing = conn.execute(
                "SELECT * FROM feeds WHERE url = ?", (url,),
            ).fetchone()
            if existing:
                merged_tags = sorted(
                    set(json.loads(existing["tags"]) or []) | set(tags or [])
                )
                conn.execute(
                    "UPDATE feeds SET name = COALESCE(?, name), tags = ? WHERE id = ?",
                    (name, json.dumps(merged_tags), existing["id"]),
                )
                return self.get(existing["id"])
            fid = new_id()
            conn.execute(
                "INSERT INTO feeds(id, url, name, tags, enabled, created_at) "
                "VALUES (?, ?, ?, ?, 1, ?)",
                (fid, url, name, json.dumps(sorted(set(tags or []))), _now()),
            )
            return self.get(fid)

    def remove(self, feed_id: str) -> bool:
        """Delete by id. Returns True if a row was actually removed."""
        with self._write_lock, self._conn() as conn:
            cur = conn.execute("DELETE FROM feeds WHERE id = ?", (feed_id,))
            return cur.rowcount > 0

    def set_enabled(self, feed_id: str, enabled: bool) -> bool:
        with self._write_lock, self._conn() as conn:
            cur = conn.execute(
                "UPDATE feeds SET enabled = ? WHERE id = ?",
                (1 if enabled else 0, feed_id),
            )
            return cur.rowcount > 0

    def record_sync(
        self,
        feed_id: str,
        *,
        status: str,
        error: str | None = None,
    ) -> None:
        with self._write_lock, self._conn() as conn:
            conn.execute(
                "UPDATE feeds SET last_sync_at = ?, last_status = ?, last_error = ? "
                "WHERE id = ?",
                (_now(), status, (error or None) and error[:1000], feed_id),
            )

    # ── reads ───────────────────────────────────────────────────────

    def get(self, feed_id: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM feeds WHERE id = ?", (feed_id,),
            ).fetchone()
            return _row_to_dict(row) if row else None

    def get_by_url(self, url: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM feeds WHERE url = ?", (url,),
            ).fetchone()
            return _row_to_dict(row) if row else None

    def list(self, *, enabled_only: bool = False) -> list[dict]:
        with self._conn() as conn:
            if enabled_only:
                rows = conn.execute(
                    "SELECT * FROM feeds WHERE enabled = 1 "
                    "ORDER BY created_at ASC"
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM feeds ORDER BY created_at ASC"
                ).fetchall()
            return [_row_to_dict(r) for r in rows]


def _row_to_dict(row: Any) -> dict:
    """Convert a sqlite Row to a plain dict + parse the tags JSON."""
    d = dict(row)
    try:
        d["tags"] = json.loads(d.get("tags") or "[]")
    except (json.JSONDecodeError, TypeError):
        d["tags"] = []
    d["enabled"] = bool(d.get("enabled"))
    return d
