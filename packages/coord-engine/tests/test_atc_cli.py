"""ATC CLI verbs — `usage log` (write shard) + `headroom` (fold accounts+shards).

Wired through the injected transport, matching the repo convention: the
in-memory FakeTransport is imported from coord_engine_test_helpers (as the
continuity-audit wiring tests do), constructed no-arg, seeded via ``.put`` and
inspected via ``.store``.
"""
import json

from coord_engine import cli
from coord_engine_test_helpers import FakeTransport

ACCOUNTS = json.dumps({"accounts": [
    {"id": "anthropic-max", "provider": "anthropic", "plan": "max",
     "harnesses": ["claude-code"], "windows": [{"hours": 5, "cap": 800}]}],
    "tiers": {"cheap": ["haiku-4.5"]}})


def test_usage_log_writes_shard(capsys):
    t = FakeTransport()
    t.put("team/fulcra/atc/accounts.json", ACCOUNTS)
    rc = cli.main(["usage", "log", "fulcra", "--account", "anthropic-max",
                   "--tier", "frontier", "--units", "250"], transport=t)
    assert rc == 0
    shard_paths = [p for p in t.store if p.startswith("team/fulcra/atc/usage/")]
    assert len(shard_paths) == 1
    body = t.store[shard_paths[0]]
    assert "anthropic-max" in body and "250" in body and "frontier" in body


def test_headroom_text_and_json(capsys):
    t = FakeTransport()
    t.put("team/fulcra/atc/accounts.json", ACCOUNTS)
    cli.main(["usage", "log", "fulcra", "--account", "anthropic-max",
              "--tier", "frontier", "--units", "200"], transport=t)
    capsys.readouterr()
    rc = cli.main(["headroom", "fulcra"], transport=t)
    out = capsys.readouterr().out
    assert rc == 0 and "anthropic-max" in out and "600" in out and "75.0%" in out
    rc = cli.main(["headroom", "fulcra", "--json"], transport=t)
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]["headroom"] == 600


def test_headroom_no_accounts_doc_graceful(capsys):
    t = FakeTransport()
    rc = cli.main(["headroom", "fulcra"], transport=t)
    out = capsys.readouterr().out
    assert rc == 0 and "no accounts declared" in out


def test_headroom_malformed_shard_does_not_crash(capsys):
    t = FakeTransport()
    t.put("team/fulcra/atc/accounts.json", ACCOUNTS)
    t.put("team/fulcra/atc/usage/bad.md", "{{{{not frontmatter")
    rc = cli.main(["headroom", "fulcra"], transport=t)
    assert rc == 0 and "anthropic-max" in capsys.readouterr().out


def test_throttled_flag_round_trip(capsys):
    t = FakeTransport()
    t.put("team/fulcra/atc/accounts.json", ACCOUNTS)
    cli.main(["usage", "log", "fulcra", "--account", "anthropic-max",
              "--tier", "frontier", "--throttled"], transport=t)
    capsys.readouterr()
    cli.main(["headroom", "fulcra", "--json"], transport=t)
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]["headroom"] == 0 and rows[0]["calibrate"] is True


def test_digest_flags_low_headroom(capsys):
    low = json.dumps({"accounts": [
        {"id": "anthropic-max", "provider": "anthropic", "plan": "max",
         "harnesses": ["claude-code"], "windows": [{"hours": 5, "cap": 100}]}],
        "tiers": {}})
    t = FakeTransport()
    t.put("team/fulcra/atc/accounts.json", low)
    cli.main(["usage", "log", "fulcra", "--account", "anthropic-max",
              "--tier", "frontier", "--units", "90"], transport=t)
    capsys.readouterr()
    cli.main(["digest", "fulcra"], transport=t)
    out = capsys.readouterr().out
    assert "headroom" in out and "anthropic-max" in out and "10.0%" in out


def test_digest_silent_when_headroom_healthy(capsys):
    t = FakeTransport()
    t.put("team/fulcra/atc/accounts.json", ACCOUNTS)
    cli.main(["digest", "fulcra"], transport=t)
    assert "headroom" not in capsys.readouterr().out
