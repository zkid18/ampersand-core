"""Backend that POSTs to a remote ampersand-server over HTTP."""

from __future__ import annotations

import os
from typing import Any

import httpx

from ampersand_core.backend.base import BackendError


class HTTPBackend:
    """Wraps a sync httpx client. Use when the calling process is on a
    different machine than the canonical vault.
    """

    def __init__(
        self,
        url: str,
        api_key: str | None = None,
        api_key_env: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        if not api_key and api_key_env:
            api_key = os.environ.get(api_key_env)
        if not api_key:
            raise BackendError(
                "HTTPBackend requires an api_key (pass directly or via api_key_env)"
            )
        self._url = url.rstrip("/")
        self._client = httpx.Client(
            base_url=self._url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
        )

    def create(
        self, body: str, frontmatter: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        try:
            r = self._client.post(
                "/vault", json={"body": body, "frontmatter": frontmatter or {}}
            )
            r.raise_for_status()
        except httpx.HTTPError as exc:
            raise BackendError(f"http create failed: {exc}") from exc
        return r.json()

    def capture_url(self, url: str) -> dict[str, Any] | None:
        try:
            r = self._client.post("/capture", json={"url": url})
            r.raise_for_status()
        except httpx.HTTPStatusError as exc:
            if exc.response is not None and exc.response.status_code == 404:
                return None  # server doesn't have /capture
            raise BackendError(f"http capture failed: {exc}") from exc
        except httpx.HTTPError as exc:
            raise BackendError(f"http capture failed: {exc}") from exc
        return r.json()

    def close(self) -> None:
        self._client.close()

    @property
    def url(self) -> str:
        return self._url
