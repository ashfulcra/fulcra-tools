"""convert.py: Gmail messages.get(full) payload → selected-email JSON."""
from __future__ import annotations

from email.header import Header

from fulcra_gmail import convert

from .conftest import attachment_part, b64url, header, make_message, text_part


def test_multipart_alternative_text_and_html_bodies():
    payload = {
        "mimeType": "multipart/alternative",
        "headers": [
            header("From", "sender@example.com"),
            header("To", "me@example.com"),
            header("Subject", "Hello"),
        ],
        "parts": [
            text_part("text/plain", "the plain body"),
            text_part("text/html", "<p>the html body</p>"),
        ],
    }
    msg = make_message(payload=payload)
    out = convert.to_selected_email(msg)

    assert out["message_id"] == "m1"
    assert out["thread_id"] == "t1"
    assert out["bodies"]["text/plain"] == "the plain body"
    assert out["bodies"]["text/html"] == "<p>the html body</p>"
    assert out["attachments"] == []


def test_header_subset_extracted():
    headers = [
        header("From", "sender@example.com"),
        header("To", "me@example.com"),
        header("Cc", "other@example.com"),
        header("Subject", "A subject"),
        header("Date", "Mon, 1 Jan 2026 00:00:00 +0000"),
        header("Message-ID", "<abc@example.com>"),
        header("X-Ignored", "should not appear"),
    ]
    out = convert.to_selected_email(make_message(headers=headers))
    assert out["headers"] == {
        "From": "sender@example.com",
        "To": "me@example.com",
        "Cc": "other@example.com",
        "Subject": "A subject",
        "Date": "Mon, 1 Jan 2026 00:00:00 +0000",
        "Message-ID": "<abc@example.com>",
    }


def test_rfc2047_subject_decoded():
    encoded = Header("Café Receipt", "utf-8").encode()
    assert encoded != "Café Receipt"  # sanity: it really is encoded-word form
    out = convert.to_selected_email(
        make_message(headers=[header("Subject", encoded)])
    )
    assert out["headers"]["Subject"] == "Café Receipt"


def test_multipart_mixed_attachment_metadata_only():
    payload = {
        "mimeType": "multipart/mixed",
        "headers": [header("Subject", "invoice attached")],
        "parts": [
            {
                "mimeType": "multipart/alternative",
                "parts": [
                    text_part("text/plain", "see attached"),
                    text_part("text/html", "<p>see attached</p>"),
                ],
            },
            attachment_part("invoice.pdf", "application/pdf", "att-9", 2048),
        ],
    }
    out = convert.to_selected_email(make_message(payload=payload))

    # Nested multipart/alternative bodies still surface.
    assert out["bodies"]["text/plain"] == "see attached"
    assert out["bodies"]["text/html"] == "<p>see attached</p>"

    # Attachment = metadata ONLY; no bytes anywhere.
    assert out["attachments"] == [{
        "filename": "invoice.pdf",
        "mimeType": "application/pdf",
        "size": 2048,
        "attachmentId": "att-9",
    }]
    assert "data" not in out["attachments"][0]


def test_padding_stripped_base64_decodes():
    # 'abc' base64url has padding; conftest.b64url strips it (as Gmail does).
    raw = b64url("abc")
    assert not raw.endswith("=")
    payload = {"mimeType": "text/plain", "body": {"data": raw},
               "headers": [header("Subject", "s")]}
    out = convert.to_selected_email(make_message(payload=payload))
    assert out["bodies"]["text/plain"] == "abc"


def test_single_part_plain_message():
    payload = {
        "mimeType": "text/plain",
        "headers": [header("Subject", "plain")],
        "body": {"data": b64url("just text")},
    }
    out = convert.to_selected_email(make_message(payload=payload))
    assert out["bodies"]["text/plain"] == "just text"
    assert "text/html" not in out["bodies"]


def test_deterministic_output_is_json_serializable():
    import json

    out = convert.to_selected_email(
        make_message(headers=[header("Subject", "s")])
    )
    # round-trips without error and is stable
    assert json.loads(json.dumps(out)) == out
