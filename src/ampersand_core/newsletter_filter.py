"""Detect whether an email is a newsletter vs personal mail or promo."""

from __future__ import annotations

import logging
import re
from email.message import EmailMessage

from ampersand_core.newsletter_classifier import predict_keep

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

MIN_WORD_COUNT = 500

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
    """Determine whether an email is likely a newsletter worth capturing.

    The old heuristic was too lax — bare `List-Id` header was enough to pass,
    which let in every commercial mailing list (real-estate listings, RZD
    loyalty mailers, cashback promos). All of those set `List-Id` because
    it's required mailing-list etiquette.

    New rules (pass ONLY if explicit signal):
    - Promo domain blocklist + noreply@ → always reject.
    - Known newsletter platform domain → accept (Substack, Beehiiv, etc).
    - List-Id present **and** substantial text body → accept (catches
      self-hosted newsletters that don't use a recognized platform).
    - Anything else → reject. Sender can be added by hand via
      `ampersand email allow <sender>` if it's something we actually want.

    Pure-word-count fallback removed — Brazilian real-estate listings have
    >200 words of property descriptions and were sneaking through.
    """
    sender = get_sender_email(msg)

    # Hard rejects first — these always lose, regardless of other signals.
    if _matches_domain_set(msg, PROMO_DOMAINS):
        logger.debug("Rejected (promo domain): %s", sender)
        return False
    if sender.startswith("noreply@") or sender.startswith("no-reply@"):
        logger.debug("Rejected (noreply pattern): %s", sender)
        return False

    # Known newsletter platforms — strong signal, accept on its own.
    if _matches_domain_set(msg, NEWSLETTER_DOMAINS):
        logger.debug("Newsletter detected (platform domain): %s", sender)
        return True

    # Self-hosted / off-platform newsletters: List-Id is required (rules out
    # one-off marketing email), then defer to the classifier (TF-IDF + LogReg
    # trained on LLM-bootstrapped labels). If the classifier isn't available
    # (no sklearn, missing model), fall back to the word-count heuristic.
    if msg.get("list-id"):
        text_body = _get_text_body(msg) or ""
        subject = str(msg.get("subject", ""))
        classifier_says = predict_keep(sender, subject, text_body)
        if classifier_says is not None:
            verdict = "KEEP" if classifier_says else "SKIP"
            logger.debug("Classifier %s: %s — %s", verdict, sender, subject[:60])
            return classifier_says
        # Fallback: word-count heuristic (classifier unavailable).
        if text_body and len(text_body.split()) >= MIN_WORD_COUNT:
            logger.debug(
                "Newsletter detected (fallback: List-Id + %d+ words): %s",
                MIN_WORD_COUNT, sender,
            )
            return True
        logger.debug("Rejected (fallback: List-Id but body too short): %s", sender)
        return False

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
