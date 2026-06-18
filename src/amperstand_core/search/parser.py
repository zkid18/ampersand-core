"""Parse a markdown body into heading-bounded sections."""

from __future__ import annotations

import re

from amperstand_core.search.models import Section

_ATX_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_FENCE = re.compile(r"^\s*(```|~~~)")
_SETEXT_H1 = re.compile(r"^=+\s*$")
_SETEXT_H2 = re.compile(r"^-+\s*$")


def parse_sections(body: str, doc_title: str | None = None) -> list[Section]:
    """Split a markdown body into heading-bounded sections.

    - ATX headings (`# foo` ... `###### foo`) start a new section at that level.
    - Setext headings (a non-blank line followed by `=====` or `-----`) become
      h1 / h2 sections.
    - Fenced code blocks (``` or ~~~) are passed through verbatim — `#` inside a
      fence is body, not a heading.
    - The text before the first heading becomes a preamble section
      (level=0, title=None).
    - When `doc_title` is provided, it's prepended to every section's `path` so
      results read like "Doc Title > Heading > Subheading".
    - Returns at least one section. An empty body produces a single preamble
      section with body="".
    """
    lines = body.splitlines(keepends=True)
    sections: list[Section] = []
    in_fence = False
    fence_marker: str | None = None

    # Stack of (level, title) for ATX nesting.
    ancestors: list[tuple[int, str]] = []

    # Buffer for the section currently being built.
    cur_title: str | None = None
    cur_level: int = 0
    cur_path: list[str] = list(_root_path(doc_title))
    cur_body: list[str] = []

    def flush() -> None:
        sections.append(
            Section(
                title=cur_title,
                level=cur_level,
                path=list(cur_path),
                body="".join(cur_body),
            )
        )

    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.rstrip("\n").rstrip("\r")

        # Track fence state — fences open/close on lines starting with ``` or ~~~.
        fence_match = _FENCE.match(stripped)
        if fence_match:
            mark = fence_match.group(1)
            if not in_fence:
                in_fence = True
                fence_marker = mark
            elif fence_marker is not None and stripped.lstrip().startswith(fence_marker):
                in_fence = False
                fence_marker = None
            cur_body.append(line)
            i += 1
            continue

        if in_fence:
            cur_body.append(line)
            i += 1
            continue

        # ATX heading.
        atx = _ATX_HEADING.match(stripped)
        if atx:
            level = len(atx.group(1))
            title = atx.group(2).strip()
            flush()
            ancestors = _push_ancestor(ancestors, level, title)
            cur_title = title
            cur_level = level
            cur_path = list(_root_path(doc_title)) + [t for _, t in ancestors]
            cur_body = []
            i += 1
            continue

        # Setext heading: `Foo\n===` or `Foo\n---`. Look ahead one line.
        if i + 1 < len(lines) and stripped.strip():
            nxt = lines[i + 1].rstrip("\n").rstrip("\r")
            if _SETEXT_H1.match(nxt) or _SETEXT_H2.match(nxt):
                level = 1 if _SETEXT_H1.match(nxt) else 2
                title = stripped.strip()
                flush()
                ancestors = _push_ancestor(ancestors, level, title)
                cur_title = title
                cur_level = level
                cur_path = list(_root_path(doc_title)) + [t for _, t in ancestors]
                cur_body = []
                i += 2  # skip the underline
                continue

        cur_body.append(line)
        i += 1

    flush()
    return sections


def _root_path(doc_title: str | None) -> list[str]:
    return [doc_title] if doc_title else []


def _push_ancestor(
    ancestors: list[tuple[int, str]], level: int, title: str
) -> list[tuple[int, str]]:
    """Pop ancestors at >= level, push the new one. Returns updated stack."""
    new_stack = [(lvl, t) for lvl, t in ancestors if lvl < level]
    new_stack.append((level, title))
    return new_stack
