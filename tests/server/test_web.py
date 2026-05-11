from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ampersand_core.server.app import app
from ampersand_core.server.vault_api.store_factory import reset_store_cache


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("AMPERSAND_API_KEY", "devkey")
    monkeypatch.setenv("AMPERSAND_DATA_DIR", str(tmp_path))
    reset_store_cache()
    yield TestClient(app)
    reset_store_cache()


def test_ui_index_returns_html(client: TestClient) -> None:
    r = client.get("/ui/")
    assert r.status_code == 200
    ct = r.headers.get("content-type", "")
    assert ct.startswith("text/html"), ct
    body = r.text
    assert "<title>Ampersand Vault</title>" in body
    assert '<script src="/ui/static/app.js"></script>' in body
    assert 'href="/ui/static/app.css"' in body


def test_ui_no_trailing_slash_works(client: TestClient) -> None:
    r = client.get("/ui")
    assert r.status_code == 200
    assert "<title>Ampersand Vault</title>" in r.text


def test_ui_static_serves_app_js(client: TestClient) -> None:
    r = client.get("/ui/static/app.js")
    assert r.status_code == 200
    ct = r.headers.get("content-type", "")
    # Some platforms report application/javascript, some text/javascript — both fine
    assert "javascript" in ct, ct
    assert "KEY_STORAGE" in r.text  # sanity: it's actually our app.js


def test_ui_static_serves_app_css(client: TestClient) -> None:
    r = client.get("/ui/static/app.css")
    assert r.status_code == 200
    ct = r.headers.get("content-type", "")
    assert "css" in ct, ct
    assert ":root" in r.text  # sanity: our CSS file


def test_ui_does_not_require_bearer(client: TestClient) -> None:
    """The static shell must load without auth so the user can paste the
    key into the page that asks for it."""
    r = client.get("/ui/", headers={})  # no Authorization header
    assert r.status_code == 200
    r = client.get("/ui/static/app.js", headers={})
    assert r.status_code == 200


def test_ui_static_404_for_unknown(client: TestClient) -> None:
    r = client.get("/ui/static/does-not-exist.txt")
    assert r.status_code == 404
