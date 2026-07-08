"""ATC team report (fulcra-agent-atc, task 4).

``report_fold`` is a pure fold over usage shards + declared accounts + the
``demotions`` fold output, producing a JSON-serialisable dict that
``render_report`` turns into the operator-facing text block. Every figure is an
estimate from self-reported units and operator-declared caps — the label line is
required and asserted verbatim below.

Clock is injected (``now=``) so the trailing-``days`` window is deterministic.
"""
import json
from datetime import datetime, timedelta, timezone

from coord_engine import cli
from coord_engine.atc import report_fold, render_report, demotions
from coord_engine_test_helpers import FakeTransport

NOW = datetime(2026, 7, 8, 12, 0, tzinfo=timezone.utc)


def _sh(tier, age_h=1, *, account="a", units=0, throttled=False,
        model=None, task_class=None, outcome=None):
    """A usage shard (headroom-fold shape). Larger age_h => older."""
    row = {"account": account, "ts": NOW - timedelta(hours=age_h),
           "tier": tier, "units": units, "throttled": throttled}
    for k, v in (("model", model), ("task_class", task_class), ("outcome", outcome)):
        if v is not None:
            row[k] = v
    return row


def _accts(*accts):
    return {"accounts": list(accts), "tiers": {}}


FRONTIER_ACCT = {"id": "amax", "harnesses": ["claude-code"],
                 "windows": [{"hours": 5, "cap": 800}]}


# --- estimate label (required, verbatim substring) ---------------------------

def test_estimate_label_present_in_header():
    rep = report_fold(_accts(FRONTIER_ACCT), [_sh("frontier")],
                      team="fulcra", now=NOW)
    text = render_report(rep)
    assert "estimates from self-reported units" in text
    assert text.splitlines()[0].startswith("ATC report — team fulcra — last 7 days")


# --- empty ledger ------------------------------------------------------------

def test_empty_ledger_reports_no_dispatches():
    rep = report_fold(_accts(FRONTIER_ACCT), [], team="fulcra", now=NOW)
    assert rep["total"] == 0
    text = render_report(rep)
    assert "no dispatches in window" in text
    # header (with the estimate label) is still present on an empty ledger
    assert "estimates from self-reported units" in text


def test_shards_all_outside_window_read_as_empty():
    old = [_sh("frontier", age_h=24 * 30)]  # 30 days old, outside a 7-day window
    rep = report_fold(_accts(FRONTIER_ACCT), old, team="fulcra", days=7, now=NOW)
    assert rep["total"] == 0
    assert "no dispatches in window" in render_report(rep)


# --- mixed-tier math ---------------------------------------------------------

def test_mixed_tier_counts_and_percentages():
    shards = ([_sh("frontier")] * 1 + [_sh("standard")] * 3 + [_sh("cheap")] * 6)
    rep = report_fold(_accts(FRONTIER_ACCT), shards, team="fulcra", now=NOW)
    assert rep["total"] == 10
    by_tier = {t["tier"]: t for t in rep["tiers"]}
    assert by_tier["frontier"]["count"] == 1 and by_tier["frontier"]["pct"] == 10
    assert by_tier["standard"]["pct"] == 30 and by_tier["cheap"]["pct"] == 60
    # canonical ordering: frontier, standard, cheap
    assert [t["tier"] for t in rep["tiers"]] == ["frontier", "standard", "cheap"]


def test_dispatch_line_shows_frontier_count_in_parens():
    shards = ([_sh("frontier")] * 1 + [_sh("standard")] * 3 + [_sh("cheap")] * 6)
    line = next(l for l in render_report(report_fold(
        _accts(FRONTIER_ACCT), shards, team="fulcra", now=NOW)).splitlines()
        if l.startswith("dispatches:"))
    assert line == "dispatches: 10 total — frontier 10% (1) / standard 30% / cheap 60%"


def test_untiered_shard_bucketed_not_dropped():
    shards = [_sh("frontier"), {"account": "a", "ts": NOW, "units": 0}]  # no tier
    rep = report_fold(_accts(FRONTIER_ACCT), shards, team="fulcra", now=NOW)
    assert rep["total"] == 2
    assert any(t["tier"] == "untiered" for t in rep["tiers"])


