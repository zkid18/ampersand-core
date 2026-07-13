"""API-key dependency for vault routes.

Authentication errors return a structured JSON body so integrators can
disambiguate without grepping source. Format:

    {"error": "<machine_readable_code>", "detail": "<human readable>"}

Error codes (string-stable across versions):
- `missing_authorization_header` — no Authorization header at all
- `unsupported_auth_scheme`      — header present but not "Bearer ..."
- `invalid_api_key`              — Bearer token doesn't match the configured key
- `server_misconfigured`         — server has no AMPERSTAND_API_KEY set (503)
"""

from __future__ import annotations

import os
import secrets

from fastapi import Header, HTTPException, status

_BEARER_PREFIX = "Bearer "


def _err(code: str, detail: str, *, status_code: int = 401) -> HTTPException:
    """Build an HTTPException whose detail is a structured dict.

    FastAPI serializes the detail as the response body's `detail` field, so
    clients see `{"detail": {"error": "...", "detail": "..."}}`. The nested
    shape is intentional — `detail` is what FastAPI puts there, `error` is
    our stable machine-readable code.
    """
    return HTTPException(
        status_code=status_code,
        detail={"error": code, "detail": detail},
        headers={"WWW-Authenticate": "Bearer"} if status_code == 401 else None,
    )


def require_api_key(authorization: str | None = Header(default=None)) -> None:
    expected = os.environ.get("AMPERSTAND_API_KEY")
    if not expected:
        raise _err(
            "server_misconfigured",
            "AMPERSTAND_API_KEY is not set on the server. Operator must "
            "configure it before any endpoint can authenticate.",
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    if not authorization:
        raise _err(
            "missing_authorization_header",
            "Send `Authorization: Bearer <your-api-key>` with every request "
            "except /health.",
        )
    if not authorization.startswith(_BEARER_PREFIX):
        raise _err(
            "unsupported_auth_scheme",
            f"Expected `Authorization: Bearer <key>`. Got header starting with "
            f"{authorization.split(' ', 1)[0]!r}. Only the Bearer scheme is supported.",
        )
    presented = authorization[len(_BEARER_PREFIX):]
    if not secrets.compare_digest(presented, expected):
        raise _err(
            "invalid_api_key",
            "The bearer token does not match the server's configured "
            "AMPERSTAND_API_KEY. If you recently rotated, re-export the env var "
            "or re-paste the key into your client config.",
        )
