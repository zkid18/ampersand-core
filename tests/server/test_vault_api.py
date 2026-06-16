from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ampersand_core.server.app import app, reset_job_store_cache
from ampersand_core.server.vault_api.store_factory import reset_store_cache


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("AMPERSAND_API_KEY", "devkey")
    monkeypatch.setenv("AMPERSAND_DATA_DIR", str(tmp_path))
    reset_store_cache()
    reset_job_store_cache()
    # `with` here triggers lifespan startup/shutdown — needed so the
    # background capture-worker actually runs during tests.
    with TestClient(app) as c:
        yield c
    reset_store_cache()
    reset_job_store_cache()


HEAD = {"Authorization": "Bearer devkey"}


SAMPLE_ARTICLE_HTML = """
<!DOCTYPE html>
<html>
<head>
  <title>An Essay on Phonk</title>
  <meta property="og:title" content="An Essay on Phonk" />
  <meta name="author" content="Some Author" />
</head>
<body>
  <article>
    <h1>An Essay on Phonk</h1>
    <p>Phonk is a subgenre of hip hop and trap music that emerged in
    the early 2010s, drawing inspiration from 1990s Memphis rap.</p>
    <p>The genre is characterized by its use of cowbell samples,
    distorted vocals, and lo-fi aesthetics. It has gained significant
    popularity through TikTok in recent years.</p>
    <p>Modern phonk has evolved into multiple subgenres including drift
    phonk, which is particularly popular in driving and gaming contexts.</p>
  </article>
</body>
</html>
"""


