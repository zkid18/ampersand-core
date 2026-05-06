"""Tests for extractor heuristics that don't require network access."""

from __future__ import annotations

from ampersand_core.extractor import _looks_like_challenge


# ── HTML structural markers (language-agnostic) ────────────────────


def test_detects_cloudflare_challenge_widget_in_html() -> None:
    html = '<html><script src="https://challenges.cloudflare.com/turnstile/v0/api.js"></script></html>'
    # Even with a long, normal-looking body, the HTML widget gives it away.
    body = "lorem ipsum " * 200
    assert _looks_like_challenge(body, "Some Article", html) is True


def test_detects_cloudflare_cdn_cgi_path() -> None:
    html = '<iframe src="/cdn-cgi/challenge-platform/h/g/orchestrate/jsch/v1"></iframe>'
    assert _looks_like_challenge("body", None, html) is True


def test_detects_recaptcha_widget() -> None:
    html = '<div class="g-recaptcha" data-sitekey="abc123"></div>'
    assert _looks_like_challenge("body", "Login", html) is True


def test_detects_hcaptcha_widget() -> None:
    html = '<div class="h-captcha" data-sitekey="x"></div>'
    assert _looks_like_challenge("body", None, html) is True


# ── title stems (catches localized variants) ───────────────────────


def test_detects_title_just_a_moment() -> None:
    assert _looks_like_challenge("anything", "Just a moment...", "") is True


def test_detects_title_attention_required() -> None:
    assert _looks_like_challenge("anything", "Attention Required! | Cloudflare", "") is True


def test_detects_title_access_denied() -> None:
    assert _looks_like_challenge("anything", "Access Denied", "") is True


def test_detects_title_verify_you_are() -> None:
    assert _looks_like_challenge("anything", "Verify you are human", "") is True


# ── short-body + weak-title heuristic ──────────────────────────────


def test_short_body_with_no_title_is_challenge() -> None:
    body = "Please complete the verification.\nCheck\nReload"
    assert _looks_like_challenge(body, None, "") is True


def test_short_body_with_untitled_is_challenge() -> None:
    body = "Ваш IP-адрес: 1.2.3.4\nCheck\nReload"
    assert _looks_like_challenge(body, "Untitled", "") is True


def test_short_body_with_real_title_passes() -> None:
    """A genuinely short article (good title, brief body) should not trip."""
    body = "A two-paragraph note about something. " * 5
    assert _looks_like_challenge(body, "On Brevity by Some Author", "") is False


# ── happy path ─────────────────────────────────────────────────────


def test_real_article_passes_through() -> None:
    body = (
        "Lorem ipsum dolor sit amet, consectetur adipiscing elit. "
        "Sed do eiusmod tempor incididunt ut labore et dolore magna aliqua. "
        "Ut enim ad minim veniam, quis nostrud exercitation. " * 10
    )
    html = "<html><body><article>...</article></body></html>"
    assert _looks_like_challenge(body, "Some Real Article", html) is False


def test_none_inputs_dont_crash() -> None:
    """Empty/None inputs must not raise. Empty-text + no-title is treated
    as wall-like since real articles always produce some text — the upstream
    `if not text: raise` is the actual safety net for the empty case."""
    # Should not raise:
    _looks_like_challenge(None, None, None)
    # Truthy non-empty body with no title that's not short isn't wall-like:
    long_body = "real content " * 100
    assert _looks_like_challenge(long_body, None, None) is False