# --- by-model ----------------------------------------------------------------

def test_by_model_counts_sorted_desc_then_name():
    shards = ([_sh("standard", model="claude-sonnet-5")] * 3
              + [_sh("cheap", model="qwen3-coder:30b")] * 2
              + [_sh("frontier")])  # no model on the frontier shard
    rep = report_fold(_accts(FRONTIER_ACCT), shards, team="fulcra", now=NOW)
    assert rep["by_model"] == [{"model": "claude-sonnet-5", "count": 3},
                               {"model": "qwen3-coder:30b", "count": 2}]
    assert "by model: claude-sonnet-5 3 · qwen3-coder:30b 2" in render_report(rep)


# --- throttle events ---------------------------------------------------------

def test_throttle_events_list_account_and_date():
    shards = [_sh("frontier", account="openai-codex-ash", age_h=48, throttled=True),
              _sh("cheap")]
    rep = report_fold(_accts(FRONTIER_ACCT), shards, team="fulcra", now=NOW)
    assert rep["throttle_events"] == [{"account": "openai-codex-ash", "date": "07-06"}]
    assert "throttle events: 1 (openai-codex-ash, 07-06)" in render_report(rep)


def test_no_throttle_events_reads_zero():
    rep = report_fold(_accts(FRONTIER_ACCT), [_sh("frontier")], team="fulcra", now=NOW)
    assert rep["throttle_events"] == []
    assert "throttle events: 0" in render_report(rep)


# --- windows exhausted -------------------------------------------------------

def test_windows_exhausted_counts_zeroed_windows():
    # a throttled shard zeroes the account's 5h window -> exhausted
    shards = [_sh("frontier", account="amax", throttled=True)]
    rep = report_fold(_accts(FRONTIER_ACCT), shards, team="fulcra", now=NOW)
    assert rep["windows_exhausted"] == 1
    assert "windows exhausted: 1" in render_report(rep)


def test_windows_exhausted_zero_when_headroom_remains():
    shards = [_sh("frontier", account="amax", units=100)]
    rep = report_fold(_accts(FRONTIER_ACCT), shards, team="fulcra", now=NOW)
    assert rep["windows_exhausted"] == 0


# --- calibration (from the demotions fold) -----------------------------------

def test_calibration_lines_from_demotions_fold():
    demo = {("haiku-4.5", "code"): {"bad": 3, "of": 5, "window": 5}}
    rep = report_fold(_accts(FRONTIER_ACCT), [_sh("frontier")],
                      team="fulcra", demotions=demo, now=NOW)
    assert rep["calibration"] == [
        {"model": "haiku-4.5", "task_class": "code", "bad": 3, "of": 5}]
    assert "calibration: haiku-4.5 demoted for code (3/5 escalated)" in render_report(rep)


def test_calibration_none_when_no_demotions():
    rep = report_fold(_accts(FRONTIER_ACCT), [_sh("frontier")], team="fulcra", now=NOW)
    assert rep["calibration"] == []
    assert "calibration: none" in render_report(rep)


def test_calibration_folds_from_real_demotions_output():
    # end-to-end: outcome-bearing shards -> demotions() -> report calibration
    bad = [_sh("cheap", model="haiku-4.5", task_class="code", outcome="escalated",
               age_h=h) for h in (3, 2, 1)]
    demo = demotions(bad)
    rep = report_fold(_accts(FRONTIER_ACCT), bad, team="fulcra",
                      demotions=demo, now=NOW)
    assert rep["calibration"] == [
        {"model": "haiku-4.5", "task_class": "code", "bad": 3, "of": 3}]


# --- headline formula --------------------------------------------------------

def test_headline_below_frontier_units_over_frontier_5h_cap():
    # 1680 below-frontier units ÷ 800 (frontier acct 5h cap) = 2.1
    shards = ([_sh("frontier", account="amax", units=50)]
              + [_sh("standard", account="other", units=1000)]
              + [_sh("cheap", account="other", units=680)])
    rep = report_fold(_accts(FRONTIER_ACCT), shards, team="fulcra", now=NOW)
    assert rep["headline"]["below_units"] == 1680
    assert rep["headline"]["cap"] == 800
    assert rep["headline"]["value"] == 2.1
    assert ("headline: ~2.1 frontier window-days preserved "
            "(below-frontier units ÷ frontier 5h cap)") in render_report(rep)


