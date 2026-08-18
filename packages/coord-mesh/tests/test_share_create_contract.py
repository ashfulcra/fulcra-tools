"""`share create`'s argv, pinned to the REAL CLI surface — and to a MEASURED one.

This file has been wrong once, and how it was wrong is the point.

ROUND 1 (the live defect). `transport.share_create` appended ``--file <prefix>``
and coord-boss's two-account smoke died in argparse: `No such option '--file'`.
Eighty-five unit tests were green and could not have caught it — every test drove
a fake that accepted whatever flag the caller passed, so the suite asserted the
flag its author WISHED for.

ROUND 2 (this file's own defect, codex-coder r5). The fixture written to fix
that was labelled "fulcra-api 0.1.40" and was captured from 0.1.38. A test
asserting "`--file` does not exist on the real CLI" therefore passed here and was
refuted on a reviewer's genuine 0.1.40, where `--file` exists. Three hosts each
believed they ran 0.1.40; two were wrong. Nobody lied — the version was never
measured, only assumed, and an assumption written into a docstring is
indistinguishable from a measurement afterwards.

So the fixture is now JSON carrying its own MEASURED provenance
(`tests/fixtures/real_share_create_help.json`, written by
`tools/capture_fixtures.py`, never by hand), and these tests assert against the
version it records rather than a version anyone typed. Re-capture with:

    python tools/capture_fixtures.py            # rewrite from installed client
    python tools/capture_fixtures.py --check    # fail if it has drifted

WHAT IS AND IS NOT PINNED HERE. `--file` exists on 0.1.40 and this package does
not use it: coord-boss's live bench proved the `--data-type file:/reports/` path
verbatim-green with read-back verification, and the sugar flag has not been
proven. That is a deliberate choice, recorded so the next maintainer knows it
was made rather than missed.
"""
import json
import os

import pytest

from coord_mesh import safety, transport

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")
HELP = os.path.join(FIXTURES, "real_share_create_help.json")
SHARE_ROW = os.path.join(FIXTURES, "real_share_row.json")

PEER = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
CHANNEL = "MomentAnnotation/d04f357e-b556-4298-ad1e-4ce307d54041"


def capture():
    with open(HELP, "r", encoding="utf-8") as fh:
        return json.load(fh)


def real_help():
    return capture()["help"]


def real_share_row():
    with open(SHARE_ROW, "r", encoding="utf-8") as fh:
        return json.load(fh)


def captured_argv(monkeypatch, **kw):
    """The argv share_create would execute, recorded rather than faked-away."""
    seen = {}

    def fake_run(args, **_):
        seen["argv"] = list(args)
        return 0, "", ""

    monkeypatch.setattr(transport, "run", fake_run)
    transport.share_create(**kw)
    return seen["argv"]


# --- provenance: the r5 regression ---------------------------------------

def test_the_capture_records_which_client_it_came_from():
    """THE r5 regression. An unlabelled capture is how this file got it wrong."""
    prov = capture().get("distribution_version")
    assert prov, ("the help fixture carries no measured version — re-run "
                  "tools/capture_fixtures.py; a capture without provenance is "
                  "exactly what r5 refuted")


def test_the_capture_is_from_a_client_that_can_do_file_grants():
    """Pinning argv to a client too old to express the feature is meaningless."""
    prov = capture()["distribution_version"]
    assert prov >= transport.MIN_FILE_GRANT_VERSION, (
        f"fixture captured from fulcra-api {prov}, which cannot express a file "
        f"grant at all; re-capture from >= {transport.MIN_FILE_GRANT_VERSION}"
    )


def test_the_capture_names_the_surface_and_the_binary():
    cap = capture()
    assert cap["surface"] == "fulcra-api share create --help"
    assert cap["captured_from"]


# --- the real CLI surface -------------------------------------------------

def test_the_options_share_create_actually_has():
    help_text = real_help()
    for opt in ("--name", "--data-type", "--user-id", "--share-all", "--file"):
        assert opt in help_text, f"{opt} vanished from the captured CLI"


def test_data_type_is_repeatable_which_is_why_a_file_grant_can_ride_it():
    """The fix depends on this sentence in the real help text."""
    assert "can be specified multiple times" in real_help()


def test_the_capability_probe_agrees_with_the_captured_surface():
    """`supports_file_grants` is what the runtime fence trusts; it must read the
    same surface these tests read, not a second opinion about it."""
    assert transport.supports_file_grants(real_help()) is True


def test_the_probe_says_no_for_a_client_without_the_marker():
    """A 0.1.39-shaped help text must not read as capable."""
    old = real_help().replace("--file", "--no-such-flag")
    assert transport.supports_file_grants(old) is False


# --- the real share row ---------------------------------------------------

def test_a_file_grant_is_a_data_type_id_on_a_real_row():
    """The mechanism, measured — not inferred from the CLI's shape."""
    types = real_share_row()["fulcra_data_types"]
    assert "file:/reports/" in types, (
        "the captured row no longer expresses a file grant as a data type"
    )


