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

from ampersand_core.server.app import app, reset_job_store_cache
from ampersand_core.server.feed_api.router import reset_registry_cache
from ampersand_core.server.vault_api.store_factory import reset_store_cache


HEAD = {"Authorization": "Bearer devkey"}


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("AMPERSAND_API_KEY", "devkey")
    monkeypatch.setenv("AMPERSAND_DATA_DIR", str(tmp_path))
    reset_store_cache()
    reset_job_store_cache()
    reset_registry_cache()
    with TestClient(app) as c:
        yield c
    reset_store_cache()
    reset_job_store_cache()
    reset_registry_cache()


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


# ── server-side feed registry ─────────────────────────────────────


def test_register_feed_creates_a_row_and_lists_it(client: TestClient) -> None:
    """POST /feeds/register persists a feed; GET /feeds shows it. Self-hoster
    S2/S4: the laptop's `feed add` finally reaches the server."""
    r = client.post(
        "/feeds/register", headers=HEAD,
        json={
            "url": "https://example.com/atom.xml",
            "name": "Example",
            "tags": ["test"],
        },
    )
    assert r.status_code == 201, r.text
    item = r.json()
    assert item["url"] == "https://example.com/atom.xml"
    assert item["name"] == "Example"
    assert item["tags"] == ["test"]
    assert item["enabled"] is True
    assert item["id"] and len(item["id"]) == 26
    feed_id = item["id"]

    listed = client.get("/feeds", headers=HEAD).json()
    assert len(listed["items"]) == 1
    assert listed["items"][0]["id"] == feed_id


def test_register_is_idempotent_on_url(client: TestClient) -> None:
    """Re-registering the same URL merges tags and returns the existing row,
    NOT a duplicate. Important because feed-sync might re-run the same
    bootstrap on every restart."""
    url = "https://example.com/atom.xml"
    a = client.post(
        "/feeds/register", headers=HEAD,
        json={"url": url, "tags": ["a"]},
    ).json()
    b = client.post(
        "/feeds/register", headers=HEAD,
        json={"url": url, "tags": ["b"]},
    ).json()
    assert a["id"] == b["id"]
    assert set(b["tags"]) == {"a", "b"}

    listed = client.get("/feeds", headers=HEAD).json()
    assert len(listed["items"]) == 1


def test_disable_then_enable_round_trips(client: TestClient) -> None:
    feed_id = client.post(
        "/feeds/register", headers=HEAD,
        json={"url": "https://example.com/atom.xml"},
    ).json()["id"]

    disabled = client.post(f"/feeds/{feed_id}/disable", headers=HEAD).json()
    assert disabled["enabled"] is False

    enabled_only = client.get("/feeds?enabled_only=true", headers=HEAD).json()
    assert enabled_only["items"] == []

    enabled = client.post(f"/feeds/{feed_id}/enable", headers=HEAD).json()
    assert enabled["enabled"] is True


def test_delete_removes_the_feed(client: TestClient) -> None:
    feed_id = client.post(
        "/feeds/register", headers=HEAD,
        json={"url": "https://example.com/atom.xml"},
    ).json()["id"]
    r = client.delete(f"/feeds/{feed_id}", headers=HEAD)
    assert r.status_code == 204
    assert client.get("/feeds", headers=HEAD).json()["items"] == []


def test_delete_unknown_feed_404s(client: TestClient) -> None:
    r = client.delete("/feeds/01XXXXXXXXXXXXXXXXXXXXXXXX", headers=HEAD)
    assert r.status_code == 404


def test_sync_all_runs_each_enabled_feed(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POST /feeds/sync drains every enabled feed. We mock _parse so this
    doesn't actually hit the network — we just want to verify the registry
    iteration path."""
    # Register two feeds, disable one.
    a = client.post(
        "/feeds/register", headers=HEAD,
        json={"url": "https://example.com/a.xml"},
    ).json()
    b = client.post(
        "/feeds/register", headers=HEAD,
        json={"url": "https://example.com/b.xml"},
    ).json()
    client.post(f"/feeds/{b['id']}/disable", headers=HEAD)

    # Mock _parse to return a no-entry feed (the iteration is what we care
    # about; per-feed capture logic is exercised by the existing /ingest tests).
    def fake_parse(url):
        return _fake_feed([])
    monkeypatch.setattr(feed_router_mod, "_parse", fake_parse)

    r = client.post("/feeds/sync", headers=HEAD)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total_feeds"] == 1  # only the enabled one
    assert len(body["results"]) == 1
    assert body["results"][0]["feed_id"] == a["id"]
    assert body["results"][0]["status"] == "ok"

    # last_sync_at gets recorded on the registry row.
    listed = client.get("/feeds", headers=HEAD).json()
    synced_a = next(it for it in listed["items"] if it["id"] == a["id"])
    assert synced_a["last_sync_at"] is not None
    assert synced_a["last_status"] == "ok"


def test_register_requires_auth(client: TestClient) -> None:
    r = client.post("/feeds/register", json={"url": "x"})
    assert r.status_code == 401
