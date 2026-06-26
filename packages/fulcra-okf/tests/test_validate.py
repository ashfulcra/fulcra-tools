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


# --- Finding 2: log.md reserved-file structure enforcement ---

def _bundle_with_log(log_text, concepts=None):
    """Helper: build a Bundle with a root log.md and optional concepts."""
    b = _bundle(concepts or [Concept(id="a", type="T")])
    b.reserved_files = {"log.md": log_text}
    return b


def test_sample_bundle_still_conformant_default(monkeypatch):
    """Existing bundle without log.md must stay conformant in default mode."""
    b = _bundle([Concept(id="a", type="T")])
    # reserved_files is empty — no log.md, so no log findings
    assert b.reserved_files == {}
    report = validate(b)
    assert report.conformant is True


def test_sample_bundle_still_conformant_strict(monkeypatch):
    """Existing bundle without log.md must stay conformant in strict mode."""
    b = _bundle([Concept(id="a", type="T")])
    assert b.reserved_files == {}
    report = validate(b, strict=True)
    assert report.conformant is True


def test_log_md_bad_heading_is_warn_default():
    """A log.md with a non-date L2 heading emits a warn in default mode (still conformant)."""
    log_text = "# Log\n\n## not-a-date\n* Entry\n"
    b = _bundle_with_log(log_text)
    report = validate(b)
    assert report.conformant is True  # warn only, no error
    assert any(
        f.code == "reserved_log_bad_heading" and f.severity == "warn"
        for f in report.findings
    )


def test_log_md_bad_heading_is_error_strict():
    """A log.md with a non-date L2 heading causes non-conformance under strict=True."""
    log_text = "# Log\n\n## not-a-date\n* Entry\n"
    b = _bundle_with_log(log_text)
    report = validate(b, strict=True)
    assert report.conformant is False
    assert any(
        f.code == "reserved_log_bad_heading" and f.severity == "error"
        for f in report.findings
    )


def test_log_md_well_formed_newest_first_is_conformant_strict():
    """A well-formed log.md with newest-first dates is conformant under strict=True."""
    log_text = (
        "# Directory Update Log\n\n"
        "## 2026-06-20\n* Update A\n\n"
        "## 2026-06-19\n* Update B\n"
    )
    b = _bundle_with_log(log_text)
    report = validate(b, strict=True)
    assert report.conformant is True
    assert not any(
        f.code in ("reserved_log_bad_heading", "reserved_log_out_of_order")
        for f in report.findings
    )


def test_log_md_out_of_order_dates_is_warn_default():
    """An out-of-order log.md emits a warn in default mode (still conformant)."""
    log_text = (
        "# Log\n\n"
        "## 2026-06-19\n* Older first\n\n"
        "## 2026-06-20\n* Newer second (wrong order)\n"
    )
    b = _bundle_with_log(log_text)
    report = validate(b)
    assert report.conformant is True
    assert any(
        f.code == "reserved_log_out_of_order" and f.severity == "warn"
        for f in report.findings
    )


def test_log_md_out_of_order_dates_is_error_strict():
    """An out-of-order log.md causes non-conformance under strict=True."""
    log_text = (
        "# Log\n\n"
        "## 2026-06-19\n* Older first\n\n"
        "## 2026-06-20\n* Newer second (wrong order)\n"
    )
    b = _bundle_with_log(log_text)
    report = validate(b, strict=True)
    assert report.conformant is False
    assert any(
        f.code == "reserved_log_out_of_order" and f.severity == "error"
        for f in report.findings
    )
