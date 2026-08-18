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


def test_send_dry_run_prints_but_never_reports_success(capsys):
    """A printed envelope is NOT a sent one.

    r2 on 3c1c78d: `mesh send` returned rc0 without writing, which reads as
    delivered. Dry run is now explicit, opt-in, and exits UNKNOWN.
    """
    rc = cli.main(["--channel", CH, "send", "--to-user", UID, "--slug", "m",
                   "--dry-run"])
    assert rc == cli.RC_UNKNOWN
    cap = capsys.readouterr()
    assert '"to_user":"' + UID + '"' in cap.out.replace(" ", "")
    assert "DRY RUN" in cap.err


def test_send_refuses_a_named_peer_rather_than_a_uid(capsys):
    """The rail reaches the CLI surface, not just the library."""
    rc = cli.main(["--channel", CH, "send", "--to-user", "michael", "--slug", "s"])
    assert rc == cli.RC_REFUSED
    assert "REFUSED" in capsys.readouterr().err


def test_send_that_writes_but_cannot_read_back_is_not_success(capsys, monkeypatch):
    """THE r2 REGRESSION: a write returning 0 is a claim, not evidence."""
    monkeypatch.setattr(cli.transport, "record", lambda *a, **k: (0, "", ""))
    monkeypatch.setattr(cli.transport, "get_records",
                        lambda *a, **k: cli.transport.Result(cli.transport.EMPTY))
    rc = cli.main(["--channel", CH, "send", "--to-user", UID, "--slug", "m"])
    assert rc == cli.RC_UNKNOWN
    assert "NOT sent" in capsys.readouterr().err


def _event_row(rid, slug="m"):
    import json as _json
    from coord_mesh import envelope as _env
    return {"id": rid, "recorded_at": "t", "sources": ["coord-mesh"],
            "note": _json.dumps(_env.build(to_user=UID, kind="response", slug=slug))}


