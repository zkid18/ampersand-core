"""Tests for extractor heuristics that don't require network access."""

from __future__ import annotations

from ampersand_core.extractor import _looks_like_challenge


def test_detects_russian_ip_challenge() -> None:
    body = (
        "Пожалуйста, пройдите проверку.\n"
        "Ваш IP-адрес: 141.98.143.94\n"
        "Check\nReload"
    )
    assert _looks_like_challenge(body, None) is True


def test_detects_cloudflare_just_a_moment() -> None:
    assert _looks_like_challenge("anything", "Just a moment...") is True


def test_detects_cloudflare_attention_required() -> None:
    body = "Attention Required! | Cloudflare\nDDoS protection by Cloudflare\n"
    assert _looks_like_challenge(body, None) is True


def test_real_article_passes_through() -> None:
    body = (
        "Lorem ipsum dolor sit amet, consectetur adipiscing elit. "
        "Sed do eiusmod tempor incididunt ut labore et dolore magna aliqua. "
        "Ut enim ad minim veniam, quis nostrud exercitation. " * 10
    )
    assert _looks_like_challenge(body, "Some Real Article") is False


def test_empty_text_is_not_a_challenge() -> None:
    assert _looks_like_challenge("", None) is False
