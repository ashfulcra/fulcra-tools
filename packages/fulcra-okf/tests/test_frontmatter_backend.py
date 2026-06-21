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
