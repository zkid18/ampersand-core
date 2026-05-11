"""HTTP endpoints for ad-hoc RSS/Atom feed ingestion.

Two endpoints under /feeds, both gated by the same Bearer key as /vault:

- POST /feeds/preview  — fetch + parse a feed, classify each entry as new
                          or already-captured. No writes.
- POST /feeds/ingest   — same parse, then capture new entries through the
                          existing extractor pipeline and write to the vault.
                          Synchronous, so the caller controls `limit`.

Dedupe is by `source` URL — the same field /capture sets when it stores a
doc. Re-running ingest on the same feed is a no-op for previously captured
entries.
"""

from ampersand_core.server.feed_api import router as _router_module

router = _router_module.router

__all__ = ["router"]
