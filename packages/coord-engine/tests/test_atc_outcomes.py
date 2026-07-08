"""ATC outcome shards + demotion fold (fulcra-agent-atc, task 3).

`demotions` is a pure fold over usage shards that carry the optional
model/task_class/outcome fields: for each (model, task_class) pair it inspects
the trailing window of outcome-bearing shards and reports the pair as demoted
when recent work has gone badly (rework/escalated). v1 shards lacking the new
fields flow through untouched and are ignored by the fold.

Also pins two binding controller amendments:
  1. an account declaring ZERO windows is "uncapped" -> eligible at 100.0%
     headroom (the local-ollama case), not ineligible.
  2. the insufficient-evidence rule is strict: demote only when >=3
     outcome-bearing shards exist AND >=3 of the trailing (up to 5) are bad.
"""
import json
from datetime import datetime, timedelta, timezone

from coord_engine import cli
from coord_engine.atc import demotions, _demotions_for_route, route
from coord_engine_test_helpers import FakeTransport

NOW = datetime(2026, 7, 8, 6, 0, tzinfo=timezone.utc)


def _sh(model, tc, outcome, age_h, account="a", units=1, throttled=False):
    """A usage shard row (headroom-fold shape) carrying the task-3 outcome
    fields. Larger age_h => older shard (ts earlier)."""
    return {"account": account, "ts": NOW - timedelta(hours=age_h),
            "units": units, "throttled": throttled,
            "model": model, "task_class": tc, "outcome": outcome}


# --- demotions fold: boundary 3/5 vs 2/5 -------------------------------------

def test_boundary_three_of_five_demotes():
    shards = [
        _sh("m", "code", "rework", 5),
        _sh("m", "code", "rework", 4),
        _sh("m", "code", "rework", 3),
        _sh("m", "code", "clean", 2),
        _sh("m", "code", "clean", 1),
    ]
    demo = demotions(shards)
    assert demo[("m", "code")] == {"bad": 3, "of": 5, "window": 5}


def test_boundary_two_of_five_does_not_demote():
    shards = [
        _sh("m", "code", "rework", 5),
        _sh("m", "code", "escalated", 4),
        _sh("m", "code", "clean", 3),
        _sh("m", "code", "clean", 2),
        _sh("m", "code", "clean", 1),
    ]
    assert demotions(shards) == {}


def test_escalated_counts_as_bad():
    shards = [
        _sh("m", "code", "escalated", 3),
        _sh("m", "code", "escalated", 2),
        _sh("m", "code", "rework", 1),
    ]
    assert demotions(shards)[("m", "code")] == {"bad": 3, "of": 3, "window": 5}


# --- recovery: later clean shards push the trailing ratio under --------------

def test_recovery_pushes_ratio_under_threshold():
    # three bad (oldest), then three clean (newest); trailing 5 = 2 bad + 3 clean.
    shards = [
        _sh("m", "code", "rework", 6),
        _sh("m", "code", "rework", 5),
        _sh("m", "code", "rework", 4),
        _sh("m", "code", "clean", 3),
        _sh("m", "code", "clean", 2),
        _sh("m", "code", "clean", 1),
    ]
    assert demotions(shards) == {}


def test_bad_run_at_the_end_still_demotes():
    # three clean (oldest), then three bad (newest); trailing 5 = 2 clean + 3 bad.
    shards = [
        _sh("m", "code", "clean", 6),
        _sh("m", "code", "clean", 5),
        _sh("m", "code", "clean", 4),
        _sh("m", "code", "rework", 3),
        _sh("m", "code", "rework", 2),
        _sh("m", "code", "escalated", 1),
    ]
    assert demotions(shards)[("m", "code")] == {"bad": 3, "of": 5, "window": 5}


# --- insufficient evidence (amendment #2, strict) ----------------------------

def test_three_of_three_bad_demotes():
    shards = [_sh("m", "code", "rework", 3), _sh("m", "code", "rework", 2),
              _sh("m", "code", "escalated", 1)]
    assert demotions(shards)[("m", "code")] == {"bad": 3, "of": 3, "window": 5}


def test_two_of_two_bad_does_not_demote_insufficient_evidence():
    # 2-of-2 bad is NOT enough: need >=3 outcome-bearing shards to demote at all.
    shards = [_sh("m", "code", "rework", 2), _sh("m", "code", "escalated", 1)]
    assert demotions(shards) == {}


def test_two_bad_one_clean_does_not_demote():
    # 3 outcome shards exist, but only 2 are bad -> below the >=3 bad threshold.
    shards = [_sh("m", "code", "rework", 3), _sh("m", "code", "escalated", 2),
              _sh("m", "code", "clean", 1)]
    assert demotions(shards) == {}


# --- v1 shards (no new fields) ignored silently ------------------------------

