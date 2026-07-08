"""ATC route fold + `coord-engine route` CLI verb (fulcra-agent-atc).

`route` is a pure fold over parsed accounts, a merged model map, requested
capability needs, and usage shards: it returns the models that cover every
need, bound to their best-headroom account, in a deterministic cost/headroom
order. The CLI verb wires it through the injected transport, reading the
optional operator overlay from accounts.json's top-level ``models`` key and
folding it over the packaged defaults via ``merge_models``.
"""
import json
from datetime import datetime, timedelta, timezone

from coord_engine import cli
from coord_engine.atc import route
from coord_engine_test_helpers import FakeTransport

NOW = datetime(2026, 7, 8, 6, 0, tzinfo=timezone.utc)


def _models(entries, map_version="test-v1"):
    return {"map_version": map_version, "models": entries}


def _accounts(*accts, tiers=None):
    return {"accounts": list(accts), "tiers": tiers or {}}


def _acct(id, harnesses, windows):
    return {"id": id, "harnesses": harnesses, "windows": windows}


def _shard(account, hours_ago, units, throttled=False):
    return {"account": account, "ts": NOW - timedelta(hours=hours_ago),
            "units": units, "throttled": throttled}


# --- coverage (all-needs AND) ------------------------------------------------

def test_coverage_requires_all_needs():
    models = _models({
        "covers": {"tags": ["code", "long-context"], "cost_rank": 3, "harnesses": ["h"]},
        "partial": {"tags": ["code"], "cost_rank": 3, "harnesses": ["h"]},
    })
    acct = _acct("a", ["h"], [{"hours": 5, "cap": 100}])
    res = route(_accounts(acct), models, ["code", "long-context"], [], now=NOW)
    assert [c["model"] for c in res["candidates"]] == ["covers"]


# --- harness filter ----------------------------------------------------------

def test_harness_filter_excludes_unmatched_model():
    models = _models({
        "wrong-harness": {"tags": ["code"], "cost_rank": 3, "harnesses": ["codex"]},
        "right-harness": {"tags": ["code"], "cost_rank": 3, "harnesses": ["claude-code"]},
    })
    acct = _acct("a", ["claude-code"], [{"hours": 5, "cap": 100}])
    res = route(_accounts(acct), models, ["code"], [], now=NOW)
    assert [c["model"] for c in res["candidates"]] == ["right-harness"]
    assert res["reason"] is None


# --- zero-headroom + throttle exclusion --------------------------------------

def test_zero_headroom_account_excluded():
    models = _models({"m": {"tags": ["code"], "cost_rank": 3, "harnesses": ["h"]}})
    acct = _acct("a", ["h"], [{"hours": 5, "cap": 100}])
    # 100 units used against a 100 cap -> headroom 0 -> account excluded
    res = route(_accounts(acct), models, ["code"], [_shard("a", 1, 100)], now=NOW)
    assert res["candidates"] == []
    assert res["reason"] == "no account headroom"


def test_any_window_zero_excludes_account():
    models = _models({"m": {"tags": ["code"], "cost_rank": 3, "harnesses": ["h"]}})
    # weekly window exhausted, 5h window healthy -> EVERY window must be >0
    acct = _acct("a", ["h"], [{"hours": 5, "cap": 100}, {"hours": 168, "cap": 100}])
    res = route(_accounts(acct), models, ["code"], [_shard("a", 6, 100)], now=NOW)
    assert res["candidates"] == [] and res["reason"] == "no account headroom"


def test_throttled_account_excluded():
    models = _models({"m": {"tags": ["code"], "cost_rank": 3, "harnesses": ["h"]}})
    acct = _acct("a", ["h"], [{"hours": 5, "cap": 100}])
    res = route(_accounts(acct), models, ["code"], [_shard("a", 1, 1, throttled=True)], now=NOW)
    assert res["candidates"] == [] and res["reason"] == "no account headroom"


# --- best-account binding ----------------------------------------------------

def test_binds_to_highest_min_window_headroom_account():
    models = _models({"m": {"tags": ["code"], "cost_rank": 3, "harnesses": ["h"]}})
    full = _acct("full", ["h"], [{"hours": 5, "cap": 100}])
    half = _acct("half", ["h"], [{"hours": 5, "cap": 100}])
    res = route(_accounts(half, full), models, ["code"], [_shard("half", 1, 50)], now=NOW)
    assert len(res["candidates"]) == 1
    assert res["candidates"][0]["account"] == "full"
    assert res["candidates"][0]["headroom_pct"] == 100.0


