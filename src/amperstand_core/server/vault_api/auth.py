"""API-key dependency for vault routes."""

from __future__ import annotations

import os
import secrets

from fastapi import Header, HTTPException, status

_BEARER_PREFIX = "Bearer "


def require_api_key(authorization: str | None = Header(default=None)) -> None:
    expected = os.environ.get("AMPERSTAND_API_KEY")
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="AMPERSTAND_API_KEY not configured",
        )
    if not authorization or not authorization.startswith(_BEARER_PREFIX):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    presented = authorization[len(_BEARER_PREFIX):]
    if not secrets.compare_digest(presented, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid api key",
            headers={"WWW-Authenticate": "Bearer"},
        )
