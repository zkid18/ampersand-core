"""Detect whether an email is a newsletter vs personal mail or promo."""

from __future__ import annotations

import logging
import re
from email.message import EmailMessage

logger = logging.getLogger(__name__)

NEWSLETTER_DOMAINS = {
    "substack.com",
    "beehiiv.com",
    "convertkit.com",
    "buttondown.email",
    "ghost.io",
    "mailchimp.com",
    "sendfox.com",
    "hubspotemail.net",
    "revue.email",
}

PROMO_DOMAINS = {
    "klaviyomail.com",
    "shopifyemail.com",
}

MIN_WORD_COUNT = 200

_EMAIL_RE = re.compile(r"<?\s*([^<>\s]+@[^<>\s]+)\s*>?")


def get_sender_email(msg: EmailMessage) -> str:
    """Extract bare email address from From header."""
    from_header = str(msg.get("from", ""))
    m = _EMAIL_RE.search(from_header)
    if m:
        return m.group(1).lower()
    return from_header.strip().lower()


def _domain_of(address: str) -> str:
    """Return the domain part of an email address."""
    _, _, domain = address.partition("@")
    return domain.lower()


def _matches_domain_set(msg: EmailMessage, domains: set[str]) -> bool:
    """Check if Return-Path or From domain is in the given set."""
    for header_name in ("return-path", "from"):
        value = str(msg.get(header_name, ""))
        m = _EMAIL_RE.search(value)
        if m:
            domain = _domain_of(m.group(1))
            if any(domain.endswith(d) for d in domains):
                return True
    return False


def is_newsletter(msg: EmailMessage) -> bool:
    """Determine whether an email is likely a newsletter.

    Three layers (pass if ANY matches):
    1. List-Id header present -> True
    2. Sender domain matches known newsletter platforms -> True
       (but exclude promo platforms and noreply@ patterns)
    3. Text body word count >= MIN_WORD_COUNT -> True
    """
    sender = get_sender_email(msg)

    # Layer 1: List-Id header strongly suggests mailing list / newsletter
    if msg.get("list-id"):
        logger.debug("Newsletter detected (List-Id header): %s", sender)
        return True

    # Layer 2a: Known newsletter platform domains
    if _matches_domain_set(msg, NEWSLETTER_DOMAINS):
        logger.debug("Newsletter detected (platform domain): %s", sender)
        return True

    # Layer 2b: Promo platform exclusion
    if _matches_domain_set(msg, PROMO_DOMAINS):
        logger.debug("Rejected (promo domain): %s", sender)
        return False

    # Layer 2c: noreply@ pattern — likely transactional, skip
    if sender.startswith("noreply@") or sender.startswith("no-reply@"):
        logger.debug("Rejected (noreply pattern): %s", sender)
        return False

    # Layer 3: Content length fallback
    text_body = _get_text_body(msg)
    if text_body and len(text_body.split()) >= MIN_WORD_COUNT:
        logger.debug("Newsletter detected (word count >= %d): %s", MIN_WORD_COUNT, sender)
        return True

    logger.debug("Rejected (no signals matched): %s", sender)
    return False


def _get_text_body(msg: EmailMessage) -> str | None:
    """Extract plain text body for word-count heuristic."""
    if msg.get_content_type() == "text/plain":
        try:
            return msg.get_content()
        except Exception:
            return None

    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                try:
                    return part.get_content()
                except Exception:
                    return None
    return None
