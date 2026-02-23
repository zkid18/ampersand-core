"""Convert CapturedContent to a markdown file with YAML frontmatter."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from ampersand_core.models import CapturedContent


def to_markdown(content: CapturedContent) -> str:
    """Render a CapturedContent object as a full markdown document with frontmatter."""
    frontmatter = {
        "title": content.title,
        "source": content.url,
        "type": content.content_type.value,
        "captured": content.captured_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "tags": content.tags,
    }
    if content.author:
        frontmatter["author"] = content.author
    if content.sender_email:
        frontmatter["sender_email"] = content.sender_email

    fm_str = yaml.dump(frontmatter, default_flow_style=False, allow_unicode=True, sort_keys=False)

    return f"---\n{fm_str}---\n\n# {content.title}\n\n{content.content_markdown}\n"


def slug_filename(title: str) -> str:
    """Turn a title into a safe filename slug."""
    slug = title.lower().strip()
    slug = re.sub(r"[^\w\s-]", "", slug)
    slug = re.sub(r"[\s_]+", "-", slug)
    slug = re.sub(r"-+", "-", slug).strip("-")
    return slug[:80] if slug else "untitled"


def save_markdown(content: CapturedContent, output_dir: Path, filename: str | None = None) -> Path:
    """Save captured content as a .md file and return the file path."""
    if filename is None:
        filename = slug_filename(content.title) + ".md"
    elif not filename.endswith(".md"):
        filename += ".md"

    output_dir.mkdir(parents=True, exist_ok=True)
    filepath = output_dir / filename
    filepath.write_text(to_markdown(content), encoding="utf-8")
    return filepath
