from __future__ import annotations

import logging
import time
from pathlib import Path

import pytest

from ampersand_core.search import (
    SearchError,
    SearchIndex,
    SearchIndexer,
    parse_sections,
)
from ampersand_core.search.parser import Section
from ampersand_core.store import ChangeEvent, ChangeKind, MarkdownStore


# ── parse_sections ───────────────────────────────────────────────────


def test_empty_body_yields_one_preamble() -> None:
    secs = parse_sections("")
    assert len(secs) == 1
    assert secs[0].title is None
    assert secs[0].level == 0
    assert secs[0].body == ""
    assert secs[0].path == []


def test_preamble_then_headings_nest_correctly() -> None:
    body = (
        "intro paragraph\n"
        "\n"
        "# Top\n"
        "before sub\n"
        "\n"
        "## Sub\n"
        "deep content\n"
    )
    secs = parse_sections(body, doc_title="Doc")
    assert len(secs) == 3
    assert secs[0].title is None
    assert secs[0].level == 0
    assert secs[0].path == ["Doc"]
    assert "intro paragraph" in secs[0].body

    assert secs[1].title == "Top"
    assert secs[1].level == 1
    assert secs[1].path == ["Doc", "Top"]
    assert "before sub" in secs[1].body
    assert "deep content" not in secs[1].body

    assert secs[2].title == "Sub"
    assert secs[2].level == 2
    assert secs[2].path == ["Doc", "Top", "Sub"]
    assert "deep content" in secs[2].body


def test_heading_inside_fenced_code_block_is_ignored() -> None:
    body = (
        "# Real\n"
        "\n"
        "```\n"
        "# fake heading inside code\n"
        "more code\n"
        "```\n"
        "\n"
        "after\n"
    )
    secs = parse_sections(body)
    assert len(secs) == 2  # preamble + Real
    assert secs[1].title == "Real"
    assert "# fake heading inside code" in secs[1].body
    assert "after" in secs[1].body


def test_setext_h1_and_h2_recognized() -> None:
    body = (
        "Top Title\n"
        "=========\n"
        "\n"
        "body of top\n"
        "\n"
        "Sub Heading\n"
        "-----------\n"
        "\n"
        "sub body\n"
    )
    secs = parse_sections(body)
    titles = [s.title for s in secs if s.title is not None]
    assert "Top Title" in titles
    assert "Sub Heading" in titles
    sub = next(s for s in secs if s.title == "Sub Heading")
    assert sub.path == ["Top Title", "Sub Heading"]
    assert "sub body" in sub.body


def test_doc_title_prepends_to_every_section_path() -> None:
    body = "# A\nbody A\n## B\nbody B\n"
    secs = parse_sections(body, doc_title="My Doc")
    for s in secs:
        assert s.path[0] == "My Doc"


def test_non_ascii_round_trip() -> None:
    body = "# Über résumé\n\n中文 — 🌍\n"
    secs = parse_sections(body, doc_title="文档")
    h1 = next(s for s in secs if s.title == "Über résumé")
    assert h1.path == ["文档", "Über résumé"]
    assert "中文 — 🌍" in h1.body


def test_h2_then_h1_resets_ancestor_stack() -> None:
    body = "# A\n## A1\n## A2\n# B\n## B1\n"
    secs = parse_sections(body)
    a2 = next(s for s in secs if s.title == "A2")
    b1 = next(s for s in secs if s.title == "B1")
    assert a2.path == ["A", "A2"]
    assert b1.path == ["B", "B1"]


# ── SearchIndex ──────────────────────────────────────────────────────


@pytest.fixture
def index(tmp_path: Path) -> SearchIndex:
    return SearchIndex(tmp_path / "search.db")


def _section(title: str | None, body: str, path: list[str] | None = None) -> Section:
    return Section(
        title=title, level=0 if title is None else 1, path=path or [title or ""], body=body
    )


