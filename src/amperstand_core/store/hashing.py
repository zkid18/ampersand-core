"""Content hashing used as the ETag value."""

from __future__ import annotations

import hashlib


def compute_hash(file_bytes: bytes) -> str:
    return "sha256:" + hashlib.sha256(file_bytes).hexdigest()
