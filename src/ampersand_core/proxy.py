"""Shared HTTP proxy resolver for fetch paths that may be IP-blocked.

`AMPERSAND_HTTP_PROXY` is the preferred env var (covers YouTube AND article
fetches alike). `AMPERSAND_YOUTUBE_PROXY` is honored as a legacy alias so
existing /etc/ampersand/env files don't need to be edited.

Format: standard proxy URL — `http://user:pass@host:port` (or `socks5://...`
if httpx[socks] is installed).
"""

from __future__ import annotations

import os

PROXY_ENVS = ("AMPERSAND_HTTP_PROXY", "AMPERSAND_YOUTUBE_PROXY")


def get_proxy() -> str | None:
    """Return the configured HTTP proxy URL, or None if unset."""
    for env in PROXY_ENVS:
        val = os.environ.get(env, "").strip()
        if val:
            return val
    return None