def test_capture_html_extracts_supplied_page(client: TestClient) -> None:
    r = client.post(
        "/capture/html", headers=HEAD,
        json={
            "url": "https://example.com/phonk",
            "html": SAMPLE_ARTICLE_HTML,
        },
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["url"] == "https://example.com/phonk"
    assert "Phonk" in data["title"]
    assert "Memphis rap" in data["markdown"]


def test_capture_html_rejects_challenge_page(client: TestClient) -> None:
    challenge_html = (
        '<html><head><title>Just a moment...</title></head>'
        '<body><script src="https://challenges.cloudflare.com/turnstile/v0/api.js"></script>'
        '<p>Verifying you are human</p></body></html>'
    )
    r = client.post(
        "/capture/html", headers=HEAD,
        json={"url": "https://hostile.example/", "html": challenge_html},
    )
    assert r.status_code == 422
    assert "wall" in r.json()["detail"].lower() or "challenge" in r.json()["detail"].lower()


def test_capture_html_requires_auth(client: TestClient) -> None:
    r = client.post("/capture/html", json={"url": "x", "html": "<p>y</p>"})
    assert r.status_code == 401


def test_capture_html_persists_by_default(client: TestClient) -> None:
    """The Self-hoster's v2 friction finding: POST /capture extracted but
    did not write a doc. New default is to persist; response carries id +
    path so callers don't need a second POST /vault round-trip."""
    r = client.post(
        "/capture/html", headers=HEAD,
        json={
            "url": "https://example.com/persist-test",
            "html": SAMPLE_ARTICLE_HTML,
        },
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["id"] is not None and len(data["id"]) == 26  # ULID
    assert data["path"].startswith("docs/")
    assert data["body_hash"] is not None and data["body_hash"].startswith("sha256:")

    # And the doc is actually findable via /vault:
    g = client.get(f"/vault/{data['id']}", headers=HEAD)
    assert g.status_code == 200, g.text
    assert g.json()["source"] == "https://example.com/persist-test"


def test_capture_html_persist_false_extracts_without_writing(
    client: TestClient,
) -> None:
    """Opt-out via persist=false preserves the legacy extract-only shape
    for callers that don't want a side effect."""
    before = client.get("/vault?limit=1", headers=HEAD).json()
    pre_total = sum(1 for _ in before.get("items") or [])

    r = client.post(
        "/capture/html", headers=HEAD,
        json={
            "url": "https://example.com/no-write",
            "html": SAMPLE_ARTICLE_HTML,
            "persist": False,
        },
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["id"] is None
    assert data["path"] is None
    assert data["body_hash"] is None
    # And no new doc was added (relies on the test client's fresh tmp_path).
    after = client.get("/vault?limit=10", headers=HEAD).json()
    sources = [it["source"] for it in (after.get("items") or [])]
    assert "https://example.com/no-write" not in sources


def test_capture_html_idempotent_on_same_url(client: TestClient) -> None:
    """Re-capturing the same URL must not create a duplicate doc — same
    body_hash → same id (relies on the store.create idempotency added in
    commit 9f5d83e)."""
    body = {
        "url": "https://example.com/idem",
        "html": SAMPLE_ARTICLE_HTML,
    }
    first = client.post("/capture/html", headers=HEAD, json=body).json()
    second = client.post("/capture/html", headers=HEAD, json=body).json()
    assert first["id"] == second["id"]


def test_capture_html_frontmatter_override_adds_tags(
    client: TestClient,
) -> None:
    """The extension wants to pass tags like ['clipper'] without a second
    POST /vault. New `frontmatter` field merges over the extractor's output."""
    r = client.post(
        "/capture/html", headers=HEAD,
        json={
            "url": "https://example.com/tagged",
            "html": SAMPLE_ARTICLE_HTML,
            "frontmatter": {"tags": ["clipper", "ai-native"]},
        },
    )
    assert r.status_code == 200, r.text
    doc_id = r.json()["id"]
    g = client.get(f"/vault/{doc_id}", headers=HEAD).json()
    assert set(g.get("tags") or []) == {"clipper", "ai-native"}


def test_capture_html_falls_back_to_html_when_no_transcript(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When extract_youtube can't get a transcript, /capture/html should
    use the supplied HTML to grab the description instead of saving a stub."""
    from ampersand_core.youtube import YouTubeTranscriptUnavailable

    def fake_yt(url: str):
        raise YouTubeTranscriptUnavailable(
            "no transcript", title="Some Video", channel="Some Channel",
            channel_url="https://youtube.com/@x",
        )

    import ampersand_core.server.app as app_mod
    monkeypatch.setattr(app_mod, "extract_youtube", fake_yt)

    # Real YouTube watch pages render the description in JS-only DOM, but
    # always include og:title + og:description meta tags server-side. The
    # fallback should pull from those, not run trafilatura over the chrome.
    yt_html = """
    <!DOCTYPE html>
    <html>
    <head>
      <meta property="og:title" content="The untold story of Brazilian funk" />
      <meta property="og:description" content="A documentary about funk carioca, the Miami-bass-inspired Brazilian street music born in Rio's favelas in the late 1980s." />
      <title>YouTube</title>
    </head>
    <body>
      <ytd-app>
        <ytd-watch-flexy>
          <!-- YouTube renders the actual description deep in JS -->
          <div id="masthead">unrelated chrome — trafilatura would grab this</div>
          <div id="related-suggestions">Billie Eilish made a BRAZILIAN song</div>
        </ytd-watch-flexy>
      </ytd-app>
    </body>
    </html>
    """
    r = client.post(
        "/capture/html", headers=HEAD,
        json={
            "url": "https://www.youtube.com/watch?v=e_NDFEWGW1w",
            "html": yt_html,
            "title": "yt page",
        },
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["content_type"] == "video"
    assert "Brazilian funk" in data["title"]
    assert "favelas" in data["markdown"]
    assert "Billie Eilish" not in data["markdown"]  # chrome must NOT leak in


def test_capture_url_returns_422_when_no_transcript(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bot/CLI hitting /capture (URL only, no HTML) gets 422 when
    transcript isn't available — there's no HTML to fall back to."""
    from ampersand_core.youtube import YouTubeTranscriptUnavailable

    def fake_yt(url: str):
        raise YouTubeTranscriptUnavailable(
            "no transcript", title="Some Video", channel="Some Channel",
            channel_url=None,
        )

    import ampersand_core.server.app as app_mod
    monkeypatch.setattr(app_mod, "extract_youtube", fake_yt)

    r = client.post(
        "/capture", headers=HEAD,
        json={"url": "https://youtube.com/watch?v=abc12345678"},
    )
    assert r.status_code == 422
    assert "transcript" in r.json()["detail"].lower()


def test_capture_html_routes_youtube_to_youtube_extractor(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A YouTube URL with arbitrary page HTML must dispatch to extract_youtube,
    not to the trafilatura HTML pipeline — the transcript is canonical."""
    from ampersand_core.models import CapturedContent, ContentType

    seen = {}

    def fake_youtube(url: str) -> CapturedContent:
        seen["yt"] = url
        return CapturedContent(
            url=url,
            title="Some Talk",
            content_markdown="full transcript text here",
            content_type=ContentType.VIDEO,
        )

    import ampersand_core.server.app as app_mod
    monkeypatch.setattr(app_mod, "extract_youtube", fake_youtube)

    r = client.post(
        "/capture/html", headers=HEAD,
        json={
            "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "html": "<html>youtube page chrome we don't care about</html>",
        },
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["title"] == "Some Talk"
    assert data["content_type"] == "video"
    assert "transcript" in data["markdown"]
    assert seen["yt"] == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


def test_unauthenticated_request_is_rejected(client: TestClient) -> None:
    r = client.get("/vault")
    assert r.status_code == 401
    assert r.headers.get("WWW-Authenticate") == "Bearer"


def test_wrong_api_key_is_rejected(client: TestClient) -> None:
    r = client.get("/vault", headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401


def test_vault_503_when_api_key_unset(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("AMPERSAND_API_KEY", raising=False)
    monkeypatch.setenv("AMPERSAND_DATA_DIR", str(tmp_path))
    reset_store_cache()
    c = TestClient(app)
    r = c.get("/vault", headers={"Authorization": "Bearer anything"})
    assert r.status_code == 503
    reset_store_cache()


def test_health_open_even_without_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AMPERSAND_API_KEY", raising=False)
    c = TestClient(app)
    assert c.get("/health").status_code == 200


def test_create_returns_doc_with_etag(client: TestClient) -> None:
    r = client.post(
        "/vault",
        headers=HEAD,
        json={"body": "# hi\n\nbody", "frontmatter": {"title": "Hi", "tags": ["t"]}},
    )
    assert r.status_code == 201
    body = r.json()
    assert body["id"].isalnum() or "-" in body["id"]  # ULID
    assert body["title"] == "Hi"
    assert body["tags"] == ["t"]
    assert body["body"].startswith("# hi")
    assert r.headers["ETag"] == body["content_hash"]


def test_get_returns_same_doc(client: TestClient) -> None:
    created = client.post(
        "/vault", headers=HEAD, json={"body": "x\n", "frontmatter": {"title": "X"}}
    ).json()
    r = client.get(f"/vault/{created['id']}", headers=HEAD)
    assert r.status_code == 200
    assert r.json()["id"] == created["id"]
    assert r.headers["ETag"] == created["content_hash"]


def test_get_raw_returns_markdown(client: TestClient) -> None:
    created = client.post(
        "/vault", headers=HEAD, json={"body": "z\n", "frontmatter": {"title": "Z"}}
    ).json()
    r = client.get(f"/vault/{created['id']}/raw", headers=HEAD)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/markdown")
    assert r.text.startswith("---\n")
    assert "title: Z" in r.text


def test_get_404_for_missing(client: TestClient) -> None:
    # 26-char Crockford-base32, valid format but unknown
    r = client.get("/vault/01ZZZZZZZZZZZZZZZZZZZZZZZZ", headers=HEAD)
    assert r.status_code == 404


def test_put_with_correct_if_match(client: TestClient) -> None:
    created = client.post(
        "/vault", headers=HEAD, json={"body": "v1\n", "frontmatter": {"title": "T"}}
    ).json()
    r = client.put(
        f"/vault/{created['id']}",
        headers={**HEAD, "If-Match": created["content_hash"]},
        json={"body": "v2\n", "frontmatter": {"title": "T"}},
    )
    assert r.status_code == 200
    new = r.json()
    assert new["body"] == "v2\n"
    assert new["content_hash"] != created["content_hash"]
    assert r.headers["ETag"] == new["content_hash"]


def test_put_with_stale_if_match_412(client: TestClient) -> None:
    created = client.post(
        "/vault", headers=HEAD, json={"body": "v1\n", "frontmatter": {"title": "T"}}
    ).json()
    client.put(
        f"/vault/{created['id']}",
        headers={**HEAD, "If-Match": created["content_hash"]},
        json={"body": "v2\n", "frontmatter": {"title": "T"}},
    )
    r = client.put(
        f"/vault/{created['id']}",
        headers={**HEAD, "If-Match": created["content_hash"]},
        json={"body": "v3\n"},
    )
    assert r.status_code == 412


def test_delete_then_404(client: TestClient) -> None:
    created = client.post(
        "/vault", headers=HEAD, json={"body": "x\n", "frontmatter": {"title": "T"}}
    ).json()
    r = client.delete(f"/vault/{created['id']}", headers=HEAD)
    assert r.status_code == 204
    r = client.get(f"/vault/{created['id']}", headers=HEAD)
    assert r.status_code == 404


def test_list_order_captured(client: TestClient) -> None:
    """order=captured sorts by captured_at desc, ignoring later updates."""
    import time

    a = client.post(
        "/vault", headers=HEAD,
        json={"body": "a\n", "frontmatter": {"title": "A"}},
    ).json()
    time.sleep(1)
    b = client.post(
        "/vault", headers=HEAD,
        json={"body": "b\n", "frontmatter": {"title": "B"}},
    ).json()
    time.sleep(1)
    # Update A — its updated_at moves forward but captured_at stays earliest.
    client.put(
        f"/vault/{a['id']}", headers=HEAD,
        json={"body": "a-updated\n", "frontmatter": {"title": "A"}},
    )

    by_updated = client.get("/vault?order=updated", headers=HEAD).json()
    by_captured = client.get("/vault?order=captured", headers=HEAD).json()
    assert [m["id"] for m in by_updated["items"]] == [a["id"], b["id"]]
    assert [m["id"] for m in by_captured["items"]] == [b["id"], a["id"]]


def test_list_order_invalid_rejected(client: TestClient) -> None:
    r = client.get("/vault?order=bogus", headers=HEAD)
    assert r.status_code == 422


def test_list_pagination(client: TestClient) -> None:
    import time

    ids = []
    for i in range(4):
        r = client.post(
            "/vault",
            headers=HEAD,
            json={"body": f"b{i}\n", "frontmatter": {"title": f"T{i}"}},
        )
        ids.append(r.json()["id"])
        time.sleep(1)

    p1 = client.get("/vault?limit=2", headers=HEAD).json()
    assert len(p1["items"]) == 2
    assert p1["next_cursor"]
    p2 = client.get(f"/vault?limit=2&cursor={p1['next_cursor']}", headers=HEAD).json()
    assert len(p2["items"]) == 2
    seen = {m["id"] for m in p1["items"] + p2["items"]}
    assert seen == set(ids)


def test_search_returns_ranked_section_results(client: TestClient) -> None:
    # seed two docs with sectioned bodies
    client.post(
        "/vault",
        headers=HEAD,
        json={
            "body": "# Setup\n\n## Install\n\nrun pip install ampersand here\n\n## Config\n\nset BAR=baz\n",
            "frontmatter": {"title": "Project"},
        },
    )
    client.post(
        "/vault",
        headers=HEAD,
        json={
            "body": "# Notes\n\nweekly meeting Friday at 3pm\n",
            "frontmatter": {"title": "Calendar"},
        },
    )

    r = client.post(
        "/vault/search",
        headers=HEAD,
        json={"q": "pip install", "limit": 5},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["results"]) >= 1
    top = body["results"][0]
    assert top["section_title"] == "Install"
    assert top["section_path"] == ["Project", "Setup", "Install"]
    assert "<mark>" in top["snippet"]
    assert isinstance(top["score"], float)


def test_search_substring_mode_treats_operators_literally(client: TestClient) -> None:
    client.post(
        "/vault",
        headers=HEAD,
        json={"body": "either this OR that\n", "frontmatter": {"title": "Maybe"}},
    )
    client.post(
        "/vault",
        headers=HEAD,
        json={"body": "alpha beta gamma\n", "frontmatter": {"title": "Greek"}},
    )
    r = client.post(
        "/vault/search",
        headers=HEAD,
        json={"q": "OR", "mode": "substring", "limit": 5},
    )
    assert r.status_code == 200
    titles = {h["section_path"][0] for h in r.json()["results"]}
    assert "Maybe" in titles
    assert "Greek" not in titles


def test_search_validation_rejects_empty_query(client: TestClient) -> None:
    r = client.post("/vault/search", headers=HEAD, json={"q": "   ", "limit": 5})
    assert r.status_code == 400


def test_search_validation_rejects_bad_mode(client: TestClient) -> None:
    r = client.post("/vault/search", headers=HEAD, json={"q": "x", "mode": "warp"})
    assert r.status_code == 422


def test_search_validation_rejects_out_of_range_limit(client: TestClient) -> None:
    r = client.post("/vault/search", headers=HEAD, json={"q": "x", "limit": 9999})
    assert r.status_code == 422


def test_search_requires_auth(client: TestClient) -> None:
    r = client.post("/vault/search", json={"q": "x"})
    assert r.status_code == 401


def test_search_hybrid_503_when_vec_unavailable(client: TestClient) -> None:
    """Without OPENAI_API_KEY the vector index dep returns None. /search/hybrid
    used to silently fall back to BM25-only with a 200, which deceived callers
    about which retrieval mode they were getting (Researcher RS5 finding in the
    friction test). Now responds 503 — callers that want BM25 should call
    /vault/search with mode='any' explicitly.
    """
    client.post(
        "/vault",
        headers=HEAD,
        json={"body": "alpha beta gamma\n", "frontmatter": {"title": "Greek"}},
    )
    r = client.post(
        "/vault/search/hybrid",
        headers=HEAD,
        json={"q": "alpha", "limit": 5},
    )
    assert r.status_code == 503
    assert "OPENAI_API_KEY" in r.json()["detail"]


def test_search_hybrid_503_when_rerank_requested_but_disabled(
    client: TestClient,
) -> None:
    """Same loud-failure pattern for rerank — silent drop is misleading."""
    client.post(
        "/vault",
        headers=HEAD,
        json={"body": "alpha beta\n", "frontmatter": {"title": "G"}},
    )
    r = client.post(
        "/vault/search/hybrid",
        headers=HEAD,
        json={"q": "alpha", "limit": 5, "rerank": True},
    )
    # Either kind of 503 is acceptable — vec or rerank precondition.
    assert r.status_code == 503


def test_openapi_404_when_docs_hidden(client: TestClient) -> None:
    # Default app has docs disabled so the API surface isn't advertised publicly.
    assert client.get("/openapi.json").status_code == 404
    assert client.get("/docs").status_code == 404


# ── async capture queue (extension's clip flow) ────────────────────


def test_capture_html_async_returns_202_and_job_id(client: TestClient) -> None:
    """The whole point of the queue: instant return for the extension popup."""
    r = client.post(
        "/capture/html/async", headers=HEAD,
        json={
            "url": "https://example.com/async-test",
            "html": SAMPLE_ARTICLE_HTML,
        },
    )
    assert r.status_code == 202, r.text
    data = r.json()
    assert data["status"] == "queued"
    assert data["job_id"] and len(data["job_id"]) == 26  # ULID


def test_capture_async_returns_202_and_job_id(client: TestClient) -> None:
    """Same for the URL-only path (bot/CLI use this)."""
    r = client.post(
        "/capture/async", headers=HEAD,
        json={"url": "https://example.com/url-async"},
    )
    assert r.status_code == 202
    assert r.json()["job_id"]


def test_get_job_returns_404_for_unknown_id(client: TestClient) -> None:
    r = client.get("/jobs/01XXXXXXXXXXXXXXXXXXXXXXXX", headers=HEAD)
    assert r.status_code == 404


def test_list_jobs_returns_queue_depth(client: TestClient) -> None:
    # Enqueue 3 jobs without giving the worker time to drain them all
    # (worker polls every 1s — TestClient doesn't time-travel).
    for i in range(3):
        client.post(
            "/capture/async", headers=HEAD,
            json={"url": f"https://example.com/depth-{i}"},
        )
    r = client.get("/jobs?limit=10", headers=HEAD)
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["items"]) >= 3
    # queue_depth + done count should approximate enqueued count
    assert body["queue_depth"] >= 0


def test_capture_async_requires_auth(client: TestClient) -> None:
    r = client.post("/capture/async", json={"url": "x"})
    assert r.status_code == 401


def test_worker_drains_a_capture_html_job(client: TestClient) -> None:
    """End-to-end: enqueue a /capture/html/async job, wait for the worker to
    drain it, verify the doc actually landed in the vault. The TestClient's
    lifespan starts the worker; we poll GET /jobs/{id} until status flips."""
    import time

    r = client.post(
        "/capture/html/async", headers=HEAD,
        json={
            "url": "https://example.com/worker-drain",
            "html": SAMPLE_ARTICLE_HTML,
        },
    )
    assert r.status_code == 202
    job_id = r.json()["job_id"]

    # Worker polls every 1s; give it up to 8s for safety on slow CI.
    deadline = time.time() + 8
    final = None
    while time.time() < deadline:
        g = client.get(f"/jobs/{job_id}", headers=HEAD).json()
        if g["status"] in ("done", "failed"):
            final = g
            break
        time.sleep(0.2)
    assert final is not None, "worker did not finish in time"
    assert final["status"] == "done", f"job failed: {final.get('error')}"
    assert final["doc_id"] and len(final["doc_id"]) == 26
    assert final["body_hash"].startswith("sha256:")

    # And the saved doc is fetchable.
    doc = client.get(f"/vault/{final['doc_id']}", headers=HEAD).json()
    assert doc["source"] == "https://example.com/worker-drain"


def test_openapi_includes_vault_routes_when_docs_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ampersand_core.server.app import create_app
    from ampersand_core.server.vault_api.store_factory import reset_store_cache

    monkeypatch.setenv("AMPERSAND_API_KEY", "devkey")
    monkeypatch.setenv("AMPERSAND_DATA_DIR", str(tmp_path))
    reset_store_cache()
    app2 = create_app(docs_visible=True)
    c = TestClient(app2)

    spec = c.get("/openapi.json").json()
    paths = spec["paths"]
    assert "/vault" in paths
    assert "/vault/{doc_id}" in paths
    assert "/vault/{doc_id}/raw" in paths
    assert "/vault/search" in paths
    assert "/capture" in paths
    reset_store_cache()


def test_capture_requires_api_key(client: TestClient) -> None:
    r = client.post("/capture", json={"url": "https://example.com"})
    assert r.status_code == 401
