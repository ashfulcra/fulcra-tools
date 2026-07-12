"""Coordinator joins: atc/bindings.json, `atc harvest`, and `route --for-role`.

Bindings map an agent/role identity to (account, tier[, model, task_class]);
harvest derives idempotent outcome shards from SETTLED review families; route
--for-role filters candidates to the role's bound account and surfaces lease
liveness so a coordinator never routes into a void silently.
"""
import json

import sys
sys.path.insert(0, ".")  # root-pythonpath convention (see root pyproject)

from coord_engine import atc, cli
from coord_engine_test_helpers import FakeTransport


BINDINGS = json.dumps({"bindings": [
    {"agent": "codex-reviewer", "account": "acct-b", "tier": "standard",
     "model": "gpt-x", "task_class": "code"},
    {"agent": "coord-boss", "account": "acct-a", "tier": "frontier"},
]})

ACCOUNTS = json.dumps({"accounts": [
    {"id": "acct-a", "provider": "anthropic", "plan": "max",
     "harnesses": ["claude-code"], "windows": [{"hours": 5, "cap": 100}]},
    {"id": "acct-b", "provider": "openai", "plan": "pro",
     "harnesses": ["codex"], "windows": [{"hours": 5, "cap": 100}]},
]})


def _seed_settled_review(t, slug, requested_by):
    t.put(f"team/r/review/{slug}.md",
          f"---\ntype: Review\nrequested_by: {requested_by}\n---\n")
    t.put(f"team/r/review/{slug}/verdicts/rev.md",
          "---\ntype: Verdict\nreviewer: rev\nverdict: approve\n---\n")
    t.put(f"team/r/review/{slug}/verdicts/.settled", "APPROVED")


# --- parse_bindings -----------------------------------------------------------

def test_parse_bindings_tolerates_bad_entries():
    text = json.dumps({"bindings": [
        {"agent": "a", "account": "x", "tier": "standard"},
        {"agent": "", "account": "x", "tier": "standard"},       # empty agent
        {"agent": "b", "account": "x", "tier": "standard",
         "task_class": "nonsense"},                               # bad taxonomy
        "not-an-object",
    ]})
    out = atc.parse_bindings(text)
    assert set(out["bindings"]) == {"a"}
    assert len(out["dropped"]) == 3
    assert atc.parse_bindings(None) == {"bindings": {}, "dropped": [], "error": None}
    assert atc.parse_bindings("{nope")["error"]


def test_review_families_and_outcome():
    fams = atc.review_families(["pr-7", "pr-7-r2", "pr-7-r3", "solo", "x-r2"])
    assert fams["pr-7"] == ["pr-7", "pr-7-r2", "pr-7-r3"]
    assert fams["solo"] == ["solo"]
    # x-r2 with no base "x" present stays its own family (not an orphan fold)
    assert fams["x-r2"] == ["x-r2"]
    assert atc.family_outcome(["pr-7"]) == "clean"
    assert atc.family_outcome(["pr-7", "pr-7-r2"]) == "rework"


# --- atc harvest --------------------------------------------------------------

def test_harvest_writes_attributed_shards_and_is_idempotent(capsys):
    t = FakeTransport()
    t.put("team/r/atc/bindings.json", BINDINGS)
    _seed_settled_review(t, "pr-1", "codex-reviewer")             # clean
    _seed_settled_review(t, "pr-2", "codex-reviewer")
    _seed_settled_review(t, "pr-2-r2", "codex-reviewer")          # rework family
    assert cli.main(["atc", "harvest", "r"], transport=t) == 0
    out = capsys.readouterr().out
    assert "2 shard(s) written" in out
    s1 = t.store["team/r/atc/usage/harvest-pr-1.md"]
    assert "outcome: clean" in s1 and "model: gpt-x" in s1 \
        and "task_class: code" in s1 and "units: 0" in s1
    assert "outcome: rework" in t.store["team/r/atc/usage/harvest-pr-2.md"]
    # idempotent: nothing new on re-run
    assert cli.main(["atc", "harvest", "r"], transport=t) == 0
    assert "0 shard(s) written, 2 already harvested" in capsys.readouterr().out


def test_harvest_skips_unsettled_and_unbound(capsys):
    t = FakeTransport()
    t.put("team/r/atc/bindings.json", BINDINGS)
    # unsettled: doc + verdict but no .settled marker
    t.put("team/r/review/pr-open.md", "---\nrequested_by: codex-reviewer\n---\n")
    t.put("team/r/review/pr-open/verdicts/rev.md", "---\nverdict: changes\n---\n")
    _seed_settled_review(t, "pr-orphan-author", "some-stranger")
    assert cli.main(["atc", "harvest", "r"], transport=t) == 0
    out = capsys.readouterr().out
    assert "0 shard(s) written" in out
    assert "no binding for author of pr-orphan-author" in out
    assert "team/r/atc/usage/harvest-pr-open.md" not in t.store


def test_harvest_no_bindings_is_loud_but_ok(capsys):
    t = FakeTransport()
    assert cli.main(["atc", "harvest", "r"], transport=t) == 0
    assert "no bindings declared" in capsys.readouterr().out


def test_harvested_outcomes_feed_demotion_fold():
    # three rework families for the same (model, task_class) -> demotion fires
    t = FakeTransport()
    t.put("team/r/atc/bindings.json", BINDINGS)
    for i in (1, 2, 3):
        _seed_settled_review(t, f"pr-{i}", "codex-reviewer")
        _seed_settled_review(t, f"pr-{i}-r2", "codex-reviewer")
    assert cli.main(["atc", "harvest", "r"], transport=t) == 0
    shards = cli._atc_usage_shards(t, "r")
    demo = atc.demotions(shards)
    assert ("gpt-x", "code") in demo


# --- route --for-role ---------------------------------------------------------

def test_route_for_role_filters_to_bound_account_and_reports_liveness(capsys):
    t = FakeTransport()
    t.put("team/r/atc/accounts.json", ACCOUNTS)
    t.put("team/r/atc/bindings.json", BINDINGS)
    t.put("team/r/roles/codex-reviewer.md",
          "---\ntype: Role\npolicy: shared\nsla_hours: 24\n---\n")
    assert cli.main(["roles", "claim", "r", "codex-reviewer",
                     "--agent", "codex-reviewer"], transport=t) == 0
    capsys.readouterr()
    assert cli.main(["route", "r", "--needs", "code",
                     "--for-role", "codex-reviewer"], transport=t) == 0
    out = capsys.readouterr().out
    assert "(acct-b)" in out and "(acct-a)" not in out
    assert "role codex-reviewer: HELD by codex-reviewer" in out


def test_route_for_role_vacant_flags_the_void(capsys):
    t = FakeTransport()
    t.put("team/r/atc/accounts.json", ACCOUNTS)
    t.put("team/r/atc/bindings.json", BINDINGS)
    t.put("team/r/roles/codex-reviewer.md",
          "---\ntype: Role\npolicy: shared\nsla_hours: 24\n---\n")
    assert cli.main(["route", "r", "--needs", "code",
                     "--for-role", "codex-reviewer"], transport=t) == 0
    assert "VACANT" in capsys.readouterr().out


def test_route_for_role_without_binding_exits_2(capsys):
    t = FakeTransport()
    t.put("team/r/atc/accounts.json", ACCOUNTS)
    assert cli.main(["route", "r", "--needs", "code",
                     "--for-role", "ghost-role"], transport=t) == 2
    assert "no binding for role" in capsys.readouterr().err
