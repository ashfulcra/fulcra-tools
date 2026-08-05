"""Engine-vs-fleet-pin currency — the check `doctor: healthy` could not make.

Background (2026-08-05): a fresh container came up on coord-engine v1.6.12, an
engine predating bus-v3 entirely, and `doctor` printed three ticks and
`healthy`. Nothing was wrong with the engine's pulse; it was simply not the
engine the fleet had agreed to run, and doctor had no opinion about that.

So, exactly as with the writer-presence suite, these tests are written against
the failure mode. The load-bearing one is
`test_stale_build_does_not_print_a_tick`: a check that only exercised the
matching case would reproduce the original bug one layer up.

Staleness and unreadability are SIMULATED — a fake transport and a patched
build sha. Nothing here uninstalls, downgrades, or reinstalls anything: CI must
not mutate its own environment to test a claim about environments.
"""
from __future__ import annotations

import pytest

from coord_engine import cli, pin_currency as pc


PIN = "ffca635fea23851311903265f6d244719fa13ed6"
OTHER = "9cc9763e0000000000000000000000000000abcd"

SCRIPT = (
    "#!/usr/bin/env bash\n"
    "# adopt-latest.sh — fleet convergence\n"
    "set -u\n"
    f'PIN="{PIN}"   # == main after PR 480\n'
    'VER="pp-ffca635f"\n'
)


class FakeTransport:
    """Classified-read transport whose one document is the adopt script."""

    def __init__(self, raw=SCRIPT, status="ok"):
        self.raw, self.status = raw, status
        self.reads: list[str] = []

    def read_classified(self, path):
        self.reads.append(path)
        if self.status != "ok":
            return None, self.status
        return self.raw, "ok"

    def read(self, path):
        raw, status = self.read_classified(path)
        return raw if status == "ok" else None


# --- parsing the authority -------------------------------------------------

def test_pin_is_parsed_from_the_adopt_script():
    assert pc.parse_pin(SCRIPT) == PIN


def test_pin_parse_ignores_a_sha_that_is_only_mentioned_in_a_comment():
    text = f"# we used to pin {OTHER}\nPIN='{PIN}'\n"
    assert pc.parse_pin(text) == PIN


def test_pin_parse_takes_the_first_assignment_when_the_script_is_ambiguous():
    text = f'PIN="{PIN}"\nPIN="{OTHER}"\n'
    assert pc.parse_pin(text) == PIN


def test_pin_parse_returns_none_when_there_is_no_pin_line():
    assert pc.parse_pin("set -u\nVER='pp-ffca635f'\n") is None


def test_pin_parse_survives_non_text_input():
    assert pc.parse_pin(None) is None
    assert pc.parse_pin(b"PIN=deadbeef1234") is None


# --- comparison ------------------------------------------------------------

def test_identical_shas_match():
    assert pc.shas_match(PIN, PIN) is True


def test_short_pin_matches_by_prefix():
    assert pc.shas_match(PIN, PIN[:8]) is True


def test_case_differences_do_not_break_the_match():
    assert pc.shas_match(PIN.upper(), PIN) is True


def test_different_shas_do_not_match():
    assert pc.shas_match(PIN, OTHER) is False


def test_a_prefix_too_short_to_be_evidence_is_not_a_match():
    # 6 hex would collide by coincidence often enough to be worthless.
    assert pc.shas_match(PIN, PIN[:6]) is False


def test_missing_either_side_is_not_a_match():
    assert pc.shas_match(None, PIN) is False
    assert pc.shas_match(PIN, None) is False


# --- classification --------------------------------------------------------

def test_matching_build_and_pin_classify_current():
    assert pc.classify(PIN, PIN, "ok") == "current"


def test_differing_build_and_pin_classify_mismatch():
    assert pc.classify(OTHER, PIN, "ok") == "mismatch"


def test_unknown_build_classifies_unknown_build():
    assert pc.classify(None, PIN, "ok") == "unknown-build"


@pytest.mark.parametrize("status", ["error", "absent", "invalid"])
def test_any_unusable_pin_classifies_unknown_pin(status):
    assert pc.classify(PIN, None, status) == "unknown-pin"


def test_unreadable_pin_wins_over_unknown_build():
    # Knowing our own sha exactly is worth nothing if we cannot say what the
    # fleet agreed to run — report the pin problem, not the build one.
    assert pc.classify(None, None, "error") == "unknown-pin"


# --- the doctor line -------------------------------------------------------

def test_only_a_match_prints_a_tick():
    line = pc.report_line("current", build=PIN, pin=PIN, pin_status="ok",
                          team="fulcra")
    assert line.strip().startswith("✓")