def test_v1_shards_without_outcome_fields_ignored():
    shards = [
        {"account": "a", "ts": NOW - timedelta(hours=3), "units": 10},
        {"account": "a", "ts": NOW - timedelta(hours=2), "units": 10,
         "model": "m", "task_class": "code"},          # no outcome
        {"account": "a", "ts": NOW - timedelta(hours=1), "units": 10,
         "model": "m", "outcome": "rework"},            # no task_class
    ]
    assert demotions(shards) == {}


def test_mixed_v1_and_outcome_shards_only_counts_outcome_bearing():
    shards = [
        {"account": "a", "ts": NOW - timedelta(hours=9), "units": 5},  # v1 noise
        _sh("m", "code", "rework", 3),
        _sh("m", "code", "rework", 2),
        _sh("m", "code", "escalated", 1),
    ]
    assert demotions(shards)[("m", "code")] == {"bad": 3, "of": 3, "window": 5}


# --- deterministic ts ordering + independent groups --------------------------

def test_trailing_window_uses_latest_by_ts_regardless_of_input_order():
    # Six bad-then-clean shards fed in shuffled order; the trailing 5 by ts must
    # still be the five most-recent (3 clean newest -> not demoted).
    shards = [
        _sh("m", "code", "clean", 1),
        _sh("m", "code", "rework", 6),
        _sh("m", "code", "clean", 2),
        _sh("m", "code", "rework", 5),
        _sh("m", "code", "clean", 3),
        _sh("m", "code", "rework", 4),
    ]
    assert demotions(shards) == {}


def test_groups_are_independent():
    shards = [
        _sh("m", "code", "rework", 3), _sh("m", "code", "rework", 2),
        _sh("m", "code", "rework", 1),
        _sh("m", "architecture", "clean", 3), _sh("m", "architecture", "clean", 2),
        _sh("m", "architecture", "clean", 1),
        _sh("other", "code", "clean", 3),
    ]
    demo = demotions(shards)
    assert set(demo) == {("m", "code")}


# --- adapter to route's demotions shape --------------------------------------

def test_demotions_for_route_groups_task_classes_per_model():
    demo_map = {("m", "code"): {"bad": 3, "of": 3, "window": 5},
                ("m", "architecture"): {"bad": 3, "of": 5, "window": 5},
                ("n", "code"): {"bad": 3, "of": 3, "window": 5}}
    adapted = _demotions_for_route(demo_map)
    assert sorted(adapted["m"]) == ["architecture", "code"]
    assert adapted["n"] == ["code"]


def test_demotions_for_route_empty():
    assert _demotions_for_route({}) == {}


# --- amendment #1: no-window (uncapped) accounts are eligible at 100% ---------

def _models(entries, map_version="test-v1"):
    return {"map_version": map_version, "models": entries}


def _accounts(*accts):
    return {"accounts": list(accts), "tiers": {}}


def test_uncapped_account_routes_at_full_headroom():
    models = _models({"m": {"tags": ["code"], "cost_rank": 3, "harnesses": ["ollama-local"]}})
    uncapped = {"id": "local-box", "harnesses": ["ollama-local"], "windows": []}
    res = route(_accounts(uncapped), models, ["code"], [], now=NOW)
    assert len(res["candidates"]) == 1
    c = res["candidates"][0]
    assert c["account"] == "local-box" and c["headroom_pct"] == 100.0


def test_uncapped_account_missing_windows_key_also_eligible():
    models = _models({"m": {"tags": ["code"], "cost_rank": 3, "harnesses": ["h"]}})
    uncapped = {"id": "box", "harnesses": ["h"]}  # no 'windows' key at all
    res = route(_accounts(uncapped), models, ["code"], [], now=NOW)
    assert res["candidates"][0]["headroom_pct"] == 100.0


def test_uncapped_account_sorts_by_cost_rank_with_full_headroom():
    models = _models({
        "hi": {"tags": ["code"], "cost_rank": 5, "harnesses": ["h"]},
        "lo": {"tags": ["code"], "cost_rank": 1, "harnesses": ["h"]},
    })
    uncapped = {"id": "box", "harnesses": ["h"], "windows": []}
    res = route(_accounts(uncapped), models, ["code"], [], now=NOW)
    assert [c["model"] for c in res["candidates"]] == ["hi", "lo"]
    assert all(c["headroom_pct"] == 100.0 for c in res["candidates"])


def test_uncapped_account_stays_eligible_despite_throttled_shard():
    # Known conservative gap: an uncapped account declares no windows, so a
    # throttled shard has no window to zero -> the account cannot be
    # throttle-excluded and keeps routing at 100%.
    models = _models({"m": {"tags": ["code"], "cost_rank": 3, "harnesses": ["h"]}})
    uncapped = {"id": "box", "harnesses": ["h"], "windows": []}
    throttled = {"account": "box", "ts": NOW - timedelta(hours=1),
                 "units": 1, "throttled": True}
    res = route(_accounts(uncapped), models, ["code"], [throttled], now=NOW)
    assert res["candidates"][0]["headroom_pct"] == 100.0


