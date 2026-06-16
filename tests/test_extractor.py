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


# ── 404 / not-found pages with HTTP 200 status ─────────────────────
# Friction test: PKM + Researcher hit cases where a site served a "404
# Not Found" page with 200 OK (Verge: "404 Not Found | The Verge"),
# extractor saved it as a doc. Title-based detection prevents this.


def test_detects_title_404_not_found() -> None:
    assert _looks_like_challenge("anything", "404 Not Found | The Verge", "") is True


def test_detects_title_page_not_found() -> None:
    assert _looks_like_challenge("anything", "Page Not Found - Example Blog", "") is True


def test_detects_title_this_page_could_not_be_found() -> None:
    assert _looks_like_challenge("anything", "This page could not be found", "") is True


def test_detects_title_page_cant_be_found_smartquote() -> None:
    # Smart-quote variant — many CMSes emit this exact glyph.
    assert _looks_like_challenge("anything", "Page can’t be found", "") is True


def test_legit_article_with_404_in_title_is_not_rejected() -> None:
    """The stems are deliberately specific (e.g., '404 not found', not just
    '404') so an article literally about HTTP 404 doesn't false-positive.
    """
    body = "The HTTP 404 status code has an interesting history. " * 20
    title = "A Brief History of HTTP 404"
    assert _looks_like_challenge(body, title, "") is False


# ── LinkedIn login wall + Twitter no-JS page (surfaced 2026-06-14) ──
# When testing extraction surfaces today, two more "saves garbage as a real
# doc" classes turned up: LinkedIn's "Sign Up | LinkedIn" wall (some profiles
# serve it, A/B'd by IP) and Twitter's "JavaScript is not available." page
# (served when the UA looks JS-disabled, which trafilatura does). Both bypass
# the existing 404 stems; both have bodies > 400 chars so the weak-title
# length check doesn't catch them either.


def test_detects_linkedin_signup_wall() -> None:
    body = (
        "Agree & Join LinkedIn By clicking Continue to join or sign in, "
        "you agree to LinkedIn's User Agreement, Privacy Policy, and Cookie "
        "Policy. By clicking..."
    )
    assert _looks_like_challenge(body, "Sign Up | LinkedIn", "") is True


def test_detects_join_linkedin_variant() -> None:
    assert _looks_like_challenge("anything", "Join LinkedIn", "") is True


def test_detects_twitter_javascript_disabled_page() -> None:
    body = (
        "We've detected that JavaScript is disabled in this browser. "
        "Please enable JavaScript or switch to a supported browser..."
    )
    assert _looks_like_challenge(body, "JavaScript is not available.", "") is True


# ── og:meta fallback hosts bypass (fxtwitter / nitter / vxtwitter) ──
# These hosts deliberately serve short structured og:meta pages. The body-
# length and title heuristics would otherwise classify them as walls — they
# should bypass the heuristic entirely.


def test_fxtwitter_url_bypasses_challenge_heuristic() -> None:
    """Even with short body and generic title (the shape fxtwitter normally
    produces), an fxtwitter URL must not be classified as a challenge."""
    body = "Tweet text"
    title = "Tweet"
    assert (
        _looks_like_challenge(
            body, title, "",
            url="https://fxtwitter.com/karpathy/status/1814038096218857728",
        )
        is False
    )


def test_nitter_url_bypasses_challenge_heuristic() -> None:
    assert (
        _looks_like_challenge(
            None, None, None,
            url="https://nitter.net/karpathy/status/1814038096218857728",
        )
        is False
    )


def test_real_twitter_url_still_detected_as_challenge() -> None:
    """The bypass is host-scoped — twitter.com URLs still go through the
    normal heuristic and get rejected when they look like a wall."""
    assert (
        _looks_like_challenge(
            None, "JavaScript is not available.", "",
            url="https://twitter.com/anyone/status/123",
        )
        is True
    )


def test_fxtwitter_nothing_to_see_here_404_is_detected() -> None:
    """fxtwitter's own 404 page ("Nothing to see here / Looks like this page
    doesn't exist") must still be rejected even though fxtwitter URLs bypass
    the general challenge heuristic. Surfaced 2026-06-14."""
    body = (
        "Looks like this page doesn't exist. Here's a picture of a poodle "
        "sitting in a chair for your trouble."
    )
    assert (
        _looks_like_challenge(
            body, "Nothing to see here", "",
            url="https://fxtwitter.com/karpathy/status/9999999999",
        )
        is True
    )


def test_fxtwitter_real_tweet_still_passes() -> None:
    """A real fxtwitter tweet response (short body, plain title) must not
    be rejected — that's the whole point of the bypass."""
    body = "Some actual tweet content extracted from og:description"
    assert (
        _looks_like_challenge(
            body, "Andrej Karpathy", "",
            url="https://fxtwitter.com/karpathy/status/1234567890",
        )
        is False
    )


