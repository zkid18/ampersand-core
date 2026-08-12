from email.message import EmailMessage

from amperstand_core.email_parser import _clean_markdown, extract_from_message


def test_view_in_browser_preheader_does_not_discard_body() -> None:
    markdown = """[View in browser](https://example.com/view)

The paid newsletter body starts here.

And continues here.
"""

    assert _clean_markdown(markdown) == (
        "The paid newsletter body starts here.\n\nAnd continues here."
    )


def test_view_this_email_after_banner_does_not_discard_body() -> None:
    markdown = """![](https://example.com/banner.png)

[View this email](https://example.com/view)

The newsletter body starts here.
"""

    assert _clean_markdown(markdown) == (
        "![](https://example.com/banner.png)\n\nThe newsletter body starts here."
    )


def test_real_footer_marker_still_discards_footer() -> None:
    markdown = """The newsletter body.

[Unsubscribe](https://example.com/unsubscribe)

123 Example Street
"""

    assert _clean_markdown(markdown) == "The newsletter body."


def test_plain_text_fallback_when_html_cleans_to_empty() -> None:
    message = EmailMessage()
    message["Subject"] = "Newsletter"
    message["From"] = "Writer <writer@example.com>"
    message.set_content("The complete plain-text newsletter body.")
    message.add_alternative(
        '<html><body><a href="https://example.com">View in browser</a></body></html>',
        subtype="html",
    )

    result = extract_from_message(message)

    assert result.content_markdown == "The complete plain-text newsletter body."
