"""ATC v2 end-to-end: CLI write path -> fold read path on ONE FakeTransport.

This exercises the whole ATC v2 loop through the real CLI on a single in-memory
transport: `atc init` seeds the ledger, `route` ranks, several `usage log` calls
write tiered+outcome-attributed shards, and `atc report` + `dash_data` fold them
back. It is the regression guard for C1 (tier dropped on shard read-back): before
the fix, `_atc_usage_shards` never re-read `tier`, so the report/dash always
showed "untiered 100%" and an n/a headline regardless of what was logged.
"""
from coord_engine import atc, atc_dash, cli
from coord_engine_test_helpers import FakeTransport

TEAM = "fulcra"


def _log(t, *, tier, units, outcome, agent, model="claude-x", task_class="code"):
    rc = cli.main(["usage", "log", TEAM, "--account", "anthropic-main",
                   "--tier", tier, "--units", str(units), "--model", model,
                   "--task-class", task_class, "--outcome", outcome,
                   "--agent", agent], transport=t)
    assert rc == 0


def _run_e2e(capsys):
    t = FakeTransport()

    # 1. init the ledger (anthropic:max seeds a 5h/1000 + 168h/15000 window set)
    rc = cli.main(["atc", "init", TEAM, "--yes",
                   "--account", "anthropic-main=anthropic:max"], transport=t)
    assert rc == 0
    capsys.readouterr()  # drain init's paste-lines

    # 2. route runs against the freshly-seeded ledger
    rc = cli.main(["route", TEAM, "--needs", "code"], transport=t)
    assert rc == 0
    capsys.readouterr()

    # 3. mixed tiers AND outcomes: 2 frontier + 1 standard + 1 cheap => 50%
    #    frontier; below-frontier units 500+500=1000 over the 5h cap 1000 => 1.0
    _log(t, tier="frontier", units=100, outcome="clean", agent="a1")
    _log(t, tier="frontier", units=100, outcome="escalated", agent="a2")
    _log(t, tier="standard", units=500, outcome="clean", agent="a3")
    _log(t, tier="cheap", units=500, outcome="rework", agent="a4")
    capsys.readouterr()
    return t


def test_e2e_report_shows_real_tier_mix_and_numeric_headline(capsys):
    t = _run_e2e(capsys)

    # `atc report` through the real CLI: real tier mix + a NUMERIC headline
    rc = cli.main(["atc", "report", TEAM], transport=t)
    assert rc == 0
    out = capsys.readouterr().out

    assert "frontier 50%" in out, out
    # headline is numeric, not the n/a fallback
    assert "frontier window-days preserved" in out, out
    assert "n/a" not in out, out
    # the headline denominator is the seeded 5h cap (1000), value 1.0
    assert "~1.0 frontier window-days preserved" in out, out


def test_e2e_dash_data_tier_mix_matches_fold(capsys):
    t = _run_e2e(capsys)

    # dash_data over the SAME CLI read path (parse_accounts + _atc_usage_shards
    # + merge_models) — the fold that C1's tier pass-through feeds.
    text = t.read(cli._atc_accounts_path(TEAM))
    parsed = atc.parse_accounts(text)
    shards = cli._atc_usage_shards(t, TEAM)
    merged, _ = atc.merge_models(atc.load_default_models(),
                                 cli._atc_models_overlay(text))
    dd = atc_dash.dash_data(parsed, shards, team=TEAM, models=merged)

    assert dd["tier_mix"] == {"frontier": 50, "standard": 25, "cheap": 25}, dd
    assert "frontier window-days preserved" in dd["headline"], dd["headline"]
    assert "n/a" not in dd["headline"], dd["headline"]


def test_e2e_report_json_headline_numeric(capsys):
    t = _run_e2e(capsys)
    import json
    rc = cli.main(["atc", "report", TEAM, "--json"], transport=t)
    assert rc == 0
    rep = json.loads(capsys.readouterr().out)
    assert rep["headline"]["value"] == 1.0
    assert rep["headline"]["cap"] == 1000
    assert rep["headline"]["below_units"] == 1000
    tiers = {row["tier"]: row["pct"] for row in rep["tiers"]}
    assert tiers == {"frontier": 50, "standard": 25, "cheap": 25}