# ── fxtwitter wrong-tweet silent corruption (F1, 2026-06-16) ──────
# When fxtwitter (and twitter/x in general) can't find a tweet ID under a
# given account, sometimes the upstream returns a *different* tweet from a
# *different* account instead of a clean 404 — saving that would attribute
# someone else's content to the requested URL's account. Caught in queue
# e2e: URL was /karpathy/status/1234567890; the response carried og:title
# "Stelios (@pathfinderSport)" + Greek tweet body. Cross-check the URL's
# account against the title's @username and reject when they disagree.


def test_fxtwitter_url_account_vs_title_mismatch_is_rejected() -> None:
    body = "Some content actually from another account"
    title = "Stelios (@pathfinderSport)"
    assert (
        _looks_like_challenge(
            body, title, "",
            url="https://fxtwitter.com/karpathy/status/1234567890",
        )
        is True
    )


def test_fxtwitter_url_account_matches_title_account_passes() -> None:
    body = "I have spent more time writing about AI-native services..."
    title = "Andrej Karpathy (@karpathy)"
    assert (
        _looks_like_challenge(
            body, title, "",
            url="https://fxtwitter.com/karpathy/status/1814038096218857728",
        )
        is False
    )


def test_fxtwitter_title_without_username_falls_through() -> None:
    """If the og:title has no `(@handle)` segment, we can't cross-check —
    let it through rather than over-block."""
    body = "Hello world"
    title = "Just a plain tweet display"  # no @username
    assert (
        _looks_like_challenge(
            body, title, "",
            url="https://fxtwitter.com/karpathy/status/1814038096218857728",
        )
        is False
    )


def test_twitter_url_account_extraction_is_case_insensitive() -> None:
    from ampersand_core.extractor import _twitter_account_from_url
    assert _twitter_account_from_url(
        "https://X.com/Karpathy/status/1234567890"
    ) == "karpathy"
    assert _twitter_account_from_url(
        "https://nitter.privacydev.net/elonmusk/status/9"
    ) == "elonmusk"
    assert _twitter_account_from_url("https://example.com/whatever") is None


# ── wrong-title from adjacent article (PKM/Researcher v2 + Dan Luu today) ──
# Stratechery-style bug: trafilatura sometimes picks up a heading from an
# adjacent article on the same page (sidebar listings, related-posts blocks,
# appendix linkbacks). The cross-check against body content lets og:title
# take over when trafilatura's pick doesn't appear in the body's opening.


def test_pick_best_title_prefers_trafilatura_when_it_matches_body() -> None:
    """Happy path — trafilatura's heading is the actual article title and
    appears in the body. Keep it."""
    from ampersand_core.extractor import _pick_best_title
    traf = "Diseconomies of Scale"
    og = "Diseconomies of Scale"
    body = "# Diseconomies of Scale\n\nLong-form article about how companies..."
    assert _pick_best_title(traf, og, None, body) == traf


def test_pick_best_title_falls_back_to_og_when_traf_title_isnt_in_body() -> None:
    """The Dan Luu case: trafilatura picked an appendix heading from another
    post; that title's words don't appear in the body. og:title is what
    we should save."""
    from ampersand_core.extractor import _pick_best_title
    traf = "Appendix: techniques that only work at small scale"
    og = "Keyboard latency"
    body = (
        "# Keyboard latency\n\n"
        "Last year, I bought a fancy keyboard with low latency. "
        "Then I started measuring keyboards everyone uses..."
    )
    assert _pick_best_title(traf, og, None, body) == og


def test_pick_best_title_falls_back_to_html_title_when_no_og() -> None:
    from ampersand_core.extractor import _pick_best_title
    traf = "Some Sidebar Heading"
    body = "Article body about completely different things"
    assert _pick_best_title(traf, None, "Real Page Title", body) == "Real Page Title"


def test_pick_best_title_keeps_traf_when_no_alternatives_even_if_bodyless() -> None:
    """Last-resort: if og:title and <title> are both absent, return
    trafilatura's pick even when it doesn't appear in the body. Better than
    None — caller can still decide to reject elsewhere."""
    from ampersand_core.extractor import _pick_best_title
    assert _pick_best_title("Whatever Title", None, None, "Unrelated body") == "Whatever Title"


def test_extract_og_title_handles_attribute_order_and_entities() -> None:
    from ampersand_core.extractor import _extract_og_title
    # Normal order
    html1 = '<meta property="og:title" content="It&#39;s a Test &amp; More">'
    assert _extract_og_title(html1) == "It's a Test & More"
    # Reverse order (content before property)
    html2 = '<meta content="Other order" property="og:title">'
    assert _extract_og_title(html2) == "Other order"
    # Missing
    assert _extract_og_title("<html><head></head></html>") is None


def test_title_in_body_is_robust_to_punctuation_and_case() -> None:
    from ampersand_core.extractor import _title_appears_in_body
    assert _title_appears_in_body(
        "Diseconomies of Scale",
        "Some intro\n# Diseconomies of scale (a brief look)\n\nDetails",
    )
    # Punctuation difference shouldn't tank the check
    assert _title_appears_in_body(
        "How to Make Wealth",
        "How to make wealth - paul graham essay reposted here",
    )
    # Wildly different — should NOT match
    assert not _title_appears_in_body(
        "Brazilian Funk and Miami Bass",
        "An essay about diseconomies of scale at large companies",
    )


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
