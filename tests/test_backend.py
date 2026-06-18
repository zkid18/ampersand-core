from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from amperstand_core.backend import (
    BackendError,
    HTTPBackend,
    StoreBackend,
    build_backend,
)


# ── StoreBackend ─────────────────────────────────────────────────────


def test_store_backend_create(tmp_path: Path) -> None:
    b = StoreBackend(tmp_path)
    out = b.create("# hello\n\nbody\n", {"title": "T", "tags": ["x"]})
    assert "id" in out
    assert out["title"] == "T"
    assert out["content_hash"].startswith("sha256:")
    assert (tmp_path / out["path"]).exists()


def test_store_backend_capture_url_returns_none(tmp_path: Path) -> None:
    b = StoreBackend(tmp_path)
    assert b.capture_url("https://example.com") is None


# ── HTTPBackend ──────────────────────────────────────────────────────


def test_http_backend_requires_key() -> None:
    with pytest.raises(BackendError):
        HTTPBackend(url="http://x", api_key=None, api_key_env="DEFINITELY_NOT_SET")


def test_http_backend_create_posts_with_bearer(httpx_mock) -> None:
    httpx_mock.add_response(
        url="http://vault.test/vault",
        method="POST",
        json={"id": "01ABC", "title": "T", "content_hash": "sha256:x"},
    )
    b = HTTPBackend(url="http://vault.test", api_key="tk-secret")
    try:
        out = b.create("hi", {"title": "T"})
    finally:
        b.close()
    assert out["id"] == "01ABC"
    sent = httpx_mock.get_request()
    assert sent.headers["authorization"] == "Bearer tk-secret"


def test_http_backend_capture_calls_capture_endpoint(httpx_mock) -> None:
    httpx_mock.add_response(
        url="http://vault.test/capture",
        method="POST",
        json={"title": "X", "url": "https://example.com", "content_type": "article", "markdown": "..."},
    )
    b = HTTPBackend(url="http://vault.test", api_key="tk")
    try:
        out = b.capture_url("https://example.com")
    finally:
        b.close()
    assert out["title"] == "X"


def test_http_backend_capture_404_returns_none(httpx_mock) -> None:
    httpx_mock.add_response(
        url="http://vault.test/capture",
        method="POST",
        status_code=404,
    )
    b = HTTPBackend(url="http://vault.test", api_key="tk")
    try:
        assert b.capture_url("https://example.com") is None
    finally:
        b.close()


def test_http_backend_create_500_raises(httpx_mock) -> None:
    httpx_mock.add_response(
        url="http://vault.test/vault",
        method="POST",
        status_code=500,
    )
    b = HTTPBackend(url="http://vault.test", api_key="tk")
    try:
        with pytest.raises(BackendError):
            b.create("x")
    finally:
        b.close()


# ── factory ──────────────────────────────────────────────────────────


def test_build_store_backend_from_config(tmp_path: Path) -> None:
    b = build_backend({"kind": "store", "store": {"path": str(tmp_path)}})
    assert isinstance(b, StoreBackend)


def test_build_http_backend_from_config() -> None:
    b = build_backend(
        {"kind": "http", "http": {"url": "http://x", "api_key": "k"}}
    )
    try:
        assert isinstance(b, HTTPBackend)
        assert b.url == "http://x"
    finally:
        b.close()


def test_build_backend_flat_form(tmp_path: Path) -> None:
    # Flat config form (CLI-friendly)
    b = build_backend({"kind": "store", "path": str(tmp_path)})
    assert isinstance(b, StoreBackend)


def test_build_backend_unknown_kind(tmp_path: Path) -> None:
    with pytest.raises(BackendError):
        build_backend({"kind": "quantum"})


def test_build_backend_missing_kind() -> None:
    with pytest.raises(BackendError):
        build_backend({})


def test_build_store_missing_path() -> None:
    with pytest.raises(BackendError):
        build_backend({"kind": "store"})


def test_build_http_missing_url() -> None:
    with pytest.raises(BackendError):
        build_backend({"kind": "http", "http": {"api_key": "k"}})
