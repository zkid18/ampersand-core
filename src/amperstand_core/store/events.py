"""Change-event protocol for indexer subscribers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Callable


class ChangeKind(str, Enum):
    CREATED = "created"
    UPDATED = "updated"
    DELETED = "deleted"


@dataclass(frozen=True)
class ChangeEvent:
    kind: ChangeKind
    id: str
    path: str | None
    content_hash: str | None
    occurred_at: datetime


OnChangeHook = Callable[[ChangeEvent], None]