def test_headline_na_when_no_frontier_account_declared():
    # no frontier-tier shard => no frontier account => n/a
    shards = [_sh("standard", account="other", units=100)]
    rep = report_fold(_accts(FRONTIER_ACCT), shards, team="fulcra", now=NOW)
    assert rep["headline"]["value"] is None
    assert "headline: n/a (no frontier account declared)" in render_report(rep)


def test_headline_na_when_frontier_account_has_no_5h_window():
    acct = {"id": "amax", "harnesses": ["h"], "windows": [{"hours": 24, "cap": 5000}]}
    shards = [_sh("frontier", account="amax", units=10),
              _sh("cheap", account="amax", units=100)]
    rep = report_fold(_accts(acct), shards, team="fulcra", now=NOW)
    assert rep["headline"]["value"] is None
    assert "no frontier account declared" in render_report(rep)


# --- days filter -------------------------------------------------------------

def test_days_filter_excludes_older_dispatches():
    shards = [_sh("frontier", age_h=1), _sh("cheap", age_h=24 * 5)]  # 5 days old
    assert report_fold(_accts(FRONTIER_ACCT), shards, team="fulcra",
                       days=2, now=NOW)["total"] == 1
    assert report_fold(_accts(FRONTIER_ACCT), shards, team="fulcra",
                       days=7, now=NOW)["total"] == 2


# --- CLI wiring: `atc report` ------------------------------------------------

_ACCOUNTS = json.dumps({"accounts": [FRONTIER_ACCT], "tiers": {}})


def test_cli_atc_report_text(capsys):
    t = FakeTransport()
    t.put("team/fulcra/atc/accounts.json", _ACCOUNTS)
    cli.main(["usage", "log", "fulcra", "--account", "amax", "--tier", "frontier",
              "--units", "50"], transport=t)
    cli.main(["usage", "log", "fulcra", "--account", "amax", "--tier", "cheap",
              "--units", "80"], transport=t)
    capsys.readouterr()
    rc = cli.main(["atc", "report", "fulcra"], transport=t)
    out = capsys.readouterr().out
    assert rc == 0
    assert "ATC report — team fulcra — last 7 days" in out
    assert "estimates from self-reported units" in out
    assert "dispatches: 2 total" in out


def test_cli_atc_report_days_flag(capsys):
    t = FakeTransport()
    t.put("team/fulcra/atc/accounts.json", _ACCOUNTS)
    cli.main(["usage", "log", "fulcra", "--account", "amax", "--tier", "frontier",
              "--units", "50"], transport=t)
    capsys.readouterr()
    rc = cli.main(["atc", "report", "fulcra", "--days", "1"], transport=t)
    assert rc == 0 and "last 1 days" in capsys.readouterr().out


def test_cli_atc_report_empty_ledger(capsys):
    t = FakeTransport()
    t.put("team/fulcra/atc/accounts.json", _ACCOUNTS)
    rc = cli.main(["atc", "report", "fulcra"], transport=t)
    out = capsys.readouterr().out
    assert rc == 0 and "no dispatches in window" in out


def test_cli_atc_report_json(capsys):
    t = FakeTransport()
    t.put("team/fulcra/atc/accounts.json", _ACCOUNTS)
    cli.main(["usage", "log", "fulcra", "--account", "amax", "--tier", "frontier",
              "--units", "50"], transport=t)
    capsys.readouterr()
    rc = cli.main(["atc", "report", "fulcra", "--json"], transport=t)
    doc = json.loads(capsys.readouterr().out)
    assert rc == 0 and doc["team"] == "fulcra" and doc["total"] == 1


def test_cli_atc_report_malformed_shard_does_not_crash(capsys):
    t = FakeTransport()
    t.put("team/fulcra/atc/accounts.json", _ACCOUNTS)
    t.put("team/fulcra/atc/usage/bad.md", "{{{{not frontmatter")
    rc = cli.main(["atc", "report", "fulcra"], transport=t)
    assert rc == 0 and "no dispatches in window" in capsys.readouterr().out
