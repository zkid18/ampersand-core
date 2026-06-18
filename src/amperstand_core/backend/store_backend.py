"""Backend that writes directly to a local MarkdownStore."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from amperstand_core.backend.base import BackendError
from amperstand_core.store import MarkdownStore


class StoreBackend:
    """Wraps a MarkdownStore. Use when the calling process runs on the same
    machine as the vault data dir.
    """

    def __init__(self, root: Path | str) -> None:
        self._store = MarkdownStore(Path(root))

    def create(
        self, body: str, frontmatter: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        try:
            doc = self._store.create(body, frontmatter or {})
        except Exception as exc:  # noqa: BLE001
            raise BackendError(f"store create failed: {exc}") from exc
        return {
            "id": doc.meta.id,
            "title": doc.meta.title,
            "path": doc.meta.path,
            "content_hash": doc.meta.content_hash,
        }

    def capture_url(self, url: str) -> dict[str, Any] | None:
        # The store backend has no URL fetcher of its own — caller extracts.
        return None

    def close(self) -> None:
        # MarkdownStore has no explicit close; nothing to release.
        return

    @property
    def store(self) -> MarkdownStore:
        return self._store
