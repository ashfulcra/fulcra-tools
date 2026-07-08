"""CLI wiring — every verb dispatches with an injected transport."""

import json

from fde_engine import cli
from fde_engine_test_helpers import FakeTransport


def run(args, transport):
    """Invoke main with a fake transport; capture stdout via capsys in tests."""
    return cli.main(args, transport=transport)


def test_init_status_phase_resume_list_roundtrip(capsys, tmp_path, monkeypatch):
    t = FakeTransport()
    monkeypatch.chdir(tmp_path)

    assert run(["init", "sourdough-coach", "--title", "Sourdough Coach"], t) == 0
    out = capsys.readouterr().out
    assert "sourdough-coach" in out and "intake" in out

    assert run(["status", "sourdough-coach", "--json"], t) == 0
    st = json.loads(capsys.readouterr().out)
    assert st["phase"] == "intake"

    assert run(["phase", "sourdough-coach", "interview"], t) == 0
    capsys.readouterr()

    assert run(["resume", "sourdough-coach"], t) == 0
    assert "phase: interview" in capsys.readouterr().out

    assert run(["list", "--json"], t) == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows == [{"slug": "sourdough-coach", "title": "Sourdough Coach",
                     "phase": "interview"}]


def test_invalid_phase_transition_is_a_clean_error(capsys):
    t = FakeTransport()
    run(["init", "x", "--title", "X"], t)
    capsys.readouterr()
    assert run(["phase", "x", "build"], t) == 1
    err = capsys.readouterr().err
    assert "invalid transition" in err


def test_unreachable_store_is_a_clean_error(capsys):
    class DeadTransport(FakeTransport):
        def list_dir(self, prefix):
            from fde_engine.transport import TransportError
            raise TransportError("list failed: store unreachable")
    assert run(["list"], DeadTransport()) == 1
    err = capsys.readouterr().err
    assert err.startswith("fde-engine: ") and "unreachable" in err


def test_sync_push_and_pull(capsys, tmp_path):
    t = FakeTransport()
    run(["init", "x", "--title", "X"], t)
    capsys.readouterr()
    d = tmp_path / "mirror"
    d.mkdir()
    (d / "retro.md").write_text("lessons", encoding="utf-8")
    assert run(["sync", "x", "push", "--dir", str(d)], t) == 0
    assert t.read("fde/engagements/x/retro.md") == "lessons"
    capsys.readouterr()
    assert run(["sync", "x", "pull", "--dir", str(d)], t) == 0
    assert "engagement.md" in capsys.readouterr().out
