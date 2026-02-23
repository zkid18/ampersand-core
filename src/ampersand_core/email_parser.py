"""Parse email messages (newsletters) into markdown."""

from __future__ import annotations

import email
import email.policy
import logging
import re
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

logger = logging.getLogger(__name__)

from bs4 import BeautifulSoup
from markdownify import markdownify

from ampersand_core.models import CapturedContent, ContentType


def parse_eml_file(path: Path) -> CapturedContent:
    """Parse a .eml file and return captured content."""
    raw = path.read_bytes()
    return parse_email_bytes(raw)


def parse_email_bytes(raw: bytes) -> CapturedContent:
    """Parse raw email bytes into captured content."""
    msg = parse_raw_to_message(raw)
    return extract_from_message(msg)


def parse_raw_to_message(raw: bytes) -> EmailMessage:
    """Parse raw email bytes into an EmailMessage (for header inspection)."""
    raw = _fix_raw_preamble(raw)
    return email.message_from_bytes(raw, policy=email.policy.default)


def extract_from_message(msg: EmailMessage) -> CapturedContent:
    """Extract content and metadata from an EmailMessage."""
    return _extract_from_message(msg)


# Pattern matching a valid RFC 2822 header line: "Header-Name: value"
_HEADER_RE = re.compile(rb"^[A-Za-z][A-Za-z0-9-]*:\s", re.MULTILINE)


def _fix_raw_preamble(raw: bytes) -> bytes:
    """Strip any junk before the first valid email header.

    Yahoo Mail's 'View raw message' prepends a bare date line and other
    non-header text. Python's email parser treats that as the body start
    and silently ignores all real headers.
    """
    # Check if the first line is already a valid header
    first_line_end = raw.find(b"\n")
    if first_line_end == -1:
        return raw
    first_line = raw[:first_line_end]
    if _HEADER_RE.match(first_line):
        return raw  # Already starts with a valid header

    # Find the first valid header line
    m = _HEADER_RE.search(raw)
    if m:
        return raw[m.start():]
    return raw


def _extract_from_message(msg: EmailMessage) -> CapturedContent:
    """Extract content and metadata from an EmailMessage."""
    # Cast to str — Python email policy returns header objects, not plain strings
    subject = str(msg.get("subject", "Untitled Newsletter"))
    from_header = str(msg.get("from", ""))
    date_header = str(msg.get("date", ""))
    logger.debug("Parsing email: subject=%r from=%r", subject, from_header)

    # Parse author and sender email from "Name <email>" format
    author = _parse_author(from_header)
    sender_email = _parse_sender_email(from_header)

    # Parse date
    captured_at = _parse_date(date_header)

    # Get HTML body (preferred) or plain text fallback
    html_body = _get_html_body(msg)
    if html_body:
        logger.debug("Using HTML body (%d chars)", len(html_body))
        markdown = _html_to_clean_markdown(html_body)
    else:
        plain_body = _get_plain_body(msg)
        logger.debug("Using plain text body (%d chars)", len(plain_body) if plain_body else 0)
        markdown = plain_body if plain_body else "*No content found in email.*"

    # Build a unique URL using Message-ID (RFC 2822) for dedup
    message_id = str(msg.get("message-id", "")).strip().strip("<>")
    if message_id:
        url = f"email://msg-id/{message_id}"
    else:
        url = f"email://{from_header}/{subject}"

    return CapturedContent(
        url=url,
        title=subject,
        content_markdown=markdown,
        content_type=ContentType.NEWSLETTER,
        author=author,
        sender_email=sender_email,
        captured_at=captured_at,
    )


def _get_html_body(msg: EmailMessage) -> str | None:
    """Extract HTML body from a MIME message."""
    if msg.get_content_type() == "text/html":
        return msg.get_content()

    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                return part.get_content()
    return None


def _get_plain_body(msg: EmailMessage) -> str | None:
    """Extract plain text body from a MIME message."""
    if msg.get_content_type() == "text/plain":
        return msg.get_content()

    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                return part.get_content()
    return None


