"""Backend protocol — every capture destination implements this."""

from __future__ import annotations

from typing import Any, Protocol


class BackendError(Exception):
    """Raised when a backend operation fails."""


class VaultBackend(Protocol):
    """A destination for captured docs. Implementations: StoreBackend, HTTPBackend."""

    def create(
        self, body: str, frontmatter: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Create a new doc. Returns a dict with at least: id, content_hash, title."""
        ...

    def capture_url(self, url: str) -> dict[str, Any] | None:
        """Optional: extract clean markdown from a URL via the backend (e.g.
        the server's /capture endpoint). Backends that don't support URL
        extraction return None and the caller falls back to extracting locally.
        """
        ...

    def close(self) -> None:
        """Release resources (sockets, db connections). Idempotent."""
        ...
