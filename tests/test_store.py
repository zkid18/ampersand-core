from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ampersand_core.store import (
    ChangeEvent,
    ChangeKind,
    Conflict,
    MarkdownStore,
    NotFound,
    StoreError,
)
from ampersand_core.store import frontmatter


def make_store(tmp_path: Path) -> MarkdownStore:
    return MarkdownStore(tmp_path)


def test_create_writes_file_and_index(tmp_path: Path) -> None:
    s = make_store(tmp_path)
    doc = s.create("# title\n\nbody", {"title": "Hello World", "tags": ["a"]})

    assert doc.meta.id
    assert doc.meta.content_hash.startswith("sha256:")
    assert doc.meta.path.startswith("docs/")
    on_disk = tmp_path / doc.meta.path
    assert on_disk.exists()
    idx = tmp_path / ".store" / "by-id" / f"{doc.meta.id}.path"
    assert idx.exists()
    assert idx.read_text(encoding="utf-8") == doc.meta.path


def test_get_round_trips(tmp_path: Path) -> None:
    s = make_store(tmp_path)
    body = "# hello\n\nsome body with **markdown**\n"
    doc = s.create(body, {"title": "RT", "tags": ["x", "y"]})
    again = s.get(doc.meta.id)
    assert again.body == body
    assert again.meta.id == doc.meta.id
    assert again.meta.content_hash == doc.meta.content_hash
    assert again.meta.tags == ["x", "y"]


def test_update_with_correct_if_match(tmp_path: Path) -> None:
    s = make_store(tmp_path)
    doc = s.create("v1\n", {"title": "T"})
    time.sleep(1)  # bump updated_at to next second
    updated = s.update(doc.meta.id, "v2\n", if_match=doc.meta.content_hash)
    assert updated.meta.content_hash != doc.meta.content_hash
    assert updated.meta.updated_at >= doc.meta.updated_at
    assert s.get(doc.meta.id).body == "v2\n"


def test_update_with_stale_if_match_raises_conflict(tmp_path: Path) -> None:
    s = make_store(tmp_path)
    doc = s.create("v1\n", {"title": "T"})
    s.update(doc.meta.id, "v2\n", if_match=doc.meta.content_hash)
    with pytest.raises(Conflict):
        s.update(doc.meta.id, "v3\n", if_match=doc.meta.content_hash)


def test_delete_removes_file_and_index(tmp_path: Path) -> None:
    s = make_store(tmp_path)
    doc = s.create("body\n", {"title": "T"})
    s.delete(doc.meta.id)
    assert not (tmp_path / doc.meta.path).exists()
    assert not (tmp_path / ".store" / "by-id" / f"{doc.meta.id}.path").exists()
    with pytest.raises(NotFound):
        s.get(doc.meta.id)


def test_delete_with_stale_if_match_raises_conflict(tmp_path: Path) -> None:
    s = make_store(tmp_path)
    doc = s.create("body\n", {"title": "T"})
    s.update(doc.meta.id, "body2\n", if_match=doc.meta.content_hash)
    with pytest.raises(Conflict):
        s.delete(doc.meta.id, if_match=doc.meta.content_hash)


def test_list_orders_by_updated_at_desc(tmp_path: Path) -> None:
    s = make_store(tmp_path)
    a = s.create("a\n", {"title": "A"})
    time.sleep(1)
    b = s.create("b\n", {"title": "B"})
    time.sleep(1)
    c = s.create("c\n", {"title": "C"})
    page = s.list()
    assert [m.id for m in page.items] == [c.meta.id, b.meta.id, a.meta.id]
    assert page.next_cursor is None


def test_list_pagination_round_trips(tmp_path: Path) -> None:
    s = make_store(tmp_path)
    ids = []
    for i in range(5):
        ids.append(s.create(f"x{i}\n", {"title": f"X{i}"}).meta.id)
        time.sleep(1)

    p1 = s.list(limit=2)
    assert len(p1.items) == 2
    assert p1.next_cursor is not None
    p2 = s.list(limit=2, cursor=p1.next_cursor)
    assert len(p2.items) == 2
    assert p2.next_cursor is not None
    p3 = s.list(limit=2, cursor=p2.next_cursor)
    assert len(p3.items) == 1
    assert p3.next_cursor is None

    seen = [m.id for m in p1.items + p2.items + p3.items]
    assert sorted(seen) == sorted(ids)