@pytest.mark.parametrize("verdict,pin_status", [
    ("mismatch", "ok"),
    ("unknown-build", "ok"),
    ("unknown-pin", "error"),
    ("unknown-pin", "absent"),
    ("unknown-pin", "invalid"),
])
def test_stale_build_does_not_print_a_tick(verdict, pin_status):
    """THE regression this whole leg exists for: unknown must never read ✓."""
    line = pc.report_line(verdict, build=OTHER, pin=PIN,
                          pin_status=pin_status, team="fulcra")
    assert "✓" not in line
    assert line.strip().startswith("!")


def test_mismatch_line_names_the_consequence_and_the_remedy():
    line = pc.report_line("mismatch", build=OTHER, pin=PIN, pin_status="ok",
                          team="fulcra")
    assert "adopt-latest.sh" in line
    assert "bus-v3" in line


def test_mismatch_line_does_not_claim_certainty_about_direction():
    # Offline there is no way to tell "behind the pin" from "ahead of it"; the
    # line may not assert more than it can prove.
    line = pc.report_line("mismatch", build=OTHER, pin=PIN, pin_status="ok",
                          team="fulcra")
    assert "may be" in line


def test_teamless_run_says_it_did_not_check_rather_than_ticking():
    line = pc.report_line("unknown-pin", build=PIN, pin=None,
                          pin_status="no-team", team=None)
    assert "✓" not in line
    assert "no team" in line


def test_unreadable_authority_line_distinguishes_itself_from_absence():
    err = pc.report_line("unknown-pin", build=PIN, pin=None,
                         pin_status="error", team="fulcra")
    absent = pc.report_line("unknown-pin", build=PIN, pin=None,
                            pin_status="absent", team="fulcra")
    assert err != absent
    assert "unreadable" in err
    assert "absent" in absent


# --- loading through a transport -------------------------------------------

def test_load_pin_reads_the_team_scoped_adopt_script():
    t = FakeTransport()
    pin, status = pc.load_pin(t, "fulcra")
    assert (pin, status) == (PIN, "ok")
    assert t.reads[0] == "team/fulcra/_coord/bus-v3/adopt-latest.sh"


def test_load_pin_reports_error_not_absent_on_a_degraded_store():
    pin, status = pc.load_pin(FakeTransport(status="error"), "fulcra")
    assert (pin, status) == (None, "error")


def test_load_pin_reports_absent_on_an_affirmative_not_found():
    pin, status = pc.load_pin(FakeTransport(status="absent"), "fulcra")
    assert (pin, status) == (None, "absent")


def test_load_pin_reports_invalid_when_the_script_has_no_pin_line():
    pin, status = pc.load_pin(FakeTransport(raw="set -u\n"), "fulcra")
    assert (pin, status) == (None, "invalid")


def test_load_pin_survives_a_transport_that_raises():
    class Boom:
        def read_classified(self, path):
            raise RuntimeError("store on fire")

    assert pc.report(Boom(), "fulcra").strip().startswith("!")


# --- end to end through report() -------------------------------------------

def test_report_ticks_when_the_running_build_is_the_pinned_one(monkeypatch):
    monkeypatch.setattr(pc, "build_sha", lambda *a, **k: PIN)
    assert pc.report(FakeTransport(), "fulcra").strip().startswith("✓")


def test_report_warns_when_the_running_build_is_stale(monkeypatch):
    monkeypatch.setattr(pc, "build_sha", lambda *a, **k: OTHER)
    line = pc.report(FakeTransport(), "fulcra")
    assert line.strip().startswith("!")
    assert "adopt-latest.sh" in line


def test_report_warns_when_the_build_sha_is_unknowable(monkeypatch):
    monkeypatch.setattr(pc, "build_sha", lambda *a, **k: None)
    line = pc.report(FakeTransport(), "fulcra")
    assert line.strip().startswith("!")
    assert "UNKNOWN" in line


def test_build_sha_returns_none_rather_than_raising_off_a_missing_dist():
    assert pc.build_sha("coord-engine-does-not-exist") is None


# --- the doctor surface ----------------------------------------------------

def test_doctor_prints_the_currency_line(monkeypatch, capsys):
    monkeypatch.setattr(pc, "build_sha", lambda *a, **k: OTHER)
    cli._report_pin_currency(FakeTransport(), "fulcra")
    out = capsys.readouterr().out
    assert "does NOT match the fleet pin" in out


def test_doctor_currency_line_never_makes_doctor_unhealthy(monkeypatch, capsys):
    """WARN posture: the reporter returns nothing and raises nothing, so it
    cannot participate in the exit code even by accident."""
    monkeypatch.setattr(pc, "build_sha", lambda *a, **k: OTHER)
    assert cli._report_pin_currency(FakeTransport(status="error"),
                                    "fulcra") is None


def test_doctor_currency_check_cannot_crash_doctor(monkeypatch, capsys):
    def _boom(*a, **k):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(pc, "report", _boom)
    cli._report_pin_currency(FakeTransport(), "fulcra")
    assert "currency unknown" in capsys.readouterr().out
