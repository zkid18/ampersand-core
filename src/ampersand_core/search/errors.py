"""Exceptions raised by the search module."""

from __future__ import annotations


class SearchError(Exception):
    """Base class for search-related errors (bad query, index failure, etc.)."""
