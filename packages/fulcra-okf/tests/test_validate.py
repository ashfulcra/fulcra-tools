import fulcra_okf.frontmatter as fm
from fulcra_okf.bundle import Bundle
from fulcra_okf.concept import Concept
from fulcra_okf.validate import validate


def _bundle(concepts, parse_errors=None):
    b = Bundle()
    for c in concepts:
        b.concepts[c.id] = c
    b.parse_errors = parse_errors or []
    return b


def test_conformant_bundle_has_no_errors():
    b = _bundle([Concept(id="a", type="T")])
    report = validate(b)
    assert report.conformant is True
    assert all(f.severity != "error" for f in report.findings)


def test_empty_type_is_hard_error():
    b = _bundle([Concept(id="a", type="")])
    report = validate(b)
    assert report.conformant is False
    assert any(f.code == "missing_type" and f.severity == "error" for f in report.findings)


def test_broken_link_is_info_not_error_by_default():
    c = Concept(id="a", type="T", body="See [x](/missing.md).\n")
    report = validate(_bundle([c]))
    assert report.conformant is True
    assert any(f.code == "broken_link" and f.severity == "info" for f in report.findings)


def test_strict_elevates_broken_link_to_error():
    c = Concept(id="a", type="T", body="See [x](/missing.md).\n")
    report = validate(_bundle([c]), strict=True)
    assert report.conformant is False
    assert any(f.code == "broken_link" and f.severity == "error" for f in report.findings)


def test_parse_error_flat_backend_is_actionable(monkeypatch):
    monkeypatch.setattr(fm, "BACKEND", "flat")
    b = _bundle([], parse_errors=[("bad.md", "flat backend cannot represent")])
    report = validate(b)
    assert report.conformant is False
    f = next(f for f in report.findings if f.path == "bad.md")
    assert f.code == "flat_backend_cannot_parse"
    assert "PyYAML" in f.message


def test_parse_error_pyyaml_backend_is_invalid(monkeypatch):
    monkeypatch.setattr(fm, "BACKEND", "pyyaml")
    b = _bundle([], parse_errors=[("bad.md", "invalid YAML frontmatter")])
    report = validate(b)
    assert any(f.code == "unparseable" and f.severity == "error" for f in report.findings)
