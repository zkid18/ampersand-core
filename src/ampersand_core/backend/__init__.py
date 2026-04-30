"""Pluggable vault backends — destination is config, not code.

Capture flows call `backend.create(body, frontmatter)`. Where the doc lands
depends on which backend the config selected:

- StoreBackend: writes directly to a local MarkdownStore (process is on the
  same box as the canonical vault).
- HTTPBackend:  POSTs to a remote /vault endpoint (process is somewhere else).

Both expose the same interface so the calling code never branches on "are we
local or remote."
"""

from ampersand_core.backend.base import VaultBackend, BackendError
from ampersand_core.backend.factory import build_backend, BackendConfig
from ampersand_core.backend.http_backend import HTTPBackend
from ampersand_core.backend.store_backend import StoreBackend

__all__ = [
    "VaultBackend",
    "BackendError",
    "build_backend",
    "BackendConfig",
    "StoreBackend",
    "HTTPBackend",
]
