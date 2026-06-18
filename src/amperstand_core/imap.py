"""IMAP connector for fetching emails from a mailbox."""

from __future__ import annotations

import imaplib
import logging
import time
from email.message import EmailMessage
from typing import Callable, Generator

from amperstand_core.email_parser import extract_from_message, parse_raw_to_message
from amperstand_core.models import CapturedContent

# Python's imaplib defaults _MAXLINE to 1MB which is fine for tiny INBOXes but
# blows up on a long-running Gmail account where `SEARCH UNSEEN` can return
# hundreds of thousands of UIDs on a single line. 10MB covers ~1M UIDs.
imaplib._MAXLINE = 10 * 1024 * 1024

logger = logging.getLogger(__name__)


def _build_search_criterion(config: dict) -> str:
    """Compose the IMAP SEARCH expression from per-account config.

    Recognized keys (all optional, in addition to the literal escape hatch):
      - `since`: ISO date or `dd-Mmm-YYYY`. Restricts to messages newer
                 than this — critical on mailboxes with huge unread backlogs
                 (`raveforlive@gmail.com` had a >1MB SEARCH response).
      - `search_criterion`: raw IMAP expression, used as-is if set.

    Default is plain `UNSEEN` when nothing is configured.
    """
    raw = config.get("search_criterion")
    if raw:
        return raw
    parts = ["UNSEEN"]
    since = config.get("since")
    if since:
        parts.append(f'SINCE "{_imap_date(since)}"')
    if len(parts) == 1:
        return parts[0]
    return "(" + " ".join(parts) + ")"


def _imap_date(value: str) -> str:
    """Normalize a date string to IMAP's `dd-Mmm-YYYY` format.

    Accepts ISO `YYYY-MM-DD` (the easy thing for users to type into JSON)
    and the native `dd-Mmm-YYYY`. Anything else is returned verbatim so a
    typo surfaces as an IMAP server error rather than getting silently
    rewritten.
    """
    import datetime as _dt
    try:
        d = _dt.date.fromisoformat(value)
        return d.strftime("%d-%b-%Y")
    except ValueError:
        return value


def _connect(config: dict) -> imaplib.IMAP4_SSL:
    """Open an IMAP connection."""
    conn = imaplib.IMAP4_SSL(config["server"], config["port"])
    conn.login(config["email"], config["password"])
    conn.select(config["mailbox"])
    return conn


def fetch_unseen(
    config: dict,
    email_filter: Callable[[EmailMessage], bool] | None = None,
    batch_size: int = 50,
    search_criterion: str | None = None,
) -> Generator[tuple[bytes, CapturedContent], None, None]:
    """Fetch emails matching *search_criterion* and parse them. Yields (uid, content) pairs.

    Processes emails in batches with automatic reconnection on failure.
    If *email_filter* is provided, only emails where the callback returns True
    are yielded. When *search_criterion* is None the SEARCH is built from
    the per-account config (`since`, `search_criterion` keys) and falls
    back to plain UNSEEN.
    """
    if search_criterion is None:
        search_criterion = _build_search_criterion(config)
    conn = _connect(config)
    try:
        _, msg_ids = conn.search(None, search_criterion)
        if not msg_ids[0]:
            return
        uids = msg_ids[0].split()
        logger.info("Found %d unseen email(s)", len(uids))
    except Exception:
        try:
            conn.logout()
        except Exception:
            pass
        raise

    processed = set()
    retries = 0
    max_retries = 3
    i = 0

    while i < len(uids):
        uid = uids[i]
        if uid in processed:
            i += 1
            continue
        try:
            _, data = conn.fetch(uid, "(RFC822)")
            if not (data and data[0] and isinstance(data[0], tuple)):
                logger.warning("Empty response for UID %s, skipping", uid)
                i += 1
                continue
            raw = data[0][1]
            msg = parse_raw_to_message(raw)
            if email_filter and not email_filter(msg):
                conn.store(uid, "+FLAGS", "\\Seen")
                i += 1
                continue
            content = extract_from_message(msg)
            conn.store(uid, "+FLAGS", "\\Seen")
            processed.add(uid)
            yield (uid, content)
            i += 1
            retries = 0  # reset on success
        except (imaplib.IMAP4.error, OSError, ConnectionError) as exc:
            logger.warning("Connection lost at UID %s: %s", uid, exc)
            retries += 1
            if retries > max_retries:
                logger.error("Max retries (%d) reached, stopping", max_retries)
                return
            time.sleep(2 ** retries)
            try:
                conn = _connect(config)
            except Exception as reconn_exc:
                logger.error("Reconnect failed: %s", reconn_exc)
                return
        except Exception as exc:
            # Parse error — skip this email, continue
            logger.warning("Failed to process UID %s: %s", uid, exc)
            i += 1

    try:
        conn.close()
        conn.logout()
    except Exception:
        pass


