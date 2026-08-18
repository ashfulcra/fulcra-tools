"""`share create`'s argv, pinned to the REAL CLI surface.

THE DEFECT THIS FILE EXISTS FOR (coord-boss's two-account live run, 2026-08-18):
`transport.share_create` appended ``--file <prefix>``. `fulcra-api share create`
has never had that option, so leg 1 of SMOKE.md died in argparse:

    Error: No such option '--file'.

Eighty-five unit tests were green at the time. They could not have caught it:
the author's host cannot run cross-account share verbs, and every test drove a
fake that accepted whatever flag the caller passed — so the suite asserted the
flag its author WISHED for, not the one the platform has.

That is the same failure mode `test_wire_contract.py` was written to kill for
`get-records` (a fake emitting `record_id` while the transport emits `id`), and
it is the package thesis restated: every defect found here so far was a
verification surface claiming more than it measured.

So this file measures. Two captured live surfaces, neither hand-written:

  - ``fixtures/real_share_create_help.txt`` — verbatim ``fulcra-api share
    create --help`` (0.1.40). It is the authority on which options exist.
  - ``fixtures/real_share_row.json`` — a real share row carrying a file grant,
    captured from ``fulcra-api share list-incoming`` on 2026-08-18, uids
    replaced with placeholders and the SHAPE untouched. It is the authority on
    how a file prefix is expressed: a data-type id, ``file:/reports/`` — not a
    flag, not a separate field.

If the platform renames an option or moves the file grant off `fulcra_data_types`,
these fail rather than the next live run.
"""
import json
import os

import pytest

from coord_mesh import safety, transport

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")
HELP = os.path.join(FIXTURES, "real_share_create_help.txt")
SHARE_ROW = os.path.join(FIXTURES, "real_share_row.json")

PEER = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
CHANNEL = "MomentAnnotation/d04f357e-b556-4298-ad1e-4ce307d54041"


def real_help():
    with open(HELP, "r", encoding="utf-8") as fh:
        return fh.read()


def real_share_row():
    with open(SHARE_ROW, "r", encoding="utf-8") as fh:
        return json.load(fh)


def captured_argv(monkeypatch, **kw):
    """Run share_create against a recorder, returning the argv it would execute.

    Deliberately NOT a fake that accepts anything: the point of this file is
    that the argv is then checked against the captured real help text.
    """
    seen = {}

    def fake_run(args, **_):
        seen["argv"] = list(args)
        return 0, "", ""

    monkeypatch.setattr(transport, "run", fake_run)
    transport.share_create(**kw)
    return seen["argv"]


# --- the real CLI surface -------------------------------------------------

def test_the_file_flag_does_not_exist_on_the_real_cli():
    """THE regression, stated as the platform states it."""
    assert "--file" not in real_help(), (
        "captured `share create --help` grew a --file option — re-derive the "
        "file-grant mechanism from the live CLI before changing transport.py"
    )


def test_the_options_share_create_actually_has():
    help_text = real_help()
    for opt in ("--name", "--data-type", "--user-id", "--share-all"):
        assert opt in help_text, f"{opt} vanished from the real CLI"


def test_data_type_is_repeatable_which_is_why_a_file_grant_can_ride_it():
    """The fix depends on this sentence in the real help text."""
    assert "can be specified multiple times" in real_help()


# --- the real share row ---------------------------------------------------

def test_a_file_grant_is_a_data_type_id_on_a_real_row():
    """The mechanism, measured — not inferred from the CLI's shape."""
    row = real_share_row()
    types = row["fulcra_data_types"]
    assert "file:/reports/" in types, (
        "the captured row no longer expresses a file grant as a data type"
    )
    assert "file_prefix" not in row and "files" not in row, (
        "a real share row grew a dedicated file field — find_share must be "
        "re-derived rather than keep reading fulcra_data_types"
    )


def test_the_captured_row_is_scoped_not_share_all():
    assert real_share_row()["share_all_data"] is False


# --- the argv we actually execute ----------------------------------------

def test_argv_never_contains_the_flag_that_broke_the_live_run(monkeypatch):
    argv = captured_argv(monkeypatch, name="mesh-smoke", data_type=CHANNEL,
                         user_id=PEER, file_prefix="reports/")
    assert "--file" not in argv, "the r4 defect is back"


def test_argv_expresses_the_prefix_exactly_as_the_real_row_does(monkeypatch):
    """The argv value and the live row's value must be the same string."""
    argv = captured_argv(monkeypatch, name="mesh-smoke", data_type=CHANNEL,
                         user_id=PEER, file_prefix="reports/")
    assert "file:/reports/" in argv
    assert argv.count("--data-type") == 2, argv
    # …and it is the value of a --data-type, not a bare positional.
    assert argv[argv.index("file:/reports/") - 1] == "--data-type"


def test_every_flag_in_the_argv_exists_in_the_real_help(monkeypatch):
    """The whole-argv version of the regression: no wished-for flags at all."""
    argv = captured_argv(monkeypatch, name="mesh-smoke", data_type=CHANNEL,
                         user_id=PEER, file_prefix="reports/")
    help_text = real_help()
    flags = [a for a in argv if str(a).startswith("--")]
    assert flags, argv
    for flag in flags:
        assert flag in help_text, (
            f"{flag} is not an option of the real `share create` — this is the "
            "exact class of defect that killed leg 1 of the live smoke"
        )


def test_channel_still_granted_and_uid_still_last(monkeypatch):
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
    """Someone who read the id off a live row and pasted it gets it back."""
    assert transport.file_data_type("file:/reports/") == "file:/reports/"


def test_a_nested_prefix_keeps_its_interior_slashes():
    assert transport.file_data_type("reports/2026/") == "file:/reports/2026/"


def test_an_empty_prefix_is_an_error_not_a_silent_grant():
    with pytest.raises(ValueError):
        transport.file_data_type("   ")
