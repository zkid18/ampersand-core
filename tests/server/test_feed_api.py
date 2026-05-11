from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ampersand_core.feed import FeedEntry, FeedInfo
import sys

# Importing the submodule registers it in sys.modules, but the package
# __init__.py rebinds the name `router` to an APIRouter instance, which
# shadows attribute-style access. Pull the module out by its dotted name.
import ampersand_core.server.feed_api.router  # noqa: F401
feed_router_mod = sys.modules["ampersand_core.server.feed_api.router"]

from ampersand_core.server.app import app
from ampersand_core.server.vault_api.store_factory import reset_store_cache


HEAD = {"Authorization": "Bearer devkey"}


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("AMPERSAND_API_KEY", "devkey")
    monkeypatch.setenv("AMPERSAND_DATA_DIR", str(tmp_path))
    reset_store_cache()
    yield TestClient(app)
    reset_store_cache()


def _fake_feed(entries: list[FeedEntry]) -> FeedInfo:
    return FeedInfo(
        url="https://example.com/feed.rss",
        title="Example Feed",
        description=None,
        entries=entries,
    )


def _fake_content(url: str, title: str):
    """Mimic ExtractedContent enough for to_markdown() + store.create()."""
    from types import SimpleNamespace

    from ampersand_core.models import ContentType

    return SimpleNamespace(
        title=title,
        url=url,
        content_type=ContentType.ARTICLE,
        author="Test Author",
        body_html="<p>body</p>",
        body_text="body",
        published_at=None,
    )


def test_preview_classifies_new_vs_skip(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Pre-seed one doc so we can test the skip path
    client.post(
        "/vault", headers=HEAD,
        json={
            "body": "old body\n",
            "frontmatter": {"title": "Old", "source": "https://ex.com/a"},
        },
    )

    entries = [
        FeedEntry(url="https://ex.com/a", title="A (already)", published=None),
        FeedEntry(url="https://ex.com/b", title="B (new)", published=None),
    ]
    monkeypatch.setattr(
        feed_router_mod, "parse_feed",
        lambda url: _fake_feed(entries),
    )

    r = client.post(
        "/feeds/preview", headers=HEAD,
        json={"url": "https://example.com/feed.rss"},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["feed_title"] == "Example Feed"
    assert data["new_count"] == 1
    assert data["skip_count"] == 1
    statuses = {e["url"]: e for e in data["entries"]}
    assert statuses["https://ex.com/a"]["status"] == "skip"
    assert statuses["https://ex.com/b"]["status"] == "new"


def test_ingest_captures_new_entries(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    entries = [
        FeedEntry(
            url="https://ex.com/essay-1", title="Essay 1",
            author="pg", published=datetime(2026, 1, 1, tzinfo=timezone.utc),
        ),
        FeedEntry(
            url="https://ex.com/essay-2", title="Essay 2", published=None,
        ),
    ]
    monkeypatch.setattr(
        feed_router_mod, "parse_feed",
        lambda url: _fake_feed(entries),
    )

    def fake_extract_article(url: str):
        return _fake_content(url, f"extracted: {url.rsplit('/', 1)[-1]}")

    def fake_to_markdown(content) -> str:
        return f"# {content.title}\n\nbody\n"

    monkeypatch.setattr(
        feed_router_mod, "extract_article", fake_extract_article
    )
    monkeypatch.setattr(
        feed_router_mod, "to_markdown", fake_to_markdown
    )

    r = client.post(
        "/feeds/ingest", headers=HEAD,
        json={"url": "https://example.com/feed.rss", "tags": ["pg"]},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert len(data["captured"]) == 2
    assert len(data["skipped"]) == 0
    assert data["failed"] == []

    # Re-running should now skip both — they're in the vault
    r2 = client.post(
        "/feeds/ingest", headers=HEAD,
        json={"url": "https://example.com/feed.rss"},
    )
    assert r2.status_code == 200
    data2 = r2.json()
    assert len(data2["captured"]) == 0
    assert len(data2["skipped"]) == 2

    # And the captured docs carry the feed metadata
    listing = client.get("/vault?limit=10", headers=HEAD).json()
    titles = {m["title"] for m in listing["items"]}
    assert "extracted: essay-1" in titles
    feed_url_hits = [m for m in listing["items"] if m["extra"].get("feed_url")]
    assert len(feed_url_hits) == 2
    pg_tagged = [m for m in listing["items"] if "pg" in m["tags"]]
    assert len(pg_tagged) == 2


def test_ingest_continues_after_extract_failure(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    entries = [
        FeedEntry(url="https://ex.com/ok", title="Ok", published=None),
        FeedEntry(url="https://ex.com/bad", title="Bad", published=None),
    ]
    monkeypatch.setattr(
        feed_router_mod, "parse_feed",
        lambda url: _fake_feed(entries),
    )

    def flaky_extract(url: str):
        if "bad" in url:
            raise RuntimeError("extractor exploded")
        return _fake_content(url, "Ok")

    monkeypatch.setattr(
        feed_router_mod, "extract_article", flaky_extract
    )
    monkeypatch.setattr(
        feed_router_mod, "to_markdown",
        lambda c: "# x\n\ny\n",
    )

    r = client.post(
        "/feeds/ingest", headers=HEAD,
        json={"url": "https://example.com/feed.rss"},
    )
    assert r.status_code == 200
    data = r.json()
    assert len(data["captured"]) == 1
    assert len(data["failed"]) == 1
    assert data["failed"][0]["error"] == "extractor exploded"


def test_feeds_require_auth(client: TestClient) -> None:
    r = client.post("/feeds/preview", json={"url": "x"})
    assert r.status_code == 401
    r = client.post("/feeds/ingest", json={"url": "x"})
    assert r.status_code == 401


def test_ingest_rejects_invalid_limit(client: TestClient) -> None:
    r = client.post(
        "/feeds/ingest", headers=HEAD,
        json={"url": "https://x", "limit": 0},
    )
    assert r.status_code == 422