# --- route wired to the demotions fold via the adapter -----------------------

def test_route_demotes_pair_from_adapted_fold():
    models = _models({
        "cheap": {"tags": ["code"], "cost_rank": 9, "harnesses": ["h"]},
        "pricey": {"tags": ["code"], "cost_rank": 1, "harnesses": ["h"]},
    })
    acct = {"id": "a", "harnesses": ["h"], "windows": [{"hours": 5, "cap": 100}]}
    demo_map = {("cheap", "code"): {"bad": 3, "of": 3, "window": 5}}
    res = route(_accounts(acct), models, ["code"], [],
                demotions=_demotions_for_route(demo_map), now=NOW)
    assert [c["model"] for c in res["candidates"]] == ["pricey", "cheap"]
    demoted = {c["model"]: c["demoted"] for c in res["candidates"]}
    assert demoted["cheap"] == ["code"] and demoted["pricey"] == []


# --- CLI: usage log gains --model / --task-class / --outcome -----------------

_OVERLAY = {
    "accounts": [{"id": "box", "harnesses": ["h"], "windows": [{"hours": 5, "cap": 1000}]}],
    "tiers": {},
    "models": {
        "cheap": {"tags": ["code"], "cost_rank": 9, "harnesses": ["h"]},
        "pricey": {"tags": ["code"], "cost_rank": 1, "harnesses": ["h"]},
    },
}


def _seed(t):
    t.put("team/fulcra/atc/accounts.json", json.dumps(_OVERLAY))


def _log_bad(t, n=3, model="cheap", tc="code", outcome="rework"):
    for _ in range(n):
        cli.main(["usage", "log", "fulcra", "--account", "box", "--tier", "x",
                  "--units", "1", "--model", model, "--task-class", tc,
                  "--outcome", outcome], transport=t)


def test_usage_log_writes_outcome_fields():
    t = FakeTransport()
    _seed(t)
    rc = cli.main(["usage", "log", "fulcra", "--account", "box", "--tier", "x",
                   "--units", "5", "--model", "cheap", "--task-class", "code",
                   "--outcome", "rework"], transport=t)
    assert rc == 0
    body = next(v for p, v in t.store.items() if p.startswith("team/fulcra/atc/usage/"))
    assert "cheap" in body and "code" in body and "rework" in body


def test_usage_log_v1_shard_omits_new_fields():
    t = FakeTransport()
    _seed(t)
    cli.main(["usage", "log", "fulcra", "--account", "box", "--tier", "x",
              "--units", "5"], transport=t)
    body = next(v for p, v in t.store.items() if p.startswith("team/fulcra/atc/usage/"))
    assert "task_class" not in body and "outcome" not in body and "model" not in body


def test_usage_log_unknown_task_class_exits_2(capsys):
    t = FakeTransport()
    _seed(t)
    rc = cli.main(["usage", "log", "fulcra", "--account", "box", "--tier", "x",
                   "--task-class", "telepathy"], transport=t)
    assert rc == 2
    # no shard written on the rejected invocation
    assert not [p for p in t.store if p.startswith("team/fulcra/atc/usage/")]


def test_cli_route_demotes_model_with_bad_outcomes(capsys):
    t = FakeTransport()
    _seed(t)
    _log_bad(t, n=3)
    capsys.readouterr()
    rc = cli.main(["route", "fulcra", "--needs", "code"], transport=t)
    out = capsys.readouterr().out
    assert rc == 0
    lines = [l for l in out.splitlines() if l[:2] in ("1.", "2.")]
    assert "pricey" in lines[0] and "cheap" in lines[1]
    assert "[demoted: code]" in lines[1]


def test_cli_route_json_demoted_marker(capsys):
    t = FakeTransport()
    _seed(t)
    _log_bad(t, n=3)
    capsys.readouterr()
    cli.main(["route", "fulcra", "--needs", "code", "--json"], transport=t)
    doc = json.loads(capsys.readouterr().out)
    by_model = {c["model"]: c for c in doc["candidates"]}
    assert by_model["cheap"]["demoted"] == ["code"]
    assert by_model["pricey"]["demoted"] == []


def test_cli_headroom_json_surfaces_demotions(capsys):
    t = FakeTransport()
    _seed(t)
    _log_bad(t, n=3)
    capsys.readouterr()
    cli.main(["headroom", "fulcra", "--json"], transport=t)
    doc = json.loads(capsys.readouterr().out)
    assert isinstance(doc["windows"], list) and doc["windows"]
    assert any(d["model"] == "cheap" and d["task_class"] == "code"
               and d["bad"] == 3 and d["of"] == 3 for d in doc["demotions"])


def test_cli_headroom_json_no_demotions_empty_list(capsys):
    t = FakeTransport()
    _seed(t)
    cli.main(["headroom", "fulcra", "--json"], transport=t)
    doc = json.loads(capsys.readouterr().out)
    assert doc["demotions"] == []
