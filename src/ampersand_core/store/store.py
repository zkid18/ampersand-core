"""Filesystem-first markdown store. Source of truth for vault docs."""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Iterator

from ampersand_core.store import frontmatter, hashing, paths
from ampersand_core.store.errors import Conflict, NotFound, StoreError
from ampersand_core.store.events import ChangeEvent, ChangeKind, OnChangeHook
from ampersand_core.store.ids import is_valid, new_id
from ampersand_core.store.meta_index import MetaIndex, row_to_kwargs

_TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
_RESERVED_KEYS = {"id", "captured", "updated", "content_hash"}


@dataclass(frozen=True)
class DocMeta:
    id: str
    title: str | None
    source: str | None
    content_type: str | None
    captured_at: datetime
    updated_at: datetime
    tags: list[str]
    extra: dict[str, Any]
    content_hash: str
    path: str


@dataclass
class VaultDoc:
    meta: DocMeta
    body: str


@dataclass(frozen=True)
class ListPage:
    items: list[DocMeta] = field(default_factory=list)
    next_cursor: str | None = None


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime(_TS_FORMAT)


def _parse_iso(value: str) -> datetime:
    return datetime.strptime(value, _TS_FORMAT).replace(tzinfo=timezone.utc)


def _coerce_captured_at(value: Any) -> datetime | None:
    """Accept either a datetime or an ISO-8601 string, return a UTC datetime."""
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        s = value.rstrip("Z")
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    return None


def _meta_from_frontmatter(meta: dict[str, Any], rel_path: str) -> DocMeta:
    extra = {k: v for k, v in meta.items() if k not in _RESERVED_KEYS and k not in {
        "title", "source", "type", "tags",
    }}
    return DocMeta(
        id=meta["id"],
        title=meta.get("title"),
        source=meta.get("source"),
        content_type=meta.get("type"),
        captured_at=_parse_iso(meta["captured"]),
        updated_at=_parse_iso(meta["updated"]),
        tags=list(meta.get("tags") or []),
        extra=extra,
        content_hash=meta["content_hash"],
        path=rel_path,
    )


def _build_frontmatter(
    *,
    doc_id: str,
    captured_at: datetime,
    updated_at: datetime,
    user_meta: dict[str, Any],
) -> dict[str, Any]:
    """Build the canonical frontmatter dict (without content_hash)."""
    fm: dict[str, Any] = {"id": doc_id}
    if (title := user_meta.get("title")) is not None:
        fm["title"] = title
    if (source := user_meta.get("source")) is not None:
        fm["source"] = source
    if (ctype := user_meta.get("type")) is not None:
        fm["type"] = ctype
    fm["captured"] = _iso(captured_at)
    fm["updated"] = _iso(updated_at)
    if (tags := user_meta.get("tags")) is not None:
        fm["tags"] = list(tags)
    for key, value in user_meta.items():
        if key in _RESERVED_KEYS:
            continue
        if key in {"title", "source", "type", "tags"}:
            continue
        fm[key] = value
    return fm


