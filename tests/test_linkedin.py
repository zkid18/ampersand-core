"""Tests for LinkedIn video extraction."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ampersand_core.extractor import is_linkedin_url
from ampersand_core.linkedin import _parse_webvtt, extract_linkedin
from ampersand_core.models import ContentType


# ── URL detection ────────────────────────────────────────────────────


class TestIsLinkedInUrl:
    @pytest.mark.parametrize(
        "url",
        [
            "https://www.linkedin.com/posts/johndoe_some-slug-ugcPost-1234567890",
            "https://www.linkedin.com/posts/janedoe_topic-activity-9876543210",
            "https://www.linkedin.com/feed/update/urn:li:ugcPost:1234567890",
            "https://www.linkedin.com/feed/update/urn:li:activity:9876543210",
            "https://www.linkedin.com/video/some-video-id",
            "https://www.linkedin.com/embed/feed/update/urn:li:ugcPost:1234567890",
            "https://www.linkedin.com/embed/feed/update/urn:li:activity:9876543210",
            "https://linkedin.com/posts/user_topic-activity-123456",
        ],
    )
    def test_valid_linkedin_urls(self, url: str):
        assert is_linkedin_url(url) is True

    @pytest.mark.parametrize(
        "url",
        [
            "https://www.linkedin.com/in/johndoe",
            "https://www.linkedin.com/company/acme",
            "https://www.linkedin.com/jobs/view/12345",
            "https://www.youtube.com/watch?v=abc123def45",
            "https://www.google.com",
            "https://www.linkedin.com/messaging/thread/123",
            "https://example.com/linkedin.com/posts/fake-ugcPost",
        ],
    )
    def test_non_linkedin_urls(self, url: str):
        assert is_linkedin_url(url) is False


# ── Full pipeline (mocked) ──────────────────────────────────────────


class TestExtractLinkedIn:
    @patch("ampersand_core.linkedin._transcribe_video")
    @patch("ampersand_core.linkedin._download_video")
    @patch("ampersand_core.linkedin._inspect_post")
    def test_full_pipeline_with_asr(self, mock_inspect, mock_download, mock_transcribe):
        """Video URL found, no captions — falls back to ASR."""
        mock_inspect.return_value = (
            "https://dms.licdn.com/playlist/vid/v2/xxx/mp4-720p/0/123",
            None,  # no captions
            "John Doe on LinkedIn: Great talk about AI",
            "John Doe",
            None,  # description (unused for video path)
        )
        mock_download.return_value = None
        mock_transcribe.return_value = "This is a test transcript about AI."

        result = extract_linkedin("https://www.linkedin.com/posts/johndoe_activity-123")

        assert result.content_type == ContentType.VIDEO
        assert result.title == "John Doe on LinkedIn: Great talk about AI"
        assert result.author == "John Doe"
        assert result.url == "https://www.linkedin.com/posts/johndoe_activity-123"
        assert "**Author**: John Doe" in result.content_markdown
        assert "## Transcript" in result.content_markdown
        assert "This is a test transcript about AI." in result.content_markdown

        mock_inspect.assert_called_once()
        mock_download.assert_called_once()
        mock_transcribe.assert_called_once()

    @patch("ampersand_core.linkedin._fetch_captions")
    @patch("ampersand_core.linkedin._inspect_post")
    def test_full_pipeline_with_captions(self, mock_inspect, mock_fetch_captions):
        """Captions URL found — uses captions, skips ASR."""
        mock_inspect.return_value = (
            "https://dms.licdn.com/playlist/vid/v2/xxx/mp4-720p/0/123",
            "https://dms.licdn.com/playlist/vid/v2/xxx/video-captions-webvtt/0/123",
            "Jane Smith on LinkedIn: Product launch",
            "Jane Smith",
            None,
        )
        mock_fetch_captions.return_value = "Welcome to our product launch."

        result = extract_linkedin("https://www.linkedin.com/posts/janesmith_activity-456")

        assert result.content_type == ContentType.VIDEO
        assert result.author == "Jane Smith"
        assert "Welcome to our product launch." in result.content_markdown
        assert "## Transcript" in result.content_markdown
        mock_fetch_captions.assert_called_once()


class TestNoTranscript:
    @patch("ampersand_core.linkedin._transcribe_video")
    @patch("ampersand_core.linkedin._download_video")
    @patch("ampersand_core.linkedin._inspect_post")
    def test_empty_transcript_fallback(self, mock_inspect, mock_download, mock_transcribe):
        mock_inspect.return_value = (
            "https://dms.licdn.com/playlist/vid/v2/xxx/mp4-720p/0/123",
            None,
            "LinkedIn Video",
            None,
            None,
        )
        mock_download.return_value = None
        mock_transcribe.return_value = ""

        result = extract_linkedin("https://www.linkedin.com/posts/user_activity-456")

        assert result.content_type == ContentType.VIDEO
        assert "## Transcript" not in result.content_markdown
        assert "*No speech detected in video.*" in result.content_markdown
        assert "**Author**" not in result.content_markdown


class TestTextPostStub:
    """No video stream — fall back to og:meta stub instead of raising 422."""

    @patch("ampersand_core.linkedin._inspect_post")
    def test_text_post_with_description(self, mock_inspect):
        mock_inspect.return_value = (
            None,  # no video
            None,  # no captions
            "Olga Maslikhova on LinkedIn: Another billion dollar business idea",
            "Olga Maslikhova",
            "If I were to start a business in Brazil today, here's what I'd do:",
        )

        result = extract_linkedin(
            "https://www.linkedin.com/feed/update/urn:li:activity:7469122525881532416/"
        )

        assert result.content_type == ContentType.ARTICLE
        assert result.author == "Olga Maslikhova"
        assert "**Author**: Olga Maslikhova" in result.content_markdown
        assert "business in Brazil" in result.content_markdown
        assert "## Transcript" not in result.content_markdown

    @patch("ampersand_core.linkedin._inspect_post")
    def test_text_post_no_description_falls_back_to_hint(self, mock_inspect):
        mock_inspect.return_value = (None, None, "LinkedIn post", None, None)

        result = extract_linkedin("https://www.linkedin.com/feed/update/urn:li:activity:999/")

        assert result.content_type == ContentType.ARTICLE
        assert "not visible without authentication" in result.content_markdown.lower()


# ── WebVTT parsing ──────────────────────────────────────────────────


class TestParseWebVTT:
    def test_basic_vtt(self):
        vtt = """WEBVTT

1
00:00:00.000 --> 00:00:02.500
Hello everyone.

2
00:00:02.500 --> 00:00:05.000
Welcome to the presentation.
"""
        assert _parse_webvtt(vtt) == "Hello everyone. Welcome to the presentation."

    def test_deduplicates_lines(self):
        vtt = """WEBVTT

00:00:00.000 --> 00:00:02.000
Hello

00:00:02.000 --> 00:00:04.000
Hello

00:00:04.000 --> 00:00:06.000
World
"""
        assert _parse_webvtt(vtt) == "Hello World"

    def test_strips_html_tags(self):
        vtt = """WEBVTT

00:00:00.000 --> 00:00:02.000
<b>Bold text</b> and <i>italic</i>
"""
        assert _parse_webvtt(vtt) == "Bold text and italic"

    def test_empty_vtt(self):
        assert _parse_webvtt("WEBVTT\n\n") == ""