# --- sort order --------------------------------------------------------------

def test_sort_cost_rank_desc_then_id():
    models = _models({
        "m-hi": {"tags": ["code"], "cost_rank": 5, "harnesses": ["h"]},
        "m-lo": {"tags": ["code"], "cost_rank": 1, "harnesses": ["h"]},
        "m-mid-b": {"tags": ["code"], "cost_rank": 3, "harnesses": ["h"]},
        "m-mid-a": {"tags": ["code"], "cost_rank": 3, "harnesses": ["h"]},
    })
    acct = _acct("only", ["h"], [{"hours": 5, "cap": 100}])
    res = route(_accounts(acct), models, ["code"], [], now=NOW)
    # cost_rank DESC (cheapest cap-weight first), tie broken by model id ASC
    assert [c["model"] for c in res["candidates"]] == \
        ["m-hi", "m-mid-a", "m-mid-b", "m-lo"]


def test_sort_cost_rank_tie_broken_by_headroom_then_id():
    models = _models({
        "big": {"tags": ["code"], "cost_rank": 3, "harnesses": ["ha"]},
        "small": {"tags": ["code"], "cost_rank": 3, "harnesses": ["hb"]},
    })
    a = _acct("acct-a", ["ha"], [{"hours": 5, "cap": 100}])     # 100%
    b = _acct("acct-b", ["hb"], [{"hours": 5, "cap": 100}])     # 50% after 50 used
    res = route(_accounts(a, b), models, ["code"], [_shard("acct-b", 1, 50)], now=NOW)
    # same cost_rank -> higher headroom-% first
    assert [c["model"] for c in res["candidates"]] == ["big", "small"]


# --- demotions ---------------------------------------------------------------

def test_demotion_pushes_below_all_non_demoted():
    models = _models({
        "cheap": {"tags": ["code"], "cost_rank": 9, "harnesses": ["h"]},
        "pricey": {"tags": ["code"], "cost_rank": 1, "harnesses": ["h"]},
    })
    acct = _acct("a", ["h"], [{"hours": 5, "cap": 100}])
    # "cheap" would sort first on cost_rank, but it's demoted for "code"
    res = route(_accounts(acct), models, ["code"], [], demotions={"cheap": ["code"]}, now=NOW)
    ids = [c["model"] for c in res["candidates"]]
    assert ids == ["pricey", "cheap"]
    demoted = {c["model"]: c["demoted"] for c in res["candidates"]}
    assert demoted["cheap"] == ["code"] and demoted["pricey"] == []


def test_demotion_only_for_requested_need():
    models = _models({"m": {"tags": ["code", "vision"], "cost_rank": 3, "harnesses": ["h"]}})
    acct = _acct("a", ["h"], [{"hours": 5, "cap": 100}])
    # demoted for "vision" but only "code" is requested -> not demoted
    res = route(_accounts(acct), models, ["code"], [], demotions={"m": ["vision"]}, now=NOW)
    assert res["candidates"][0]["demoted"] == []


# --- unknown need ------------------------------------------------------------

def test_unknown_need_sets_reason_and_empty():
    models = _models({"m": {"tags": ["code"], "cost_rank": 3, "harnesses": ["h"]}})
    acct = _acct("a", ["h"], [{"hours": 5, "cap": 100}])
    res = route(_accounts(acct), models, ["code", "telepathy"], [], now=NOW)
    assert res["candidates"] == []
    assert res["reason"] == "unknown need: telepathy"


# --- empty-result reasons ----------------------------------------------------

def test_no_model_covers_needs_reason():
    models = _models({"m": {"tags": ["writing"], "cost_rank": 3, "harnesses": ["h"]}})
    acct = _acct("a", ["h"], [{"hours": 5, "cap": 100}])
    res = route(_accounts(acct), models, ["code"], [], now=NOW)
    assert res["candidates"] == [] and res["reason"] == "no model covers needs"


# --- defensive coercion (overlay entries pass through merge unvalidated) -----

