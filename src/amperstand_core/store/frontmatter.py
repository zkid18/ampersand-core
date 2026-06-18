"""Parse and serialize markdown files with YAML frontmatter."""

from __future__ import annotations

from typing import Any

import yaml

from amperstand_core.store.errors import StoreError

_FENCE = "---\n"

_CANONICAL_ORDER = (
    "id",
    "title",
    "source",
    "type",
    "captured",
    "updated",
    "tags",
    "content_hash",
)


def parse(text: str) -> tuple[dict[str, Any], str]:
    """Return (meta, body). Raises StoreError if no frontmatter block is present."""
    if not text.startswith(_FENCE):
        raise StoreError("missing leading frontmatter fence")

    rest = text[len(_FENCE):]
    end = rest.find("\n" + _FENCE.rstrip("\n") + "\n")
    if end == -1:
        # tolerate file ending exactly with closing fence
        if rest.rstrip("\n").endswith("---"):
            yaml_block = rest[: rest.rfind("---")].rstrip("\n")
            body = ""
        else:
            raise StoreError("missing trailing frontmatter fence")
    else:
        yaml_block = rest[:end]
        body = rest[end + len("\n---\n"):]
        # Strip the conventional single blank line after the closing fence.
        if body.startswith("\n"):
            body = body[1:]

    meta = yaml.safe_load(yaml_block) or {}
    if not isinstance(meta, dict):
        raise StoreError("frontmatter must be a mapping")
    return meta, body


def dump(meta: dict[str, Any], body: str) -> str:
    """Serialize meta+body into a frontmatter-headed markdown string."""
    ordered: dict[str, Any] = {}
    for key in _CANONICAL_ORDER:
        if key in meta:
            ordered[key] = meta[key]
    for key in sorted(k for k in meta if k not in _CANONICAL_ORDER):
        ordered[key] = meta[key]

    fm = yaml.dump(
        ordered,
        default_flow_style=False,
        allow_unicode=True,
        sort_keys=False,
    )
    return f"---\n{fm}---\n\n{body}"
