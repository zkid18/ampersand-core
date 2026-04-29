"""Exceptions raised by the markdown store."""

from __future__ import annotations


class StoreError(Exception):
    """Base class for all store-related errors."""


class NotFound(StoreError):
    """Raised when a doc id has no matching file in the store."""


class Conflict(StoreError):
    """Raised when an `if_match` precondition fails on update or delete."""