def test_upsert_and_search_round_trip(index: SearchIndex) -> None:
    index.upsert_doc_sections(
        "doc1",
        [_section("Setup", "run pip install ampersand here")],
    )
    results = index.search("pip install")
    assert len(results) == 1
    assert results[0].doc_id == "doc1"
    assert results[0].section_title == "Setup"
    assert "<mark>pip</mark>" in results[0].snippet
    assert "<mark>install</mark>" in results[0].snippet


def test_bm25_ranks_more_hits_higher(index: SearchIndex) -> None:
    index.upsert_doc_sections(
        "many", [_section("M", "alpha alpha alpha alpha")]
    )
    index.upsert_doc_sections(
        "few", [_section("F", "alpha plus other words here")]
    )
    results = index.search("alpha")
    ids = [r.doc_id for r in results]
    # `many` should rank above `few` (lower bm25 = better)
    assert ids[0] == "many"
    assert ids[1] == "few"


def test_delete_doc_removes_all_its_sections(index: SearchIndex) -> None:
    index.upsert_doc_sections(
        "d", [_section("A", "alpha"), _section("B", "alpha bravo")]
    )
    assert len(index.search("alpha")) == 2
    index.delete_doc("d")
    assert index.search("alpha") == []
    assert index.is_empty()


def test_upsert_replaces_previous_sections(index: SearchIndex) -> None:
    index.upsert_doc_sections(
        "d", [_section("A", "alpha"), _section("B", "alpha bravo")]
    )
    assert len(index.search("alpha")) == 2
    index.upsert_doc_sections("d", [_section("C", "alpha charlie")])
    results = index.search("alpha")
    titles = {r.section_title for r in results}
    assert titles == {"C"}


def test_search_empty_query_raises(index: SearchIndex) -> None:
    with pytest.raises(SearchError):
        index.search("")


def test_search_unknown_mode_raises(index: SearchIndex) -> None:
    with pytest.raises(SearchError):
        index.search("foo", mode="quantum")


def test_substring_mode_treats_OR_as_literal(index: SearchIndex) -> None:
    index.upsert_doc_sections(
        "with_or",
        [_section("X", "either this or that")],
    )
    index.upsert_doc_sections(
        "without_or",
        [_section("Y", "alpha bravo charlie delta")],
    )
    # In FTS mode, `OR` is an operator joining empty terms — would be invalid.
    # In substring mode, we treat it as a literal word and match the doc that has it.
    results = index.search("OR", mode="substring")
    ids = {r.doc_id for r in results}
    assert ids == {"with_or"}


def test_any_mode_or_joins_tokens(index: SearchIndex) -> None:
    index.upsert_doc_sections(
        "robots", [_section("R", "humanoid robotics startup raised funding")]
    )
    index.upsert_doc_sections(
        "gtm", [_section("G", "gtm problems for early-stage startups")]
    )
    index.upsert_doc_sections(
        "off", [_section("O", "completely unrelated topic about gardening")]
    )
    # FTS-AND mode: no section has both "gtm" AND "robotics" → 0 results.
    assert index.search("gtm robotics", mode="fts") == []
    # any mode: ORs the tokens, both robots and gtm sections come back.
    results = index.search("gtm robotics", mode="any")
    ids = {r.doc_id for r in results}
    assert "robots" in ids
    assert "gtm" in ids
    assert "off" not in ids


def test_any_mode_filters_stopwords(index: SearchIndex) -> None:
    index.upsert_doc_sections(
        "doc", [_section("X", "robotics is the future")]
    )
    # query has lots of stopwords; `robotics` is the only meaningful token.
    results = index.search("the in of robotics", mode="any")
    assert len(results) == 1


def test_any_mode_falls_back_when_only_stopwords(index: SearchIndex) -> None:
    index.upsert_doc_sections("doc", [_section("X", "the and or of")])
    # All tokens are stopwords — fall back to using them rather than raising.
    results = index.search("the and or", mode="any")
    assert len(results) >= 0  # no error, may or may not match


def test_is_empty_flips_after_first_upsert(index: SearchIndex) -> None:
    assert index.is_empty()
    index.upsert_doc_sections("d", [_section("A", "alpha")])
    assert not index.is_empty()


