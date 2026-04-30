"""Build a backend from a config dict."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ampersand_core.backend.base import BackendError, VaultBackend
from ampersand_core.backend.http_backend import HTTPBackend
from ampersand_core.backend.store_backend import StoreBackend


@dataclass(frozen=True)
class BackendConfig:
    """Normalized config for `build_backend()`. Mirrors the JSON shape:

        { "kind": "http", "http": { "url": "...", "api_key_env": "..." } }
        { "kind": "store", "store": { "path": "/var/lib/ampersand/vault" } }
    """

    kind: str
    options: dict[str, Any]


def build_backend(config: dict[str, Any]) -> VaultBackend:
    """Construct a VaultBackend from a `[vault.backend]`-shaped dict.

    Accepts either nested form `{"kind": "http", "http": {...}}` or flattened
    `{"kind": "http", "url": "...", "api_key_env": "..."}` for ergonomic use
    from the CLI.
    """
    kind = (config.get("kind") or config.get("backend") or "").strip().lower()
    if not kind:
        raise BackendError("backend config missing 'kind'")

    if kind == "store":
        opts = config.get("store", {}) or {}
        path = opts.get("path") or config.get("path")
        if not path:
            raise BackendError("store backend requires 'path'")
        return StoreBackend(Path(path).expanduser())

    if kind == "http":
        opts = config.get("http", {}) or {}
        url = opts.get("url") or config.get("url")
        if not url:
            raise BackendError("http backend requires 'url'")
        api_key = opts.get("api_key") or config.get("api_key")
        api_key_env = opts.get("api_key_env") or config.get("api_key_env")
        return HTTPBackend(url=url, api_key=api_key, api_key_env=api_key_env)

    raise BackendError(f"unknown backend kind: {kind!r}")
