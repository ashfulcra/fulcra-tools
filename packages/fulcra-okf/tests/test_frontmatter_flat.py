import pytest
from fulcra_okf.frontmatter import parse, dump, FrontmatterError


def test_parse_scalars_lists_and_body():
    text = (
        "---\n"
        "type: BigQuery Table\n"
        "title: Orders\n"
        "tags:\n- sales\n- revenue\n"
        "---\n"
        "Body line.\n"
    )
    fm, body = parse(text)
    assert fm == {"type": "BigQuery Table", "title": "Orders", "tags": ["sales", "revenue"]}
    assert body == "Body line.\n"


def test_parse_no_frontmatter_returns_empty_mapping():
    fm, body = parse("Just a body, no frontmatter.\n")
    assert fm == {}
    assert body == "Just a body, no frontmatter.\n"


def test_parse_missing_close_raises():
    with pytest.raises(FrontmatterError):
        parse("---\ntype: X\nno closing fence\n")


def test_flat_backend_rejects_nested_map():
    with pytest.raises(FrontmatterError):
        parse("---\ntype: X\nmeta:\n  nested: 1\n---\nbody\n")


def test_dump_preserves_insertion_order():
    out = dump({"type": "T", "title": "Z", "tags": ["a", "b"]})
    assert out == "type: T\ntitle: Z\ntags:\n- a\n- b\n"


def test_round_trip_idempotent():
    text = "---\ntype: T\ntitle: Orders\ntags:\n- a\n- b\n---\nbody\n"
    fm, body = parse(text)
    reemitted = "---\n" + dump(fm) + "---\n" + body
    fm2, body2 = parse(reemitted)
    assert fm2 == fm and body2 == body
