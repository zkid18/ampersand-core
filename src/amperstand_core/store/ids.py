"""ULID generation and validation."""

from __future__ import annotations

from ulid import ULID

ULID_LEN = 26


def new_id() -> str:
    return str(ULID())


def is_valid(value: str) -> bool:
    if not isinstance(value, str) or len(value) != ULID_LEN:
        return False
    try:
        ULID.from_str(value)
    except (ValueError, TypeError):
        return False
    return True