def test_reset_empties_index(index: SearchIndex) -> None:
    index.upsert_doc_sections("d", [_section("A", "alpha")])
    assert not index.is_empty()
    index.reset()
    assert index.is_empty()


# ── SearchIndexer ────────────────────────────────────────────────────


@pytest.fixture
def store_and_indexer(tmp_path: Path) -> tuple[MarkdownStore, SearchIndexer]:
    idx = SearchIndex(tmp_path / ".store" / "search.db")

    # Late-bound on_change so we can build store first then attach.
    indexer_ref: dict[str, SearchIndexer] = {}

    def on_change(event: ChangeEvent) -> None:
        indexer_ref["x"].handle_change(event)

    store = MarkdownStore(tmp_path / "vault", on_change=on_change)
    indexer = SearchIndexer(store, idx)
    indexer_ref["x"] = indexer
    return store, indexer


def test_create_event_indexes_sections(
    store_and_indexer: tuple[MarkdownStore, SearchIndexer]
) -> None:
    store, indexer = store_and_indexer
    store.create(
        "# Top\n\nalpha bravo\n\n## Sub\n\ncharlie delta\n",
        {"title": "Doc"},
    )
    results = indexer.index.search("charlie")
    assert len(results) == 1
    assert results[0].section_title == "Sub"
    assert results[0].section_path == ["Doc", "Top", "Sub"]


def test_update_event_replaces_sections(
    store_and_indexer: tuple[MarkdownStore, SearchIndexer]
) -> None:
    store, indexer = store_and_indexer
    doc = store.create("# A\n\nalpha\n", {"title": "T"})
    store.update(doc.meta.id, "# A\n\nbravo\n", if_match=doc.meta.content_hash)
    assert indexer.index.search("alpha") == []
    assert len(indexer.index.search("bravo")) == 1


def test_delete_event_removes_sections(
    store_and_indexer: tuple[MarkdownStore, SearchIndexer]
) -> None:
    store, indexer = store_and_indexer
    doc = store.create("# A\n\nalpha\n", {"title": "T"})
    store.delete(doc.meta.id)
    assert indexer.index.search("alpha") == []
    assert indexer.index.is_empty()


def test_indexer_swallows_exceptions(
    store_and_indexer: tuple[MarkdownStore, SearchIndexer],
    caplog: pytest.LogCaptureFixture,
) -> None:
    _, indexer = store_and_indexer
    # Force the index to raise by closing it.
    indexer.index.close()
    caplog.set_level(logging.ERROR, logger="ampersand_core.search.indexer")
    # No raise even though the underlying call will fail.
    indexer.handle_change(
        ChangeEvent(
            kind=ChangeKind.CREATED,
            id="anything",
            path="x",
            content_hash="sha256:x",
            occurred_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        )
    )
    assert any("search indexer failed" in rec.message for rec in caplog.records)


def test_bootstrap_no_op_on_populated(
    store_and_indexer: tuple[MarkdownStore, SearchIndexer]
) -> None:
    store, indexer = store_and_indexer
    store.create("# A\n\nalpha\n", {"title": "T"})
    n = indexer.bootstrap(force=False)
    assert n == 0  # already populated, no-op


def test_bootstrap_force_rebuilds(tmp_path: Path) -> None:
    # Create a store with NO indexer wired in, populate it, then attach an
    # indexer afterwards. force=True should rebuild from iter_all().
    store = MarkdownStore(tmp_path / "vault")
    store.create("# A\n\nalpha\n", {"title": "T1"})
    time.sleep(1)
    store.create("# B\n\nbravo\n", {"title": "T2"})

    idx = SearchIndex(tmp_path / ".store" / "search.db")
    indexer = SearchIndexer(store, idx)
    assert idx.is_empty()
    n = indexer.bootstrap(force=True)
    assert n == 2
    assert len(idx.search("alpha")) == 1
    assert len(idx.search("bravo")) == 1
