"""Tests for the MetaIndex SQLite sidecar and store integration."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from amperstand_core.store import MarkdownStore, paths
from amperstand_core.store.meta_index import MetaIndex, row_to_kwargs


def test_upsert_and_list_rows(tmp_path: Path) -> None:
    idx = MetaIndex(tmp_path / "meta.db")

    class M:  # duck-typed DocMeta
        id = "01J0AAAAAAAAAAAAAAAAAAAAAA"
        path = "docs/2026/05/foo.md"
        title = "Hello"
        source = None
        content_type = None
        captured_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
        updated_at = datetime(2026, 1, 2, tzinfo=timezone.utc)
        tags = ["a", "b"]
        extra = {"author": "x"}
        content_hash = "sha256:abc"
        body_hash = "sha256:def"

    idx.upsert(M)
    rows = idx.list_rows(limit=10)
    assert len(rows) == 1
    kwargs = row_to_kwargs(rows[0])
    assert kwargs["id"] == M.id
    assert kwargs["title"] == "Hello"
    assert kwargs["tags"] == ["a", "b"]
    assert kwargs["extra"] == {"author": "x"}
    assert kwargs["captured_at"] == M.captured_at
    assert kwargs["updated_at"] == M.updated_at


def test_list_rows_orders_by_updated_then_id_desc(tmp_path: Path) -> None:
    idx = MetaIndex(tmp_path / "meta.db")
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def m(doc_id: str, ts: datetime) -> object:
        return type("M", (), dict(
            id=doc_id, path=f"docs/{doc_id}.md", title=None, source=None,
            content_type=None, captured_at=ts, updated_at=ts,
            tags=[], extra={}, content_hash="h", body_hash="bh",
        ))

    idx.upsert(m("01AAAA", base.replace(hour=1)))
    idx.upsert(m("01BBBB", base.replace(hour=2)))
    idx.upsert(m("01CCCC", base.replace(hour=2)))  # tie on updated_at
    rows = idx.list_rows(limit=10)
    ids = [row_to_kwargs(r)["id"] for r in rows]
    assert ids == ["01CCCC", "01BBBB", "01AAAA"]


def test_list_rows_cursor_pagination(tmp_path: Path) -> None:
    idx = MetaIndex(tmp_path / "meta.db")
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for i in range(5):
        idx.upsert(type("M", (), dict(
            id=f"01{i:024d}", path=f"docs/{i}.md", title=None, source=None,
            content_type=None, captured_at=base, updated_at=base.replace(hour=i),
            tags=[], extra={}, content_hash="h", body_hash="bh",
        )))

    page1 = idx.list_rows(limit=2)
    assert len(page1) == 2
    last = row_to_kwargs(page1[-1])
    page2 = idx.list_rows(
        cursor_ts=last["updated_at"],
        cursor_id=last["id"],
        limit=10,
    )
    assert len(page2) == 3
    seen = [row_to_kwargs(r)["id"] for r in page1 + page2]
    assert len(set(seen)) == 5  # no duplicates across pages


def test_list_rows_order_by_captured(tmp_path: Path) -> None:
    """captured_at sort is independent of updated_at — a doc captured long
    ago but updated yesterday should sort low under order=captured."""
    idx = MetaIndex(tmp_path / "meta.db")
    old_capture = datetime(2024, 1, 1, tzinfo=timezone.utc)
    new_capture = datetime(2026, 1, 1, tzinfo=timezone.utc)
    yesterday = datetime(2026, 5, 1, tzinfo=timezone.utc)

    idx.upsert(type("M", (), dict(  # captured long ago, updated yesterday
        id="01OLD", path="docs/old.md", title=None, source=None,
        content_type=None, captured_at=old_capture, updated_at=yesterday,
        tags=[], extra={}, content_hash="h", body_hash="bh",
    )))
    idx.upsert(type("M", (), dict(  # captured recently, untouched
        id="01NEW", path="docs/new.md", title=None, source=None,
        content_type=None, captured_at=new_capture, updated_at=new_capture,
        tags=[], extra={}, content_hash="h", body_hash="bh",
    )))

    by_updated = [row_to_kwargs(r)["id"] for r in idx.list_rows(order="updated")]
    by_captured = [row_to_kwargs(r)["id"] for r in idx.list_rows(order="captured")]
    assert by_updated == ["01OLD", "01NEW"]    # updated yesterday wins
    assert by_captured == ["01NEW", "01OLD"]   # captured recently wins


def test_list_rows_unknown_order_raises(tmp_path: Path) -> None:
    idx = MetaIndex(tmp_path / "meta.db")
    with pytest.raises(Exception):
        idx.list_rows(order="bogus")


def test_list_rows_since_filter(tmp_path: Path) -> None:
    idx = MetaIndex(tmp_path / "meta.db")
    early = datetime(2026, 1, 1, tzinfo=timezone.utc)
    late = datetime(2026, 6, 1, tzinfo=timezone.utc)
    idx.upsert(type("M", (), dict(
        id="01OLD", path="docs/old.md", title=None, source=None,
        content_type=None, captured_at=early, updated_at=early,
        tags=[], extra={}, content_hash="h", body_hash="bh",
    )))
    idx.upsert(type("M", (), dict(
        id="01NEW", path="docs/new.md", title=None, source=None,
        content_type=None, captured_at=late, updated_at=late,
        tags=[], extra={}, content_hash="h", body_hash="bh",
    )))
    cutoff = datetime(2026, 3, 1, tzinfo=timezone.utc)
    rows = idx.list_rows(since=cutoff, limit=10)
    assert [row_to_kwargs(r)["id"] for r in rows] == ["01NEW"]


def test_delete_removes_row(tmp_path: Path) -> None:
    idx = MetaIndex(tmp_path / "meta.db")
    ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
    idx.upsert(type("M", (), dict(
        id="01ZZ", path="docs/z.md", title=None, source=None,
        content_type=None, captured_at=ts, updated_at=ts,
        tags=[], extra={}, content_hash="h", body_hash="bh",
    )))
    assert idx.count() == 1
    idx.delete("01ZZ")
    assert idx.is_empty()


# ── store integration ──────────────────────────────────────────────


def test_store_writes_populate_meta_index(tmp_path: Path) -> None:
    s = MarkdownStore(tmp_path)
    doc = s.create("body\n", {"title": "T"})
    idx = MetaIndex(paths.meta_index_path(tmp_path))
    rows = idx.list_rows(limit=10)
    assert len(rows) == 1
    assert row_to_kwargs(rows[0])["id"] == doc.meta.id


def test_store_delete_removes_from_meta_index(tmp_path: Path) -> None:
    s = MarkdownStore(tmp_path)
    doc = s.create("body\n", {"title": "T"})
    s.delete(doc.meta.id)
    idx = MetaIndex(paths.meta_index_path(tmp_path))
    assert idx.is_empty()


def test_store_bootstraps_meta_index_from_existing_files(tmp_path: Path) -> None:
    """If the meta_index sidecar is missing/empty but .md files exist,
    `MarkdownStore.__init__` backfills the index from disk."""
    # First store: write 3 docs through the API.
    s1 = MarkdownStore(tmp_path)
    ids = []
    for i in range(3):
        ids.append(s1.create(f"body{i}\n", {"title": f"T{i}"}).meta.id)
        time.sleep(1.01)  # distinct updated_at seconds

    # Simulate "fresh deploy": delete just the sidecar, keep .md files.
    sidecar = paths.meta_index_path(tmp_path)
    sidecar.unlink()

    # New store should backfill.
    s2 = MarkdownStore(tmp_path)
    page = s2.list()
    assert {m.id for m in page.items} == set(ids)


def test_rebuild_meta_index_admin_op(tmp_path: Path) -> None:
    s = MarkdownStore(tmp_path)
    s.create("a\n", {"title": "A"})
    s.create("b\n", {"title": "B"})
    # Manually corrupt: delete a row
    idx = MetaIndex(paths.meta_index_path(tmp_path))
    idx.reset()
    assert idx.is_empty()
    # rebuild_meta_index should restore both rows from disk
    n = s.rebuild_meta_index()
    assert n == 2
    page = s.list()
    assert len(page.items) == 2
