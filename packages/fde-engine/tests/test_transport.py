"""Transport parsing + the fake used by every other test module."""

from fde_engine import transport
from fde_engine_test_helpers import FakeTransport


def test_parse_list_output_handles_files_and_dirs():
    text = (
        "350B    2026-07-07 09:45PM UTC  brief.md\n"
        "intake/\n"
        "\n"
    )
    entries = transport.parse_list_output(text)
    names = {e["name"]: e["is_dir"] for e in entries}
    assert names == {"brief.md": False, "intake/": True}


def test_fake_transport_roundtrip_and_listing():
    t = FakeTransport()
    assert t.read("fde/engagements/x/engagement.md") is None
    assert t.write("fde/engagements/x/engagement.md", "hello")
    assert t.read("fde/engagements/x/engagement.md") == "hello"
    t.write("fde/engagements/x/intake/brief.md", "b")
    listing = t.list_dir("fde/engagements/x/")
    assert [(e["name"], e["is_dir"]) for e in listing] == [
        ("engagement.md", False), ("intake/", True),
    ]


def test_fake_transport_delete():
    t = FakeTransport()
    t.write("a/b.md", "x")
    assert t.delete("a/b.md") is True
    assert t.read("a/b.md") is None
    assert t.delete("a/b.md") is False
