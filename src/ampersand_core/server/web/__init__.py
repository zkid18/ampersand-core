"""Static HTML+JS web view for the vault.

Serves a minimal SPA (vanilla JS, no build) at /ui/. The HTML+JS shell is
unauthenticated — the user pastes their AMPERSAND_API_KEY into the page,
which then attaches it as a Bearer token to every /vault/* call. The
JSON endpoints are still gated by the existing require_api_key dep.
"""

from ampersand_core.server.web.router import mount_static, router

__all__ = ["router", "mount_static"]
