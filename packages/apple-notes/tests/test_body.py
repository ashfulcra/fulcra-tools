"""Body decoding: protobuf wire format -> text -> markdown."""
from __future__ import annotations

import pytest

from fulcra_apple_notes import protobuf as pb
from fulcra_apple_notes.body import BodyDecodeError, decode, decompress
from apple_notes_test_helpers import make_body, make_run, pb_str, pb_varint


def test_varint_multibyte_roundtrip():
    msg = pb.parse(pb_varint(1, 300) + pb_varint(2, 1))
    assert msg[1] == [300]


def test_repeated_field_keeps_every_value_not_just_the_last():
    # Losing repeats would silently drop attribute runs.
    msg = pb.parse(pb_varint(1, 7) + pb_varint(1, 9))
    assert msg[1] == [7, 9]


def test_truncated_varint_is_an_error_not_a_silent_zero():
    with pytest.raises(pb.ProtobufError):
        pb.parse(b"\x08\x80")


def test_length_overrun_is_rejected():
    with pytest.raises(pb.ProtobufError):
        pb.parse(b"\x12\x10short")


def test_decompress_passes_through_non_gzip():
    assert decompress(b"raw-bytes") == b"raw-bytes"


def test_decompress_rejects_corrupt_gzip():
    with pytest.raises(BodyDecodeError):
        decompress(b"\x1f\x8b" + b"garbage")


def test_plain_text_note():
    blob = make_body("Hello\nWorld", [make_run(11)])
    assert decode(blob).markdown == "Hello\nWorld"


def test_title_and_heading_styles_become_markdown_headings():
    blob = make_body("Title\nHeading\nbody", [
        make_run(6, style=0), make_run(8, style=1), make_run(4)])
    assert decode(blob).markdown == "# Title\n## Heading\nbody"


def test_checkbox_style_renders_checked_and_unchecked():
    blob = make_body("done\ntodo", [
        make_run(5, style=103, checked=True),
        make_run(4, style=103, checked=False)])
    assert decode(blob).markdown == "- [x] done\n- [ ] todo"


def test_numbered_list_restarts_after_a_non_list_paragraph():
    blob = make_body("one\ntwo\nbreak\nthree", [
        make_run(4, style=102), make_run(4, style=102),
        make_run(6), make_run(5, style=102)])
    lines = decode(blob).markdown.split("\n")
    assert lines[0].startswith("1.") and lines[1].startswith("2.")
    assert lines[3].startswith("1.")


def test_attachment_placeholder_becomes_a_token_carrying_its_id():
    blob = make_body("see ￼", [make_run(4), make_run(1, attachment_id="att-9")])
    decoded = decode(blob)
    assert "{{attachment:att-9}}" in decoded.markdown
    assert decoded.attachment_ids == ["att-9"]


def test_placeholder_without_an_attachment_run_degrades_visibly():
    # It must not vanish: the user needs to know something was there.
    blob = make_body("see ￼", [make_run(5)])
    assert "*(attachment)*" in decode(blob).markdown


def test_body_that_is_not_protobuf_raises_rather_than_returning_empty():
    # Returning "" here would let the sync overwrite a good vault note.
    with pytest.raises(BodyDecodeError):
        decode(b"\x1f\x8b\x08\x00" + b"\x00" * 20)


def test_missing_document_field_is_an_error():
    import gzip
    with pytest.raises(BodyDecodeError):
        decode(gzip.compress(pb_varint(1, 5)))


def test_indent_is_applied_to_nested_list_items():
    blob = make_body("top\nnested", [
        make_run(4, style=100), make_run(6, style=100, indent=1)])
    assert decode(blob).markdown.split("\n")[1].startswith("    - ")