def test_send_succeeds_only_when_a_NEW_event_is_read_back(capsys, monkeypatch):
    calls = {"n": 0}

    def reads(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:          # pre-write snapshot: empty
            return cli.transport.Result(cli.transport.EMPTY)
        return cli.transport.Result(cli.transport.OK, rows=[_event_row("rec-new")])

    monkeypatch.setattr(cli.transport, "record", lambda *a, **k: (0, "", ""))
    monkeypatch.setattr(cli.transport, "get_records", reads)
    rc = cli.main(["--channel", CH, "send", "--to-user", UID, "--slug", "m"])
    assert rc == cli.RC_OK
    assert "new record rec-new" in capsys.readouterr().out


def test_send_does_not_verify_against_a_STALE_same_slug_event(capsys, monkeypatch):
    """THE r3 REGRESSION: re-sending a slug that already exists would otherwise
    'verify' instantly against the old record, even if the write did nothing."""
    stale = _event_row("rec-old")
    monkeypatch.setattr(cli.transport, "record", lambda *a, **k: (0, "", ""))
    monkeypatch.setattr(cli.transport, "get_records",
                        lambda *a, **k: cli.transport.Result(cli.transport.OK,
                                                             rows=[stale]))
    rc = cli.main(["--channel", CH, "send", "--to-user", UID, "--slug", "m"])
    assert rc == cli.RC_UNKNOWN
    assert "NO NEW record" in capsys.readouterr().err


def test_send_refuses_to_write_when_the_pre_snapshot_is_unknown(capsys, monkeypatch):
    """Without a baseline a read-back cannot tell new from old, so writing
    would produce an unverifiable claim."""
    monkeypatch.setattr(cli.transport, "get_records",
                        lambda *a, **k: cli.transport.Result(cli.transport.ERROR,
                                                             detail="denied"))
    wrote = {"n": 0}
    monkeypatch.setattr(cli.transport, "record",
                        lambda *a, **k: (wrote.__setitem__("n", 1), (0, "", ""))[1])
    rc = cli.main(["--channel", CH, "send", "--to-user", UID, "--slug", "m"])
    assert rc == cli.RC_UNKNOWN
    assert wrote["n"] == 0, "must not write without a baseline"
    assert "refusing to write" in capsys.readouterr().err


def test_send_write_failure_is_unknown_not_success(capsys, monkeypatch):
    monkeypatch.setattr(cli.transport, "record", lambda *a, **k: (1, "", "denied"))
    rc = cli.main(["--channel", CH, "send", "--to-user", UID, "--slug", "m"])
    assert rc == cli.RC_UNKNOWN
    assert "failed" in capsys.readouterr().err


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


# --- mesh init read-back must identify OUR share (r2 on 3c1c78d) -------------

def _share(name, uid, types, share_all=False):
    return {"datashare_name": name, "share_all_data": share_all,
            "fulcra_data_types": list(types),
            "permissions": [{"allowed_fulcra_userid": uid}]}


def test_init_readback_rejects_a_preexisting_share_to_the_same_uid(capsys, monkeypatch):
    """THE r2 REGRESSION, and it is not hypothetical: the first mesh peer
    already holds a 2024 share-all from the operator, so a uid-only read-back
    passes before the mesh has created anything."""
    monkeypatch.setattr(cli.transport, "share_create", lambda **k: (0, "", ""))
    monkeypatch.setattr(cli.transport, "list_outgoing",
                        lambda *a, **k: cli.transport.Result(
                            cli.transport.OK, rows=[_share("MJJT share", UID, [], True)]))
    rc = cli.main(["--channel", CH, "init", UID, "--name", "mesh-m2-test"])
    assert rc == cli.RC_UNKNOWN
    assert "not evidence that ours exists" in capsys.readouterr().err


def test_init_readback_accepts_the_share_we_actually_minted(capsys, monkeypatch):
    monkeypatch.setattr(cli.transport, "share_create", lambda **k: (0, "", ""))
    monkeypatch.setattr(cli.transport, "list_outgoing",
                        lambda *a, **k: cli.transport.Result(
                            cli.transport.OK,
                            rows=[_share("MJJT share", UID, [], True),
                                  _share("mesh-m2-test", UID, [CH])]))
    rc = cli.main(["--channel", CH, "init", UID, "--name", "mesh-m2-test"])
    assert rc == cli.RC_OK
    assert "read-back verified" in capsys.readouterr().out


def test_init_readback_rejects_right_name_wrong_data_type(capsys, monkeypatch):
    """Name alone is not identity either — the grant must actually carry our channel."""
    monkeypatch.setattr(cli.transport, "share_create", lambda **k: (0, "", ""))
    monkeypatch.setattr(cli.transport, "list_outgoing",
                        lambda *a, **k: cli.transport.Result(
                            cli.transport.OK,
                            rows=[_share("mesh-m2-test", UID, ["StepCount"])]))
    assert cli.main(["--channel", CH, "init", UID, "--name", "mesh-m2-test"]) == cli.RC_UNKNOWN


def test_init_refuses_share_all_even_when_it_lists_our_data_type(capsys, monkeypatch):
    """r3: a share-all grants everything, so it satisfies any data-type test —
    which makes it useless as evidence that OUR scoped share exists."""
    monkeypatch.setattr(cli.transport, "share_create", lambda **k: (0, "", ""))
    monkeypatch.setattr(cli.transport, "list_outgoing",
                        lambda *a, **k: cli.transport.Result(
                            cli.transport.OK,
                            rows=[_share("mesh-m2-test", UID, [CH], share_all=True)]))
    assert cli.main(["--channel", CH, "init", UID, "--name", "mesh-m2-test"]) == cli.RC_UNKNOWN


def test_init_does_not_claim_the_reports_prefix_was_verified(capsys, monkeypatch):
    """r3: `share list-outgoing` has no file-prefix field, so the reports path
    is unobservable — the success line must not imply otherwise."""
    monkeypatch.setattr(cli.transport, "share_create", lambda **k: (0, "", ""))
    monkeypatch.setattr(cli.transport, "list_outgoing",
                        lambda *a, **k: cli.transport.Result(
                            cli.transport.OK, rows=[_share("mesh-m2-test", UID, [CH])]))
    rc = cli.main(["--channel", CH, "init", UID, "--name", "mesh-m2-test",
                   "--reports", "reports/"])
    assert rc == cli.RC_OK
    cap = capsys.readouterr()
    assert "reports/" not in cap.out, "success line must not claim the reports path"
    assert "not observable" in cap.err and "unverified" in cap.err
