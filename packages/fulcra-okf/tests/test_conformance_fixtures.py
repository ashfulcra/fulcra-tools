from pathlib import Path

from fulcra_okf.bundle import Bundle
from fulcra_okf.validate import validate

FIX = Path(__file__).parent / "fixtures" / "sample_bundle"


def test_sample_bundle_is_conformant():
    report = validate(Bundle.load_dir(FIX, lenient=True))
    assert report.conformant, [vars(f) for f in report.findings if f.severity == "error"]


def test_sample_bundle_has_no_broken_links_in_strict_mode():
    report = validate(Bundle.load_dir(FIX, lenient=True), strict=True)
    assert report.conformant, [vars(f) for f in report.findings if f.severity == "error"]
