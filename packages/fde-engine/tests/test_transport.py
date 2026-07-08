"""Transport parsing + the fake used by every other test module."""

import pytest

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


# --- Fix I3: missing/hung backing CLI must surface as TransportError,
# not a raw traceback from subprocess ---------------------------------------


def test_missing_cli_binary_raises_transport_error_not_file_not_found():
    t = transport.FulcraFileTransport(command=["/nonexistent-fde-test-binary"])
    with pytest.raises(transport.TransportError):
        t.read("fde/engagements/x/engagement.md")


def test_missing_cli_binary_error_message_is_actionable():
    t = transport.FulcraFileTransport(command=["/nonexistent-fde-test-binary"])
    with pytest.raises(transport.TransportError, match="nonexistent-fde-test-binary"):
        t.read("fde/engagements/x/engagement.md")


def test_hung_cli_binary_raises_transport_error_on_timeout():
    t = transport.FulcraFileTransport(
        command=["python3", "-c", "import time; time.sleep(5)"], timeout=0.2
    )
    with pytest.raises(transport.TransportError, match="timed out"):
        t.read("fde/engagements/x/engagement.md")
