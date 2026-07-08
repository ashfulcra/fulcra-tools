"""`atc init` — standalone ATC onboarding (Task 6).

Seeds `team/<team>/atc/accounts.json` via the SAME injected-transport write seam
the review-request flow uses, so a fresh operator gets a routable ledger in one
command. Covers: --yes non-interactive writes a parse_accounts-round-tripping
doc with the plan-seeded caps; idempotent merge preserves existing entries
(and sibling keys like tiers/models); the interactive numbered-prompt path via
monkeypatched `input`; and the zero-accounts refusal (exit 2).
"""
import json

from coord_engine import cli, atc
from coord_engine_test_helpers import FakeTransport

PATH = "team/solo/atc/accounts.json"


def test_yes_writes_parseable_seeded_account(capsys):
    t = FakeTransport()
    rc = cli.main(["atc", "init", "--yes",
                   "--account", "anthropic-main=anthropic:max"], transport=t)
    assert rc == 0
    raw = t.read(PATH)
    assert raw is not None, "init must write accounts.json via the transport seam"
    # round-trips through the canonical fold
    parsed = atc.parse_accounts(raw)
    assert "error" not in parsed
    accts = {a["id"]: a for a in parsed["accounts"]}
    a = accts["anthropic-main"]
    assert a["provider"] == "anthropic" and a["plan"] == "max"
    # harnesses = union of the default map's anthropic harnesses
    assert set(a["harnesses"]) == {"claude-code", "cowork"}
    # anthropic seed: 5h/1000 + 168h/15000
    assert a["windows"] == [{"hours": 5, "cap": 1000},
                            {"hours": 168, "cap": 15000}]


def test_yes_openai_and_other_provider_seeds(capsys):
    t = FakeTransport()
    rc = cli.main(["atc", "init", "--yes",
                   "--account", "codex=openai:pro",
                   "--account", "grok=xai:"], transport=t)
    assert rc == 0
    accts = {a["id"]: a for a in atc.parse_accounts(t.read(PATH))["accounts"]}
    assert accts["codex"]["windows"] == [{"hours": 5, "cap": 600}]
    assert set(accts["codex"]["harnesses"]) == {"codex"}
    # any other provider -> 5h/500 placeholder
    assert accts["grok"]["windows"] == [{"hours": 5, "cap": 500}]
    assert set(accts["grok"]["harnesses"]) == {"grok-cli"}
    # empty plan is omitted, not written as ""
    assert "plan" not in accts["grok"]


def test_harness_override(capsys):
    t = FakeTransport()
    cli.main(["atc", "init", "--yes", "--account", "a=anthropic:max",
              "--harness", "claude-code"], transport=t)
    accts = {a["id"]: a for a in atc.parse_accounts(t.read(PATH))["accounts"]}
    assert accts["a"]["harnesses"] == ["claude-code"]


def test_idempotent_merge_preserves_existing(capsys):
    t = FakeTransport()
    existing = json.dumps({
        "accounts": [{"id": "codex", "provider": "openai", "plan": "pro",
                      "harnesses": ["codex"],
                      "windows": [{"hours": 5, "cap": 600}]}],
        "tiers": {"cheap": ["haiku-4.5"]},
        "models": {"custom-x": {"tags": ["code"], "cost_rank": 4,
                                "harnesses": ["codex"]}},
    })
    t.put(PATH, existing)
    rc = cli.main(["atc", "init", "--yes",
                   "--account", "anthropic-main=anthropic:max"], transport=t)
    assert rc == 0
    doc = json.loads(t.read(PATH))
    ids = [a["id"] for a in doc["accounts"]]
    assert ids == ["codex", "anthropic-main"], "existing kept, new appended"
    # sibling keys survive the merge
    assert doc["tiers"] == {"cheap": ["haiku-4.5"]}
    assert doc["models"]["custom-x"]["cost_rank"] == 4

    # re-running with the same id is a no-op merge (no duplicate)
    capsys.readouterr()
    rc = cli.main(["atc", "init", "--yes",
                   "--account", "anthropic-main=anthropic:max"], transport=t)
    assert rc == 0
    doc = json.loads(t.read(PATH))
    assert [a["id"] for a in doc["accounts"]] == ["codex", "anthropic-main"]


def test_interactive_numbered_prompts(monkeypatch, capsys):
    t = FakeTransport()
    # provider list is sorted: 1=anthropic; then id (default) + plan prompts.
    answers = iter(["1", "", "max"])
    monkeypatch.setattr("builtins.input", lambda *a: next(answers))
    rc = cli.main(["atc", "init"], transport=t)  # no --yes -> interactive
    assert rc == 0
    accts = {a["id"]: a for a in atc.parse_accounts(t.read(PATH))["accounts"]}
    # default id for anthropic is anthropic-main
    assert "anthropic-main" in accts
    assert accts["anthropic-main"]["plan"] == "max"
    assert accts["anthropic-main"]["windows"][0] == {"hours": 5, "cap": 1000}


def test_refuses_zero_accounts_yes(capsys):
    t = FakeTransport()
    rc = cli.main(["atc", "init", "--yes"], transport=t)
    assert rc == 2
    assert t.read(PATH) is None, "no doc written when zero accounts declared"


def test_refuses_zero_accounts_interactive(monkeypatch, capsys):
    t = FakeTransport()
    monkeypatch.setattr("builtins.input", lambda *a: "")  # select nothing
    rc = cli.main(["atc", "init"], transport=t)
    assert rc == 2
    assert t.read(PATH) is None


def test_bad_account_spec_yes(capsys):
    t = FakeTransport()
    rc = cli.main(["atc", "init", "--yes", "--account", "no-provider"],
                  transport=t)
    assert rc == 2
    assert t.read(PATH) is None


def test_prints_three_paste_lines(capsys):
    t = FakeTransport()
    cli.main(["atc", "init", "--yes", "--account", "anthropic-main=anthropic:max"],
             transport=t)
    out = capsys.readouterr().out
    assert "skills/fulcra-agent-atc/SKILL.md" in out
    assert "coord-engine route solo --needs code" in out
    assert "coord-engine usage log solo --account anthropic-main" in out
    assert "--tier" in out and "--task-class" in out and "--outcome clean" in out