def test_bad_cost_rank_coerced_to_mid_and_reported():
    models = _models({
        "strval": {"tags": ["code"], "cost_rank": "cheap", "harnesses": ["h"]},
        "oob": {"tags": ["code"], "cost_rank": 99, "harnesses": ["h"]},
    })
    acct = _acct("a", ["h"], [{"hours": 5, "cap": 100}])
    res = route(_accounts(acct), models, ["code"], [], now=NOW)
    ranks = {c["model"]: c["cost_rank"] for c in res["candidates"]}
    assert ranks == {"strval": 5, "oob": 5}
    assert any("strval" in r for r in res["dropped_unknown_tags"])
    assert any("oob" in r for r in res["dropped_unknown_tags"])


def test_bad_harnesses_makes_model_unroutable_and_reported():
    models = _models({"m": {"tags": ["code"], "cost_rank": 3, "harnesses": "claude-code"}})
    acct = _acct("a", ["claude-code"], [{"hours": 5, "cap": 100}])
    res = route(_accounts(acct), models, ["code"], [], now=NOW)
    assert res["candidates"] == []          # non-list harnesses -> unroutable
    assert any("harnesses" in r for r in res["dropped_unknown_tags"])


def test_map_version_surfaced():
    models = _models({"m": {"tags": ["code"], "cost_rank": 3, "harnesses": ["h"]}},
                     map_version="2026-07-08")
    acct = _acct("a", ["h"], [{"hours": 5, "cap": 100}])
    res = route(_accounts(acct), models, ["code"], [], now=NOW)
    assert res["map_version"] == "2026-07-08"


# --- CLI verb ----------------------------------------------------------------

ACCOUNTS = json.dumps({"accounts": [
    {"id": "anthropic-max", "provider": "anthropic", "plan": "max",
     "harnesses": ["claude-code", "cowork"],
     "windows": [{"hours": 5, "cap": 800}]}],
    "tiers": {}})


def test_cli_route_text(capsys):
    t = FakeTransport()
    t.put("team/fulcra/atc/accounts.json", ACCOUNTS)
    rc = cli.main(["route", "fulcra", "--needs", "code,architecture"], transport=t)
    out = capsys.readouterr().out
    assert rc == 0
    # a claude-code model covering code+architecture, bound to anthropic-max at 100%
    assert "anthropic-max" in out and "100%" in out
    assert "claude-" in out  # some Anthropic model surfaced from defaults


def test_cli_route_json_shape(capsys):
    t = FakeTransport()
    t.put("team/fulcra/atc/accounts.json", ACCOUNTS)
    rc = cli.main(["route", "fulcra", "--needs", "code", "--json"], transport=t)
    doc = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert doc["map_version"] and doc["reason"] is None
    assert "dropped_unknown_tags" in doc
    c = doc["candidates"][0]
    assert set(c) >= {"model", "account", "headroom_pct", "tags", "cost_rank", "demoted"}


def test_cli_route_unknown_need_exit_2(capsys):
    t = FakeTransport()
    t.put("team/fulcra/atc/accounts.json", ACCOUNTS)
    rc = cli.main(["route", "fulcra", "--needs", "telepathy"], transport=t)
    assert rc == 2


def test_cli_route_empty_reason_exit_0(capsys):
    # no accounts declared -> no account headroom, graceful exit 0
    t = FakeTransport()
    t.put("team/fulcra/atc/accounts.json", json.dumps({"accounts": [], "tiers": {}}))
    rc = cli.main(["route", "fulcra", "--needs", "code"], transport=t)
    out = capsys.readouterr().out
    assert rc == 0 and "no candidates" in out


def test_cli_route_operator_overlay_applied(capsys):
    # accounts.json carries a top-level `models` overlay: a new local model on
    # harness "ollama-local" bound to a local account. It must surface.
    doc = {"accounts": [
        {"id": "local-box", "harnesses": ["ollama-local"],
         "windows": [{"hours": 24, "cap": 100000}]}],
        "tiers": {},
        "models": {"my-local-7b": {"tags": ["code"], "cost_rank": 9,
                                   "harnesses": ["ollama-local"]}}}
    t = FakeTransport()
    t.put("team/fulcra/atc/accounts.json", json.dumps(doc))
    rc = cli.main(["route", "fulcra", "--needs", "code", "--json"], transport=t)
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert any(c["model"] == "my-local-7b" for c in out["candidates"])


def test_cli_route_v1_accounts_without_models_key(capsys):
    # v1 accounts.json (no `models` key) must work off packaged defaults only.
    t = FakeTransport()
    t.put("team/fulcra/atc/accounts.json", ACCOUNTS)
    rc = cli.main(["route", "fulcra", "--needs", "code", "--json"], transport=t)
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and len(out["candidates"]) >= 1
