"""Writer-presence reporting — the check that the false-pass one could not do.

Background (2026-08-04): `adopt-latest.sh` shipped a post-install sanity check
of `coord-engine annotate --help`. That check passes on an install with no
`fulcra_common` at all, because the writer is imported lazily inside a
swallowing try/except and `--help` never reaches it. The engine reported
healthy, `annotate project` exited 0, and the task digest emitted nothing for a
day.

So these tests are written against the failure mode, not the happy path: each
one SIMULATES ABSENCE and asserts the surface is loud about it. A test that
only checked the present-case would reproduce the original bug exactly.

Absence is simulated by making the import fail (blocking it in `sys.modules`),
never by uninstalling anything — CI must not mutate its own environment.
"""
from __future__ import annotations

import builtins

import pytest

from coord_engine import cli, commands_annotate


@pytest.fixture
def writer_absent(monkeypatch):
    """Make `import fulcra_common` raise, exactly as a bare install does."""
    real_import = builtins.__import__

    def _blocked(name, *args, **kwargs):
        if name == "fulcra_common" or name.startswith("fulcra_common."):
            raise ImportError("simulated: no module named 'fulcra_common'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _blocked)
    monkeypatch.setattr(commands_annotate, "_WRITER_WARNED", False)
    return _blocked


@pytest.fixture
def writer_present(monkeypatch):
    """Make `import fulcra_common` succeed without requiring it installed."""
    import sys
    import types
    mod = types.ModuleType("fulcra_common")
    ann = types.ModuleType("fulcra_common.annotations")
    ann.emit_projection_annotation = lambda **kw: True
    mod.annotations = ann
    monkeypatch.setitem(sys.modules, "fulcra_common", mod)
    monkeypatch.setitem(sys.modules, "fulcra_common.annotations", ann)
    return mod


def test_writer_present_is_detected(writer_present):
    assert cli.writer_present() is True


def test_writer_absent_is_detected(writer_absent):
    assert cli.writer_present() is False


def test_doctor_line_says_MISSING_and_names_the_consequence(
        writer_absent, capsys):
    """The line must state the consequence, not just the state.

    "writer: absent" is true and useless — a reader cannot tell whether it
    matters. The 2026-08-04 outage was invisible precisely because nothing
    connected the missing package to "your digest emits nothing".
    """
    cli._report_writer_presence()
    out = capsys.readouterr().out
    assert "MISSING" in out
    assert "no-op" in out.lower(), (
        "the doctor line must spell out that the legs silently no-op; "
        f"got: {out!r}")


def test_doctor_line_does_not_claim_present_when_absent(writer_absent, capsys):
    """The exact false-pass regression: reporting healthy on a broken install."""
    cli._report_writer_presence()
    out = capsys.readouterr().out
    assert "✓ fulcra_common writer present" not in out, (
        "doctor claimed the writer is present while the import fails — this is "
        "the `annotate --help` false pass reproduced one layer up")


def test_doctor_line_is_present_when_writer_is(writer_present, capsys):
    cli._report_writer_presence()
    out = capsys.readouterr().out
    assert "✓ fulcra_common writer present" in out
    assert "MISSING" not in out


def test_absent_writer_does_NOT_make_doctor_unhealthy(writer_absent):
    """Per the directive: WARN, never a failed exit.

    Most hosts never run annotate/digest. Flipping doctor to unhealthy
    fleet-wide for an unused capability trains agents to ignore the exit code,
    which costs more than the warning buys.
    """
    assert cli._report_writer_presence() is None


def test_emit_warns_on_stderr_when_writer_missing(writer_absent, capsys):
    """The silent no-op becomes audible — while still returning False."""
    spec = type("Spec", (), {"note": "n", "tags": [], "ts": None, "id": "i"})()
    assert commands_annotate._emit_projection_spec(spec, agent="a") is False
    err = capsys.readouterr().err
    assert "MISSING" in err and "exit 0" in err, (
        f"missing-writer emit must warn loudly on stderr; got: {err!r}")


def test_emit_warning_is_once_per_process_not_per_spec(writer_absent, capsys):
    """A batch of specs must not bury its own warning under repetition."""
    spec = type("Spec", (), {"note": "n", "tags": [], "ts": None, "id": "i"})()
    for _ in range(5):
        commands_annotate._emit_projection_spec(spec, agent="a")
    assert capsys.readouterr().err.count("MISSING") == 1


def test_emit_writes_nothing_to_stdout_when_writer_missing(
        writer_absent, capsys):
    """stdout contracts stay byte-identical — machine consumers are unaffected."""
    spec = type("Spec", (), {"note": "n", "tags": [], "ts": None, "id": "i"})()
    commands_annotate._emit_projection_spec(spec, agent="a")
    assert capsys.readouterr().out == ""