def mark_seen(config: dict, uid: bytes) -> None:
    """Mark an email as seen by UID."""
    conn = _connect(config)
    try:
        conn.store(uid, "+FLAGS", "\\Seen")
    finally:
        conn.close()
        conn.logout()


def watch(
    config: dict,
    on_email: Callable[[CapturedContent], None],
    poll_interval: int = 30,
    email_filter: Callable[[EmailMessage], bool] | None = None,
) -> None:
    """Watch for new emails using IMAP IDLE (with polling fallback).

    Calls on_email for each new message. Runs forever until interrupted.
    """
    while True:
        conn = _connect(config)
        try:
            # Try IMAP IDLE if supported
            if _supports_idle(conn):
                _idle_loop(conn, config, on_email, email_filter)
            else:
                _poll_loop(conn, config, on_email, poll_interval, email_filter)
        except (imaplib.IMAP4.error, OSError):
            # Connection lost — reconnect after a delay
            time.sleep(5)
        finally:
            try:
                conn.logout()
            except Exception:
                pass


def _supports_idle(conn: imaplib.IMAP4_SSL) -> bool:
    """Check if the server advertises IDLE capability."""
    _, caps = conn.capability()
    if caps:
        return b"IDLE" in caps[0].upper().split()
    return False


def _idle_loop(
    conn: imaplib.IMAP4_SSL,
    config: dict,
    on_email: Callable[[CapturedContent], None],
    email_filter: Callable[[EmailMessage], bool] | None = None,
) -> None:
    """Use IMAP IDLE to wait for new emails."""
    while True:
        # Send IDLE command
        tag = conn._new_tag().decode()
        conn.send(f"{tag} IDLE\r\n".encode())
        conn.readline()  # + idling

        # Wait for server notification (up to 29 min, then re-IDLE per RFC)
        try:
            response = conn._get_line().decode(errors="replace")
        except (TimeoutError, OSError):
            response = ""

        # End IDLE
        conn.send(b"DONE\r\n")
        conn.readline()  # tag OK

        # If we got an EXISTS notification, fetch new emails
        if "EXISTS" in response:
            _process_unseen(conn, config, on_email, email_filter)


def _poll_loop(
    conn: imaplib.IMAP4_SSL,
    config: dict,
    on_email: Callable[[CapturedContent], None],
    interval: int,
    email_filter: Callable[[EmailMessage], bool] | None = None,
) -> None:
    """Poll for new emails at a regular interval."""
    while True:
        _process_unseen(conn, config, on_email, email_filter)
        time.sleep(interval)
        # NOOP to keep connection alive
        conn.noop()


def _process_unseen(
    conn: imaplib.IMAP4_SSL,
    config: dict,
    on_email: Callable[[CapturedContent], None],
    email_filter: Callable[[EmailMessage], bool] | None = None,
) -> None:
    """Fetch and process all unseen emails on an existing connection."""
    _, msg_ids = conn.search(None, _build_search_criterion(config))
    if not msg_ids[0]:
        return

    for uid in msg_ids[0].split():
        try:
            _, data = conn.fetch(uid, "(RFC822)")
            if not (data and data[0] and isinstance(data[0], tuple)):
                continue
            raw = data[0][1]
            msg = parse_raw_to_message(raw)
            if email_filter and not email_filter(msg):
                continue
            content = extract_from_message(msg)
            on_email(content)
            # Mark as seen
            conn.store(uid, "+FLAGS", "\\Seen")
        except Exception as exc:
            logger.warning("Failed to process UID %s: %s", uid, exc)
