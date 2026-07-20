"""Tests for the answers bridge's idempotency + partial-failure contracts.

Run: uv run pytest tools/answers-bridge/ -q   (not part of the packages/ CI
sweep; exercised in PR validation).

Promote leans on the ENGINE's delivery contract for dedupe (directive path =
payload hash over title/summary/next/assignee, never time; re-delivery prints
`already delivered`, rc 0 — cli._directive_payload/_write_directive). The
simulated `later` below enforces exactly that contract, so these tests pin:
  1. capture is idempotent by answer id (re-run/retry updates, never duplicates)
  2. a Linear finalize failure retries the WHOLE idempotent sequence and the
     store ends with exactly ONE task (second run dedupes)
  3. a degraded `later` skips the card fail-closed (no finalize, exit 2)
  4. both engine success shapes parse to the same slug
"""
import importlib.util
import os
import types

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))


@pytest.fixture()
def bridge(tmp_path, monkeypatch):
    env = tmp_path / "linear.env"
    env.write_text("LINEAR_API_KEY=test-key\nLINEAR_TEAM_KEY=BUS\nLINEAR_TEAM_ID=t-1\n")
    monkeypatch.setenv("ANSWERS_LINEAR_ENV", str(env))
    spec = importlib.util.spec_from_file_location(
        "answers_bridge_under_test", os.path.join(HERE, "answers_bridge.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _card(identifier, description, labels=()):
    return {"id": f"uuid-{identifier}", "identifier": identifier,
            "title": "the promoted question", "url": f"https://linear/x/{identifier}",
            "description": description,
            "state": {"name": "Todo"},
            "labels": {"nodes": [{"id": f"lid-{n}", "name": n} for n in labels]}}


class EngineSim:
    """Simulates `coord-engine later` per the engine's delivery contract:
    identical payload (argv) -> same slug, `already delivered`, rc 0."""

    def __init__(self):
        self.store = {}   # payload-key -> slug
        self.fail = False

    def run(self, argv, **kw):
        assert argv[0] == "coord-engine" and argv[1] == "later", argv
        if self.fail:
            return types.SimpleNamespace(returncode=1, stdout="", stderr="transport down")
        key = "\x00".join(argv[2:])
        if key in self.store:
            return types.SimpleNamespace(
                returncode=0, stdout=f"directive {self.store[key]} already delivered\n",
                stderr="")
        slug = f"task-{len(self.store) + 1}-abc123"
        self.store[key] = slug
        return types.SimpleNamespace(
            returncode=0, stdout=f"directive {slug} -> @backlog\n", stderr="")


def test_capture_is_idempotent_by_answer_id(bridge, monkeypatch):
    """Second capture of the same Q/by (same aid) must UPDATE, not create."""
    calls = {"create": 0, "update": 0}
    existing = []

    def fake_gql(query, variables=None):
        if "issueCreate" in query:
            calls["create"] += 1
            existing.append(_card("BUS-90", variables["in"]["description"]))
            return {"issueCreate": {"issue": {"id": "uuid-BUS-90", "identifier": "BUS-90",
                                              "url": "https://linear/x/BUS-90"}}}
        if "issueUpdate" in query:
            calls["update"] += 1
            return {"issueUpdate": {"success": True}}
        return {"issues": {"nodes": list(existing),
                           "pageInfo": {"hasNextPage": False, "endCursor": None}}}

    monkeypatch.setattr(bridge, "gql", fake_gql)
    monkeypatch.setattr(bridge, "bus_write", lambda p, c: True)

    args = {"q": "what is the retry contract?", "a": "engine dedupe", "by": "coord-boss",
            "type": "factual"}
    assert bridge.cmd_capture(dict(args)) == 0
    assert calls == {"create": 1, "update": 0}
    assert bridge.cmd_capture(dict(args)) == 0
    assert calls == {"create": 1, "update": 1}, "re-run must update, never duplicate"


def test_promote_finalize_failure_retry_dedupes_to_one_task(bridge, monkeypatch):
    """Finalize fails after the task files; the retry re-runs `later`, the
    engine dedupes to the SAME slug, exactly one task exists, finalize completes."""
    engine = EngineSim()
    card = _card("BUS-91", "b", ("promote",))
    monkeypatch.setattr(bridge, "_project_issues", lambda: [card])
    monkeypatch.setattr(bridge.subprocess, "run", engine.run)
    finalize = {"fail": True, "updates": 0, "comments": []}

    def fake_gql(query, variables=None):
        if finalize["fail"]:
            raise RuntimeError("linear 500")
        finalize["updates"] += 1
        if "commentCreate" in query:
            finalize["comments"].append(variables["in"]["body"])
        return {"ok": True}

    monkeypatch.setattr(bridge, "gql", fake_gql)

    assert bridge.cmd_promote({}) == 2          # filed, finalize blew up
    assert len(engine.store) == 1

    finalize["fail"] = False
    assert bridge.cmd_promote({}) == 0          # retry: dedupe + finalize
    assert len(engine.store) == 1, "retry must dedupe at the store, never duplicate"
    assert finalize["updates"] == 2
    # the deduped slug (parsed from `already delivered`) reaches the comment
    assert "task-1-abc123" in finalize["comments"][0]


def test_promote_degraded_later_skips_card_fail_closed(bridge, monkeypatch):
    """`later` rc!=0 -> no finalize for that card, pass exits 2."""
    engine = EngineSim(); engine.fail = True
    monkeypatch.setattr(bridge, "_project_issues", lambda: [_card("BUS-92", "b", ("promote",))])
    monkeypatch.setattr(bridge.subprocess, "run", engine.run)
    monkeypatch.setattr(bridge, "gql",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no finalize")))
    assert bridge.cmd_promote({}) == 2
    assert engine.store == {}


def test_promote_parses_both_engine_success_shapes(bridge):
    """`-> @backlog` and `already delivered` must yield the same slug."""
    import re
    pat = r"^directive\s+(\S+?)(?:\s*->|\s+already delivered)"
    m1 = re.search(pat, "directive my-task-1a2b3c4d -> @backlog\n", re.M)
    m2 = re.search(pat, "directive my-task-1a2b3c4d already delivered\n", re.M)
    assert m1 and m2 and m1.group(1) == m2.group(1) == "my-task-1a2b3c4d"