def test_list_since_filters(tmp_path: Path) -> None:
    s = make_store(tmp_path)
    s.create("old\n", {"title": "old"})
    time.sleep(1)
    cutoff = datetime.now(timezone.utc)
    time.sleep(1)
    new = s.create("new\n", {"title": "new"})
    page = s.list(since=cutoff)
    assert [m.id for m in page.items] == [new.meta.id]


def test_iter_all_yields_every_doc(tmp_path: Path) -> None:
    s = make_store(tmp_path)
    ids = {s.create(f"b{i}\n", {"title": f"T{i}"}).meta.id for i in range(3)}
    seen = {m.id for m in s.iter_all()}
    assert seen == ids


def test_on_change_fires_for_lifecycle(tmp_path: Path) -> None:
    events: list[ChangeEvent] = []
    s = MarkdownStore(tmp_path, on_change=events.append)
    doc = s.create("a\n", {"title": "T"})
    upd = s.update(doc.meta.id, "b\n", if_match=doc.meta.content_hash)
    s.delete(doc.meta.id)
    kinds = [e.kind for e in events]
    assert kinds == [ChangeKind.CREATED, ChangeKind.UPDATED, ChangeKind.DELETED]
    assert events[0].content_hash == doc.meta.content_hash
    assert events[1].content_hash == upd.meta.content_hash
    assert events[2].content_hash is None
    assert events[2].path is None


def test_non_ascii_round_trips(tmp_path: Path) -> None:
    s = make_store(tmp_path)
    body = "# héllo 🌍\n\n中文 — émoji test\n"
    doc = s.create(body, {"title": "Über résumé", "tags": ["ümlaut", "中文"]})
    again = s.get(doc.meta.id)
    assert again.body == body
    assert again.meta.title == "Über résumé"
    assert again.meta.tags == ["ümlaut", "中文"]


def test_frontmatter_parse_rejects_missing_fence() -> None:
    with pytest.raises(StoreError):
        frontmatter.parse("no frontmatter here\n")


def test_get_invalid_id_returns_not_found(tmp_path: Path) -> None:
    s = make_store(tmp_path)
    with pytest.raises(NotFound):
        s.get("not-a-ulid")


def test_upsert_creates_when_missing(tmp_path: Path) -> None:
    from ampersand_core.store.ids import new_id

    s = make_store(tmp_path)
    new = new_id()
    doc = s.upsert(new, "first\n", {"title": "via upsert"})
    assert doc.meta.id == new
    assert s.get(new).body == "first\n"


def test_extra_frontmatter_keys_round_trip(tmp_path: Path) -> None:
    s = make_store(tmp_path)
    doc = s.create("body\n", {"title": "T", "author": "Jane", "custom_key": 42})
    again = s.get(doc.meta.id)
    assert again.meta.extra.get("author") == "Jane"
    assert again.meta.extra.get("custom_key") == 42


def test_recompute_hash_matches_stored(tmp_path: Path) -> None:
    from ampersand_core.store import recompute_hash

    s = make_store(tmp_path)
    doc = s.create("# body\n\nstuff\n", {"title": "T", "tags": ["x"]})
    on_disk = tmp_path / doc.meta.path
    assert recompute_hash(on_disk) == doc.meta.content_hash


def test_recompute_hash_detects_tamper(tmp_path: Path) -> None:
    from ampersand_core.store import recompute_hash

    s = make_store(tmp_path)
    doc = s.create("v1\n", {"title": "T"})
    on_disk = tmp_path / doc.meta.path
    text = on_disk.read_text(encoding="utf-8")
    on_disk.write_text(text.replace("v1", "v2-tampered"), encoding="utf-8")
    assert recompute_hash(on_disk) != doc.meta.content_hash
