import json
from datetime import datetime, timedelta, timezone
from coord_engine.atc import parse_accounts, headroom

NOW = datetime(2026, 7, 8, 6, 0, tzinfo=timezone.utc)

ACCOUNTS_JSON = json.dumps({
    "accounts": [
        {"id": "anthropic-max", "provider": "anthropic", "plan": "max",
         "harnesses": ["claude-code", "cowork"],
         "windows": [{"hours": 5, "cap": 800}, {"hours": 168, "cap": 12000}]},
        {"id": "openai-codex", "provider": "openai", "plan": "pro",
         "harnesses": ["codex"],
         "windows": [{"hours": 5, "cap": 600}]},
    ],
    "tiers": {"frontier": ["fable-5"], "standard": ["opus-4.8", "sonnet-5"],
              "cheap": ["haiku-4.5"]},
})

def _shard(account, hours_ago, units, throttled=False):
    return {"account": account, "ts": NOW - timedelta(hours=hours_ago),
            "units": units, "throttled": throttled}

def test_parse_accounts_roundtrip():
    d = parse_accounts(ACCOUNTS_JSON)
    assert [a["id"] for a in d["accounts"]] == ["anthropic-max", "openai-codex"]
    assert d["tiers"]["cheap"] == ["haiku-4.5"]
    assert "error" not in d

def test_parse_accounts_none_and_malformed():
    assert parse_accounts(None) == {"accounts": [], "tiers": {}}
    bad = parse_accounts("{not json")
    assert bad["accounts"] == [] and bad["tiers"] == {} and "error" in bad

def test_headroom_window_math():
    accounts = parse_accounts(ACCOUNTS_JSON)["accounts"]
    rows = headroom(accounts, [_shard("anthropic-max", 1, 200),
                               _shard("anthropic-max", 6, 300)], NOW)
    r5 = next(r for r in rows if r["account"] == "anthropic-max" and r["window_hours"] == 5)
    assert (r5["used"], r5["headroom"]) == (200, 600)          # 6h-old shard outside 5h window
    r168 = next(r for r in rows if r["account"] == "anthropic-max" and r["window_hours"] == 168)
    assert (r168["used"], r168["headroom"]) == (500, 11500)     # both inside weekly window
    assert r5["pct"] == 75.0

def test_throttled_zeroes_window_and_flags_calibrate():
    accounts = parse_accounts(ACCOUNTS_JSON)["accounts"]
    rows = headroom(accounts, [_shard("anthropic-max", 1, 100, throttled=True)], NOW)
    r5 = next(r for r in rows if r["account"] == "anthropic-max" and r["window_hours"] == 5)
    assert r5["headroom"] == 0 and r5["throttled"] is True and r5["calibrate"] is True

def test_throttle_expires_with_window():
    accounts = parse_accounts(ACCOUNTS_JSON)["accounts"]
    rows = headroom(accounts, [_shard("anthropic-max", 6, 100, throttled=True)], NOW)
    r5 = next(r for r in rows if r["account"] == "anthropic-max" and r["window_hours"] == 5)
    assert r5["throttled"] is False and r5["headroom"] == 800

def test_unknown_account_shard_ignored_not_crash():
    accounts = parse_accounts(ACCOUNTS_JSON)["accounts"]
    rows = headroom(accounts, [_shard("ghost", 1, 100)], NOW)
    assert all(r["used"] == 0 for r in rows)

def test_empty_ledger_full_headroom():
    accounts = parse_accounts(ACCOUNTS_JSON)["accounts"]
    rows = headroom(accounts, [], NOW)
    assert all(r["headroom"] == r["cap"] and r["pct"] == 100.0 for r in rows)
    assert [(r["account"], r["window_hours"]) for r in rows] == [
        ("anthropic-max", 5), ("anthropic-max", 168), ("openai-codex", 5)]