def _atomic_write(target: Path, data: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(
        dir=target.parent, prefix=".tmp-", suffix=target.suffix, delete=False
    ) as tmp:
        tmp.write(data)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp_name = tmp.name
    os.replace(tmp_name, target)
    try:
        dir_fd = os.open(str(target.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        pass


class MarkdownStore:
    """Filesystem-first markdown vault. Each doc is a .md file with YAML frontmatter."""

    def __init__(self, root: Path, on_change: OnChangeHook | None = None) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        paths.docs_root(self.root).mkdir(parents=True, exist_ok=True)
        paths.index_root(self.root).mkdir(parents=True, exist_ok=True)
        self._on_change = on_change
        self._meta_index = MetaIndex(paths.meta_index_path(self.root))
        self._bootstrap_meta_index_if_needed()

    # ── writes ──────────────────────────────────────────────────────

    def create(self, body: str, frontmatter_in: dict[str, Any] | None = None) -> VaultDoc:
        user_meta = dict(frontmatter_in or {})
        doc_id = new_id()
        now = _now()
        captured_at = _coerce_captured_at(user_meta.get("captured_at")) or now
        user_meta.pop("captured_at", None)

        target = paths.doc_path(self.root, doc_id, captured_at, user_meta.get("title"))
        rel = paths.relative_to_root(self.root, target)

        doc = self._write(
            doc_id=doc_id,
            target=target,
            rel_path=rel,
            captured_at=captured_at,
            updated_at=now,
            user_meta=user_meta,
            body=body,
        )
        self._write_index(doc_id, rel)
        self._fire(ChangeKind.CREATED, doc.meta)
        return doc

    def update(
        self,
        doc_id: str,
        body: str,
        frontmatter_in: dict[str, Any] | None = None,
        if_match: str | None = None,
    ) -> VaultDoc:
        existing = self.get(doc_id)
        if if_match is not None and if_match != existing.meta.content_hash:
            raise Conflict(
                f"if_match mismatch (expected {existing.meta.content_hash})"
            )
        target = self.root / existing.meta.path
        user_meta = dict(frontmatter_in or {})
        now = _now()
        doc = self._write(
            doc_id=doc_id,
            target=target,
            rel_path=existing.meta.path,
            captured_at=existing.meta.captured_at,
            updated_at=now,
            user_meta=user_meta,
            body=body,
        )
        self._fire(ChangeKind.UPDATED, doc.meta)
        return doc

    def upsert(
        self,
        doc_id: str,
        body: str,
        frontmatter_in: dict[str, Any] | None = None,
        if_match: str | None = None,
    ) -> VaultDoc:
        if not is_valid(doc_id):
            raise StoreError(f"invalid doc id: {doc_id!r}")
        try:
            return self.update(doc_id, body, frontmatter_in, if_match=if_match)
        except NotFound:
            if if_match is not None:
                raise Conflict("if_match given but doc does not exist") from None
            user_meta = dict(frontmatter_in or {})
            now = _now()
            captured_at = _coerce_captured_at(user_meta.get("captured_at")) or now
            user_meta.pop("captured_at", None)
            target = paths.doc_path(self.root, doc_id, captured_at, user_meta.get("title"))
            rel = paths.relative_to_root(self.root, target)
            doc = self._write(
                doc_id=doc_id,
                target=target,
                rel_path=rel,
                captured_at=captured_at,
                updated_at=now,
                user_meta=user_meta,
                body=body,
            )
            self._write_index(doc_id, rel)
            self._fire(ChangeKind.CREATED, doc.meta)
            return doc

    def delete(self, doc_id: str, if_match: str | None = None) -> None:
        existing = self.get(doc_id)
        if if_match is not None and if_match != existing.meta.content_hash:
            raise Conflict(
                f"if_match mismatch (expected {existing.meta.content_hash})"
            )
        idx = paths.index_path(self.root, doc_id)
        try:
            idx.unlink()
        except FileNotFoundError:
            pass
        target = self.root / existing.meta.path
        try:
            target.unlink()
        except FileNotFoundError:
            pass
        self._meta_index.delete(doc_id)
        self._fire(
            ChangeKind.DELETED,
            existing.meta,
            override_path=None,
            override_hash=None,
        )

    # ── reads ───────────────────────────────────────────────────────

    def get(self, doc_id: str) -> VaultDoc:
        if not is_valid(doc_id):
            raise NotFound(doc_id)
        idx = paths.index_path(self.root, doc_id)
        if not idx.exists():
            raise NotFound(doc_id)
        rel = idx.read_text(encoding="utf-8").strip()
        path = self.root / rel
        if not path.exists():
            raise NotFound(doc_id)
        return self._read(path, rel)

    def list(
        self,
        since: datetime | None = None,
        cursor: str | None = None,
        limit: int = 100,
        order: str = "updated",
    ) -> ListPage:
        cursor_ts: datetime | None = None
        cursor_id: str | None = None
        if cursor:
            try:
                decoded = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
                cur_iso, cur_id = decoded.split("|", 1)
                cursor_ts = _parse_iso(cur_iso)
                cursor_id = cur_id
            except Exception as exc:
                raise StoreError(f"invalid cursor: {cursor!r}") from exc

        if since is not None:
            since_utc = (
                since.astimezone(timezone.utc)
                if since.tzinfo
                else since.replace(tzinfo=timezone.utc)
            )
        else:
            since_utc = None

        # Fetch limit+1 to detect "has more" without a separate count.
        rows = self._meta_index.list_rows(
            since=since_utc,
            cursor_ts=cursor_ts,
            cursor_id=cursor_id,
            limit=limit + 1,
            order=order,
        )
        has_more = len(rows) > limit
        rows = rows[:limit]
        page_items = [DocMeta(**row_to_kwargs(r)) for r in rows]
        next_cursor: str | None = None
        if has_more and page_items:
            last = page_items[-1]
            ts = last.captured_at if order == "captured" else last.updated_at
            raw = f"{_iso(ts)}|{last.id}".encode("utf-8")
            next_cursor = base64.urlsafe_b64encode(raw).decode("ascii")
        return ListPage(items=page_items, next_cursor=next_cursor)

    def iter_all(self) -> Iterator[DocMeta]:
        docs_dir = paths.docs_root(self.root)
        if not docs_dir.exists():
            return
        for path in docs_dir.rglob("*.md"):
            if path.name.startswith(".tmp-"):
                continue
            try:
                rel = paths.relative_to_root(self.root, path)
                yield self._read(path, rel).meta
            except StoreError:
                continue

    # ── internals ───────────────────────────────────────────────────

    def _write(
        self,
        *,
        doc_id: str,
        target: Path,
        rel_path: str,
        captured_at: datetime,
        updated_at: datetime,
        user_meta: dict[str, Any],
        body: str,
    ) -> VaultDoc:
        if not body.endswith("\n"):
            body = body + "\n"
        fm = _build_frontmatter(
            doc_id=doc_id,
            captured_at=captured_at,
            updated_at=updated_at,
            user_meta=user_meta,
        )
        # First pass: serialize without content_hash to derive it.
        provisional = frontmatter.dump(fm, body).encode("utf-8")
        h = hashing.compute_hash(provisional)
        fm["content_hash"] = h
        final_bytes = frontmatter.dump(fm, body).encode("utf-8")
        _atomic_write(target, final_bytes)
        meta = _meta_from_frontmatter(fm, rel_path)
        self._meta_index.upsert(meta)
        return VaultDoc(meta=meta, body=body)

    def _read(self, path: Path, rel_path: str) -> VaultDoc:
        text = path.read_text(encoding="utf-8")
        meta, body = frontmatter.parse(text)
        return VaultDoc(meta=_meta_from_frontmatter(meta, rel_path), body=body)

    def _write_index(self, doc_id: str, rel_path: str) -> None:
        idx = paths.index_path(self.root, doc_id)
        idx.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(idx, rel_path.encode("utf-8"))

    def _bootstrap_meta_index_if_needed(self) -> int:
        """Backfill meta_index from .md files when the sidecar is empty.

        Runs at most once per fresh deploy: after that every write keeps the
        index in sync. External edits captured by `vault_watcher.py` keep it
        current. If both stays empty, no-op.
        """
        if not self._meta_index.is_empty():
            return 0
        count = 0
        for meta in self.iter_all():
            self._meta_index.upsert(meta)
            count += 1
        return count

    def rebuild_meta_index(self) -> int:
        """Drop and rebuild the metadata sidecar from .md files. Admin op."""
        self._meta_index.reset()
        count = 0
        for meta in self.iter_all():
            self._meta_index.upsert(meta)
            count += 1
        return count

    def _fire(
        self,
        kind: ChangeKind,
        meta: DocMeta,
        override_path: str | None = ...,  # type: ignore[assignment]
        override_hash: str | None = ...,  # type: ignore[assignment]
    ) -> None:
        if self._on_change is None:
            return
        path = meta.path if override_path is ... else override_path
        chash = meta.content_hash if override_hash is ... else override_hash
        self._on_change(
            ChangeEvent(
                kind=kind,
                id=meta.id,
                path=path,
                content_hash=chash,
                occurred_at=_now(),
            )
        )


def recompute_hash(path: Path) -> str:
    """Re-derive the content_hash for a file on disk.

    Used by integrity checks: re-runs the same hashing strategy used at write
    time (canonical dump of frontmatter without `content_hash`, plus body) and
    returns the resulting `sha256:...` value. Compare against the file's
    stored `content_hash` field to detect tampering or silent corruption.
    """
    text = path.read_text(encoding="utf-8")
    meta, body = frontmatter.parse(text)
    meta_no_hash = {k: v for k, v in meta.items() if k != "content_hash"}
    provisional = frontmatter.dump(meta_no_hash, body).encode("utf-8")
    return hashing.compute_hash(provisional)
