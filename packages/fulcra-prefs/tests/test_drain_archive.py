"""Re-draining the same (platform, session) must not clobber the prior archive.

mark_captured renamed the queue file to a fixed `<name>.captured`. A second
drain (after new notices land in a fresh queue file) overwrote the first
archive via Path.replace, destroying the earlier captured record.
"""
import json

from fulcra_prefs.candidates import mark_captured


def test_mark_captured_does_not_clobber_prior_archive(tmp_path):
    p = tmp_path / "s1.json"
    p.write_text(json.dumps([{"key": "k.one"}]))
    a = mark_captured(p)

    p.write_text(json.dumps([{"key": "k.two"}]))   # new notices, same session
    b = mark_captured(p)

    assert a != b
    assert a.exists() and b.exists()
    keys = sorted(c["key"] for arch in (a, b) for c in json.loads(arch.read_text()))
    assert keys == ["k.one", "k.two"]