def test_the_captured_row_has_no_dedicated_file_field():
    row = real_share_row()
    assert "file_prefix" not in row and "files" not in row, (
        "a real share row grew a dedicated file field — find_share must be "
        "re-derived rather than keep reading fulcra_data_types"
    )


def test_the_captured_row_is_scoped_not_share_all():
    assert real_share_row()["share_all_data"] is False


# --- the argv we actually execute ----------------------------------------

def test_argv_expresses_the_prefix_exactly_as_the_real_row_does(monkeypatch):
    """The argv value and the live row's value must be the same string."""
    argv = captured_argv(monkeypatch, name="mesh-smoke", data_type=CHANNEL,
                         user_id=PEER, file_prefix="reports/")
    assert "file:/reports/" in argv
    assert argv.count("--data-type") == 2, argv
    assert argv[argv.index("file:/reports/") - 1] == "--data-type"


def test_we_use_the_proven_path_not_the_sugar_flag(monkeypatch):
    """`--file` exists on 0.1.40 but has never been proven end-to-end here;
    the --data-type path has. Changing this is a decision, not a cleanup."""
    argv = captured_argv(monkeypatch, name="mesh-smoke", data_type=CHANNEL,
                         user_id=PEER, file_prefix="reports/")
    assert "--file" not in argv


def test_every_flag_in_the_argv_exists_in_the_captured_help(monkeypatch):
    """The whole-argv check: no wished-for flags, measured against a client
    whose version is recorded rather than assumed."""
    argv = captured_argv(monkeypatch, name="mesh-smoke", data_type=CHANNEL,
                         user_id=PEER, file_prefix="reports/")
    help_text = real_help()
    flags = [a for a in argv if str(a).startswith("--")]
    assert flags, argv
    for flag in flags:
        assert flag in help_text, (
            f"{flag} is not an option of the captured `share create` — this is "
            "the class of defect that killed leg 1 of the live smoke"
        )


def test_channel_still_granted_and_uid_present(monkeypatch):
    argv = captured_argv(monkeypatch, name="mesh-smoke", data_type=CHANNEL,
                         user_id=PEER, file_prefix="reports/")
    assert argv[:2] == ["share", "create"]
    assert CHANNEL in argv
    assert argv[argv.index("--user-id") + 1] == PEER


def test_no_file_data_type_when_no_prefix_requested(monkeypatch):
    argv = captured_argv(monkeypatch, name="mesh-smoke", data_type=CHANNEL,
                         user_id=PEER, file_prefix=None)
    assert argv.count("--data-type") == 1
    assert not [a for a in argv if str(a).startswith("file:")]


def test_rails_still_run_on_the_new_argv(monkeypatch):
    """The prefix change must not have moved the argv past the rails."""
    with pytest.raises(safety.SafetyViolation):
        captured_argv(monkeypatch, name="mesh-smoke", data_type=CHANNEL,
                      user_id="not-a-uid", file_prefix="reports/")


# --- prefix normalization -------------------------------------------------

@pytest.mark.parametrize("given", ["reports/", "/reports", "reports", "/reports/"])
def test_all_the_ways_an_operator_writes_the_path_normalize_to_the_live_value(given):
    assert transport.file_data_type(given) == "file:/reports/"


def test_an_already_namespaced_value_passes_through():
    assert transport.file_data_type("file:/reports/") == "file:/reports/"


def test_a_nested_prefix_keeps_its_interior_slashes():
    assert transport.file_data_type("reports/2026/") == "file:/reports/2026/"


def test_an_empty_prefix_is_an_error_not_a_silent_grant():
    with pytest.raises(ValueError):
        transport.file_data_type("   ")


# --- naming WHICH client answered (two-binary hosts) ----------------------

def test_which_client_names_a_path_not_just_a_word(monkeypatch):
    """A host can carry two `fulcra-api` binaries with DIFFERENT surfaces —
    measured on two hosts 2026-08-18: the uv tool has `share create`, the
    workspace venv one has no `share` command at all, and under `uv run` the
    venv one wins. "the installed fulcra-api" is not an actionable phrase on
    such a host; the path is."""
    monkeypatch.setattr(transport.shutil, "which", lambda n: "/somewhere/" + n)
    named = transport.which_client()
    assert "/somewhere/fulcra-api" in named


def test_which_client_says_so_when_the_binary_is_not_on_path(monkeypatch):
    monkeypatch.setattr(transport.shutil, "which", lambda n: None)
    assert "not on PATH" in transport.which_client()


def test_capability_unknown_names_the_binary_it_probed(monkeypatch):
    """The r6 message said "the installed fulcra-api" and left the reader to
    guess which one had failed."""
    def boom(args, **_):
        return 2, "", "Error: No such command 'share'."
    monkeypatch.setattr(transport, "run", boom)
    with pytest.raises(transport.CapabilityUnknown) as exc:
        transport.share_create_help()
    assert "fulcra-api" in str(exc.value)
    assert "No such command" in str(exc.value)
