"""The console entry point must actually RUN.

codex-coder, reviewing head 0667bfb: pyproject registered
``coord-mesh = "coord_mesh.cli:main"`` while ``coord_mesh/cli.py`` did not
exist, so the installed command crashed — and 61 unit tests stayed green,
because every one of them imported a module and none invoked the entry point.

These tests close that gap. They exercise `main()` the way the console script
does, and one of them resolves the entry point THROUGH the package metadata, so
a pyproject that names a missing target fails here instead of at a user's shell.
"""
import subprocess
import sys

import pytest

from coord_mesh import cli

UID = "a24a9667-c2c6-4bbf-9a0f-36ea0afcb521"
MINE = "d64bbe9b-4902-42e9-a607-7db51ebc6379"
CH = "MomentAnnotation/d04f357e-b556-4298-ad1e-4ce307d54041"


def test_declared_entry_point_resolves_and_is_callable():
    """THE REGRESSION: pyproject's console target must import and be callable.

    Resolved through importlib.metadata — the same lookup the installed script
    uses — so a renamed or deleted target is caught here.
    """
    from importlib.metadata import entry_points
    eps = [e for e in entry_points(group="console_scripts") if e.name == "coord-mesh"]
    assert eps, "console_scripts entry point 'coord-mesh' is not installed"
    fn = eps[0].load()
    assert callable(fn)


def test_module_is_runnable_as_a_script():
    """`python -m coord_mesh.cli` must not traceback — the crash codex saw."""
    cp = subprocess.run([sys.executable, "-m", "coord_mesh.cli", "--help"],
                        capture_output=True, text=True, timeout=60)
    assert cp.returncode == 0, cp.stderr
    assert "coord-mesh" in cp.stdout
    assert "Traceback" not in cp.stderr


def test_bare_invocation_prints_help_and_refuses():
    assert cli.main([]) == cli.RC_REFUSED


@pytest.mark.parametrize("verb", ["init", "peers", "send", "queue", "doctor"])
def test_every_planned_verb_is_registered(verb):
    """All five plan verbs exist as subcommands — a verb named in the plan and
    absent from the parser is the same defect class as the missing cli.py."""
    help_text = cli.build_parser().format_help()
    assert verb in help_text


def test_send_builds_an_envelope_without_touching_the_network(capsys):
    rc = cli.main(["send", "--to-user", UID, "--slug", "mesh-m2", "--kind", "response"])
    assert rc == cli.RC_OK
    out = capsys.readouterr().out
    assert '"to_user":"' + UID + '"' in out.replace(" ", "")


def test_send_refuses_a_named_peer_rather_than_a_uid(capsys):
    """The rail reaches the CLI surface, not just the library."""
    assert cli.main(["send", "--to-user", "michael", "--slug", "s"]) == cli.RC_REFUSED
    assert "REFUSED" in capsys.readouterr().err


def test_verbs_needing_a_channel_refuse_without_one(capsys):
    assert cli.main(["doctor"]) == cli.RC_REFUSED
    assert "--channel is required" in capsys.readouterr().err


def test_queue_without_peers_refuses_rather_than_reporting_empty(capsys):
    """Reporting '0 events' when nobody was polled is the exact lie this
    package exists to avoid."""
    rc = cli.main(["--channel", CH, "queue", "--me", MINE])
    assert rc == cli.RC_REFUSED
    assert "no --peer given" in capsys.readouterr().err


def test_unreadable_peer_makes_queue_exit_unknown_not_zero(capsys, monkeypatch):
    """THE trusted-empty discipline, at the exit code: a peer we could not read
    must not produce a green 'no messages'."""
    monkeypatch.setattr(cli.transport, "get_records",
                        lambda *a, **k: cli.transport.Result(
                            cli.transport.ERROR, detail="classifier denied"))
    monkeypatch.setattr(cli.peers, "save", lambda *a, **k: None)
    rc = cli.main(["--channel", CH, "queue", "--me", MINE, "--peer", UID,
                   "--no-advance"])
    assert rc == cli.RC_UNKNOWN
    err = capsys.readouterr().err
    assert "UNKNOWN" in err and "not empty" in err


def test_doctor_reports_unknown_when_a_check_is_unreadable(capsys, monkeypatch):
    monkeypatch.setattr(cli.transport, "list_incoming",
                        lambda *a, **k: cli.transport.Result(cli.transport.ERROR,
                                                             detail="boom"))
    monkeypatch.setattr(cli.transport, "get_records",
                        lambda *a, **k: cli.transport.Result(cli.transport.EMPTY))
    assert cli.main(["--channel", CH, "doctor"]) == cli.RC_UNKNOWN
    assert "UNREADABLE" in capsys.readouterr().err
