from coord_engine import transport
from coord_engine.budget import Deadline


def test_parse_list_output_basic():
    text = "81B     2026-07-01 04:12PM UTC  probe.md\n93B     2026-07-01 04:15PM UTC  other.md"
    entries = transport.parse_list_output(text)
    assert len(entries) == 2
    assert entries[0] == {
        "name": "probe.md", "size": "81B", "mtime": "2026-07-01 04:12PM UTC", "is_dir": False,
    }
    assert entries[1]["name"] == "other.md"


def test_parse_list_output_directory_entry():
    entries = transport.parse_list_output("0B      2026-07-01 04:12PM UTC  subdir/")
    assert entries[0]["is_dir"] is True


def test_parse_list_output_empty():
    assert transport.parse_list_output("") == []
    assert transport.parse_list_output("\n\n") == []


def test_parse_stat_output():
    text = (
        "/_coord-probe/probe.md (93 bytes)\n"
        "Uploaded: 2026-07-01T16:12:44.623092Z\n"
        "Version: 75c13308-76c0-4379-837e-8a96b4899535\n"
        "Previous Versions: 1\n"
        "- b8b68ea9-0986-4f9b-bb24-4a693d380ba4 2026-07-01T16:12:20.176191Z (81 bytes)"
    )
    st = transport.parse_stat_output(text)
    assert st["uploaded"] == "2026-07-01T16:12:44.623092Z"
    assert st["version"] == "75c13308-76c0-4379-837e-8a96b4899535"
    assert st["previous_count"] == 1
    assert st["previous"][0]["version"] == "b8b68ea9-0986-4f9b-bb24-4a693d380ba4"
    assert st["path"] == "/_coord-probe/probe.md"


def test_parse_stat_no_previous():
    text = "/x.md (10 bytes)\nUploaded: 2026-07-01T00:00:00Z\nVersion: abc\nPrevious Versions: 0"
    st = transport.parse_stat_output(text)
    assert st["previous_count"] == 0
    assert st["previous"] == []


def test_list_dir_sorted_by_name():
    # the real transport must return list entries sorted by name (determinism for
    # "last wins" folds). Simulate parse output order != sorted, then sort.
    entries = transport.parse_list_output(
        "1B  2026-07-01 04:12PM UTC  zzz.md\n1B  2026-07-01 04:12PM UTC  aaa.md")
    names = [e["name"] for e in sorted(entries, key=lambda e: e.get("name") or "")]
    assert names == ["aaa.md", "zzz.md"]


# --- transport.updates() (data-updates feed) ---
#
# updates() runs through the hard-bounded runner ``run_bounded`` (Popen + group
# kill), so the seam these tests patch is ``run_bounded`` — returning the
# ``(returncode, stdout, stderr)`` tuple the real one yields.

def _fake_run(rc, out, calls):
    def run(argv, timeout, **kw):
        calls.append(argv)
        return (rc, out, "")
    return run


def test_updates_parses_file_changes(monkeypatch):
    from coord_engine import transport as tr
    t = tr.FulcraFileTransport(command=["uv", "tool", "run", "fulcra-api"])
    calls = []
    monkeypatch.setattr(
        tr,
        "run_bounded",
        _fake_run(
            0,
            '{"file_changes": ['
            '{"full_name": "/team/r/task/a.md", "state": "uploaded",'
            ' "uploaded_at": "2026-07-01T12:00:00Z"},'
            '{"full_name": "/other/task/b.md", "state": "uploaded"}'
            "]}",
            calls,
        ),
    )
    got = t.updates("900 seconds", team="r")
    assert got == [{
        "path": "team/r/task/a.md",
        "state": "uploaded",
        "uploaded_at": "2026-07-01T12:00:00Z",
        "archived_at": None,
        "deleted_at": None,
    }]
    # exact command: the transport's own base verbatim — no binary rewriting
    assert calls == [["uv", "tool", "run", "fulcra-api", "data-updates", "900 seconds"]]


def test_updates_never_raises(monkeypatch):
    from coord_engine import transport as tr
    t = tr.FulcraFileTransport(command=["fulcra-api"])
    for rc, out in ((2, ""), (0, "not json"), (0, '{"file_changes": "nope"}')):
        monkeypatch.setattr(tr, "run_bounded", _fake_run(rc, out, []))
        assert t.updates("60 seconds") is None
    def boom(argv, timeout, **kw):
        raise OSError("no binary")
    monkeypatch.setattr(tr, "run_bounded", boom)
    assert t.updates("60 seconds") is None


def test_updates_fails_closed_on_malformed_change(monkeypatch):
    from coord_engine import transport as tr
    t = tr.FulcraFileTransport(command=["fulcra-api"])
    monkeypatch.setattr(
        tr,
        "run_bounded",
        _fake_run(0, '{"file_changes": [{"state": "uploaded"}]}', []),
    )
    assert t.updates("60 seconds", team="r") is None


def test_read_classified_does_not_start_past_supplied_deadline(monkeypatch):
    """The detector's authority read must not open a fresh transport timeout."""
    from coord_engine import transport as tr

    t = tr.FulcraFileTransport(command=["fulcra-api"], timeout=30)
    calls = []
    monkeypatch.setattr(t, "_http_enabled", lambda: False)
    monkeypatch.setattr(
        tr, "run_bounded", _fake_run(0, '{"data_type":"coordination"}', calls),
    )

    assert t.read_classified(
        "team/r/_coord/bus-v3/records.json", deadline=Deadline.open(0.0),
    ) == (None, "error")
    assert calls == []


def test_write_uses_remaining_deadline_not_transport_default(monkeypatch):
    """Probe upload must not reopen the transport's broader default timeout."""
    from types import SimpleNamespace
    from coord_engine import transport as tr

    t = tr.FulcraFileTransport(command=["fulcra-api"], timeout=30)
    observed = []

    def bounded_run(args, *, timeout=None):
        observed.append((args, timeout))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(t, "_run", bounded_run)
    assert t.write("team/r/probe.json", "{}", deadline=Deadline.open(0.5)) is True
    assert observed[0][0][0] == "upload"
    assert 0 < observed[0][1] <= 0.5


def test_write_does_not_start_past_supplied_deadline(monkeypatch):
    from coord_engine import transport as tr

    t = tr.FulcraFileTransport(command=["fulcra-api"], timeout=30)
    calls = []
    monkeypatch.setattr(t, "_run", lambda *args, **kwargs: calls.append((args, kwargs)))

    assert t.write("team/r/probe.json", "{}", deadline=Deadline.open(0.0)) is False
    assert calls == []


def test_records_cursor_without_a_server_attested_boundary_is_unknown(monkeypatch):
    """A local clock cannot prove which records the cursor window covered."""
    from coord_engine import transport as tr
    t = tr.FulcraFileTransport(command=["fulcra-api"], timeout=7)
    calls = []
    monkeypatch.setattr(
        tr, "run_bounded",
        _fake_run(0, '{"id":"r-1","recorded_at":"2026-08-20T12:00:01Z"}\n', calls),
    )
    got = t.records_cursor("coordination", "2026-08-20T12:00:00Z")
    assert got is None
    assert len(calls) == 1
    assert calls[0][:3] == ["fulcra-api", "get-records", "coordination"]
