"""SQLite-backed capture job queue.

Built for the extension's clip flow: clicking the extension icon enqueues
a job, returns instantly with a job_id, and a background worker drains the
queue. Survives server restarts (queued + running jobs are picked back up
on the next startup; running jobs are reset to queued so they get retried).

Single-machine FIFO. No multi-worker coordination beyond a transactional
claim. Drop a row into `capture_jobs`, the worker grabs it, runs it through
the same `_dispatch` + `_persist_capture` path that /capture uses, and writes
the result back. The schema is small enough to inspect with `sqlite3` if
something gets stuck.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ampersand_core.store.ids import new_id as _new_doc_id

logger = logging.getLogger(__name__)

_SCHEMA_VERSION = 1

_INIT_SQL = """
CREATE TABLE IF NOT EXISTS jobs (
    id              TEXT PRIMARY KEY,
    url             TEXT NOT NULL,
    persist         INTEGER NOT NULL DEFAULT 1,
    frontmatter     TEXT,
    html            TEXT,
    fallback_title  TEXT,
    status          TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    started_at      TEXT,
    completed_at    TEXT,
    doc_id          TEXT,
    doc_path        TEXT,
    body_hash       TEXT,
    error           TEXT,
    attempts        INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_jobs_status_created
    ON jobs(status, created_at);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _new_id() -> str:
    # Reuse the project's ULID generator so job IDs and doc IDs sort/compare
    # consistently. Job IDs aren't doc IDs — they live in a separate table —
    # but using the same ULID generator keeps the codebase coherent.
    return _new_doc_id()


class JobStore:
    """SQLite-backed capture job store. Thread-safe for our single-worker model
    via a per-instance lock around writes. Reads use a separate connection each
    time, which is fine at the volumes we expect (clip = O(seconds), not O(ms))."""

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

    def enqueue(
        self,
        url: str,
        *,
        persist: bool = True,
        frontmatter: dict[str, Any] | None = None,
        html: str | None = None,
        fallback_title: str | None = None,
    ) -> str:
        """Add a new queued job. Returns the job_id."""
        job_id = _new_id()
        fm_json = json.dumps(frontmatter) if frontmatter else None
        with self._write_lock, self._conn() as conn:
            conn.execute(
                "INSERT INTO jobs"
                "(id, url, persist, frontmatter, html, fallback_title,"
                " status, created_at, attempts)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)",
                (
                    job_id, url, 1 if persist else 0, fm_json,
                    html, fallback_title,
                    STATUS_QUEUED, _now(),
                ),
            )
        return job_id

    def claim_next(self) -> dict | None:
        """Atomically grab the oldest queued job and mark it running. Returns
        None when the queue is empty. The transactional update prevents two
        workers from grabbing the same job (matters if we ever scale beyond
        the single-worker model)."""
        with self._write_lock, self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM jobs WHERE status = ? ORDER BY created_at ASC LIMIT 1",
                (STATUS_QUEUED,),
            ).fetchone()
            if row is None:
                return None
            now = _now()
            conn.execute(
                "UPDATE jobs SET status = ?, started_at = ?, attempts = attempts + 1"
                " WHERE id = ? AND status = ?",
                (STATUS_RUNNING, now, row["id"], STATUS_QUEUED),
            )
            return dict(row)

    def mark_done(self, job_id: str, *, doc_id: str | None, doc_path: str | None, body_hash: str | None) -> None:
        with self._write_lock, self._conn() as conn:
            conn.execute(
                "UPDATE jobs SET status = ?, completed_at = ?, "
                " doc_id = ?, doc_path = ?, body_hash = ?, error = NULL"
                " WHERE id = ?",
                (STATUS_DONE, _now(), doc_id, doc_path, body_hash, job_id),
            )

    def mark_failed(self, job_id: str, error: str) -> None:
        with self._write_lock, self._conn() as conn:
            conn.execute(
                "UPDATE jobs SET status = ?, completed_at = ?, error = ?"
                " WHERE id = ?",
                (STATUS_FAILED, _now(), error[:2000], job_id),
            )

    def reset_running_on_startup(self) -> int:
        """Jobs that were `running` when the server died should be retried.
        Move them back to `queued`. Returns the number reset."""
        with self._write_lock, self._conn() as conn:
            cur = conn.execute(
                "UPDATE jobs SET status = ?, started_at = NULL WHERE status = ?",
                (STATUS_QUEUED, STATUS_RUNNING),
            )
            return cur.rowcount

    # ── reads ───────────────────────────────────────────────────────

    def get(self, job_id: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM jobs WHERE id = ?", (job_id,),
            ).fetchone()
            return dict(row) if row else None

    def list(self, *, status: str | None = None, limit: int = 50) -> list[dict]:
        with self._conn() as conn:
            if status:
                rows = conn.execute(
                    "SELECT * FROM jobs WHERE status = ? ORDER BY created_at DESC LIMIT ?",
                    (status, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            return [dict(r) for r in rows]

    def queue_depth(self) -> int:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE status = ?",
                (STATUS_QUEUED,),
            ).fetchone()
            return row[0] if row else 0