def _html_to_clean_markdown(html: str) -> str:
    """Convert newsletter HTML to clean markdown, stripping email cruft."""
    soup = BeautifulSoup(html, "html.parser")

    # Remove elements that are never useful content
    for tag in soup.find_all(["style", "script", "noscript", "head"]):
        tag.decompose()

    # Remove hidden preheader/preview text (Ghost, Substack, Mailchimp)
    for tag in soup.find_all(attrs={"class": re.compile(r"preheader|preview-text")}):
        tag.decompose()
    for tag in soup.find_all(style=re.compile(r"display:\s*none|max-height:\s*0|overflow:\s*hidden")):
        tag.decompose()

    # Remove email header chrome (Ghost: site logo, title, byline, feature image)
    # Use class_ for exact CSS class matching (avoids "header" matching "header-anchor-post")
    for cls in ["header-image", "site-info", "site-url", "post-title",
                "post-meta", "feature-image", "header-main", "header",
                "post-header"]:
        for tag in soup.find_all(class_=cls):
            tag.decompose()

    # Remove Beehiiv header section (contains duplicate title, date, subtitle)
    for tag in soup.find_all(id="header"):
        tag.decompose()

    # Remove Substack UI chrome (like/comment/share buttons, subscribe widgets)
    for cls in ["email-ufi-2-top", "email-ufi-2-bottom",
                "subscription-widget-wrap", "footer"]:
        for tag in soup.find_all(class_=cls):
            tag.decompose()

    # Remove tracking pixels (1x1 images)
    for img in soup.find_all("img"):
        attrs = img.attrs
        if not attrs:
            continue
        if attrs.get("width") in ("1", "0") or attrs.get("height") in ("1", "0"):
            img.decompose()
            continue
        src = attrs.get("src", "")
        if any(k in src.lower() for k in ("track", "beacon", "pixel", "open.", "spacer")):
            img.decompose()

    # Identify data tables — those with <thead> or <th> as direct children,
    # not inherited from nested tables inside layout wrappers.
    for table in soup.find_all("table"):
        if table.find("thead", recursive=False):
            table["data-keep"] = "1"
            continue
        for tr in table.find_all("tr", recursive=False):
            if tr.find("th", recursive=False):
                table["data-keep"] = "1"
                break

    # Unwrap layout tables — email newsletters use <table> for layout, not data.
    # Preserve data tables and their internal structure.
    for tag_name in ["table", "tbody", "thead", "tr", "td", "th"]:
        for tag in soup.find_all(tag_name):
            if tag.get("data-keep") == "1":
                continue
            if tag.find_parent("table", attrs={"data-keep": "1"}):
                continue
            tag.unwrap()

    # Clean up data-keep markers
    for table in soup.find_all("table", attrs={"data-keep": "1"}):
        del table["data-keep"]

    # Remove MS Office / MJML junk wrappers
    for div in soup.find_all("div"):
        classes = " ".join(div.get("class", []))
        if any(k in classes for k in ("mj-", "outlook", "wrapper")):
            div.unwrap()

    clean_html = str(soup)

    # Convert to markdown
    md = markdownify(clean_html, heading_style="ATX")

    # Clean up the result
    md = _clean_markdown(md)
    # Remove first H1 if present (converter.py adds its own from the title)
    md = re.sub(r"^#\s+.+\n*", "", md, count=1, flags=re.MULTILINE).lstrip("\n")
    return md


def _clean_markdown(md: str) -> str:
    """Clean up converted markdown — remove email footer cruft."""
    lines = md.split("\n")
    cleaned = []
    skip_from_here = False

    for line in lines:
        lower = line.lower().strip()

        # Skip "forwarded this newsletter" subscribe prompts (ConvertKit, etc.)
        if "forwarded this newsletter" in lower or "forwarded this email" in lower:
            continue

        # Stop at common email footer markers
        if any(marker in lower for marker in [
            "unsubscribe",
            "update your preferences",
            "manage your subscription",
            "view in browser",
            "view this email",
            "email preferences",
            "you are receiving this",
            "this email was sent",
            "to stop receiving",
            "if you no longer wish",
        ]):
            skip_from_here = True
            continue

        if skip_from_here:
            continue

        cleaned.append(line)

    result = "\n".join(cleaned)
    # Collapse multiple blank lines
    result = re.sub(r"\n{3,}", "\n\n", result)
    # Strip zero-width spaces
    result = result.replace("\u200b", "")
    # Trim trailing footer cruft (postal addresses, short non-content lines)
    lines_out = result.rstrip().split("\n")
    while lines_out and _is_footer_cruft(lines_out[-1]):
        lines_out.pop()
    return "\n".join(lines_out).strip()


_ADDRESS_RE = re.compile(r"\d+\s+\w+.*(road|street|st|ave|blvd|suite|ste|po box)", re.IGNORECASE)


def _is_footer_cruft(line: str) -> bool:
    """Check if a trailing line looks like footer cruft (address, copyright, etc.)."""
    stripped = line.strip()
    if not stripped:
        return True
    if _ADDRESS_RE.search(stripped):
        return True
    lower = stripped.lower()
    if lower.startswith("©") or lower.startswith("copyright"):
        return True
    return False


def _parse_author(from_header: str) -> str | None:
    """Extract display name from 'Name <email>' format."""
    if not from_header:
        return None
    # Match "Display Name <email@example.com>"
    m = re.match(r'"?([^"<]+)"?\s*<', from_header)
    if m:
        return m.group(1).strip()
    return from_header.strip()


def _parse_sender_email(from_header: str) -> str | None:
    """Extract bare email address from 'Name <email>' format."""
    if not from_header:
        return None
    m = re.search(r'<([^>]+)>', from_header)
    if m:
        return m.group(1).strip()
    # Bare email without angle brackets
    if "@" in from_header:
        return from_header.strip()
    return None


def _parse_date(date_header: str) -> datetime:
    """Parse email date header, fallback to now."""
    if not date_header:
        return datetime.now(timezone.utc)
    try:
        parsed = email.utils.parsedate_to_datetime(date_header)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except Exception:
        return datetime.now(timezone.utc)
