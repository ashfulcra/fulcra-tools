import pytest
import fulcra_okf.frontmatter as fm


def test_backend_is_known_value():
    assert fm.BACKEND in ("pyyaml", "flat")


def test_pyyaml_backend_parses_nested(monkeypatch):
    pytest.importorskip("yaml")
    monkeypatch.setattr(fm, "BACKEND", "pyyaml")
    mapping, body = fm.parse("---\ntype: X\nmeta:\n  nested: 1\n---\nbody\n")
    assert mapping["type"] == "X"
    assert mapping["meta"] == {"nested": 1}
    assert body == "body\n"


def test_flat_backend_still_rejects_nested(monkeypatch):
    monkeypatch.setattr(fm, "BACKEND", "flat")
    with pytest.raises(fm.FrontmatterError):
        fm.parse("---\ntype: X\nmeta:\n  nested: 1\n---\nbody\n")


def test_pyyaml_invalid_yaml_raises_frontmatter_error(monkeypatch):
    pytest.importorskip("yaml")
    monkeypatch.setattr(fm, "BACKEND", "pyyaml")
    with pytest.raises(fm.FrontmatterError):
        fm.parse("---\ntype: X\n: : :\n bad\n---\nbody\n")


# ---------------------------------------------------------------------------
# I1 — backend matrix + YAML-magic round-trip coverage
# ---------------------------------------------------------------------------

# Raw frontmatter blocks exercising rich types: timestamp, date, and magic
# scalars (yes -> True, on -> True, leading-zero string "007").
_TIMESTAMP_BLOCK = "---\ntype: T\ntimestamp: 2026-05-28T14:30:00Z\n---\nbody\n"
_DATE_BLOCK = "---\ntype: T\ndate_field: 2026-05-28\n---\nbody\n"
# plain-string "007" should survive as a string (quoted in YAML output)
_MAGIC_BLOCK = '---\ntype: T\ncode: "007"\nflag: yes\n---\nbody\n'


def _round_trip(text, backend, monkeypatch):
    """parse -> dump -> parse; return (first_mapping, second_mapping)."""
    monkeypatch.setattr(fm, "BACKEND", backend)
    first, body = fm.parse(text)
    dumped = fm.dump(first)
    second, _ = fm.parse("---\n" + dumped + "---\n" + body)
    return first, second


def test_pyyaml_round_trip_timestamp(monkeypatch):
    pytest.importorskip("yaml")
    first, second = _round_trip(_TIMESTAMP_BLOCK, "pyyaml", monkeypatch)
    assert second["type"] == first["type"]
    assert second["timestamp"] == first["timestamp"]


def test_pyyaml_round_trip_date(monkeypatch):
    pytest.importorskip("yaml")
    first, second = _round_trip(_DATE_BLOCK, "pyyaml", monkeypatch)
    assert second["date_field"] == first["date_field"]


def test_pyyaml_round_trip_magic_scalars(monkeypatch):
    """yes/on parsed as True; "007" stays as the string "007"."""
    pytest.importorskip("yaml")
    monkeypatch.setattr(fm, "BACKEND", "pyyaml")
    # Parse once — yaml.safe_load turns yes -> True and "007" -> string "007"
    first, body = fm.parse(_MAGIC_BLOCK)
    assert first["flag"] is True
    assert first["code"] == "007"
    # dump must not raise, and re-parse must be stable
    dumped = fm.dump(first)
    second, _ = fm.parse("---\n" + dumped + "---\n" + body)
    assert second == first


def test_flat_round_trip_magic_scalars_stay_strings(monkeypatch):
    """Under the flat backend magic scalars stay strings and round-trip is stable."""
    monkeypatch.setattr(fm, "BACKEND", "flat")
    # Under the flat backend "yes" stays as the string "yes"
    text = "---\ntype: T\ncode: \"007\"\n---\nbody\n"
    first, body = fm.parse(text)
    dumped = fm.dump(first)
    second, _ = fm.parse("---\n" + dumped + "---\n" + body)
    assert second == first


def test_flat_round_trip_dump_never_raises_on_its_own_output(monkeypatch):
    """dump(parse(x)) must not raise for flat-parseable input."""
    monkeypatch.setattr(fm, "BACKEND", "flat")
    text = "---\ntype: T\ntitle: hello\ncount: 42\n---\nbody\n"
    first, _ = fm.parse(text)
    # should not raise
    dumped = fm.dump(first)
    assert dumped
