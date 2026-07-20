"""Tests for the answers bridge's idempotency + partial-failure contracts.

Run: uv run pytest tools/answers-bridge/ -q   (not part of the packages/ CI
sweep; exercised in PR validation). Covers the two review findings:
  1. capture is idempotent by answer id (re-run/retry updates, never duplicates)
  2. promote survives a Linear-finalize failure without double-filing the task
"""
import importlib.util
import os
import sys
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
            "title": "t", "url": f"https://linear/x/{identifier}",
            "description": description,
            "state": {"name": "Todo"},
            "labels": {"nodes": [{"id": f"lid-{n}", "name": n} for n in labels]}}


def test_capture_is_idempotent_by_answer_id(bridge, monkeypatch):
    """Second capture of the same Q/by (same aid) must UPDATE, not create."""
    calls = {"create": 0, "update": 0}
    existing = []

    def fake_gql(query, variables=None):
        if "issueCreate" in query:
            calls["create"] += 1
            aid_body = variables["in"]["description"]
            existing.append(_card("BUS-90", aid_body))
            return {"issueCreate": {"issue": {"id": "uuid-BUS-90", "identifier": "BUS-90",
                                              "url": "https://linear/x/BUS-90"}}}
        if "issueUpdate" in query:
            calls["update"] += 1
            return {"issueUpdate": {"success": True}}
        # issues listing
        return {"issues": {"nodes": list(existing),
                           "pageInfo": {"hasNextPage": False, "endCursor": None}}}

    monkeypatch.setattr(bridge, "gql", fake_gql)
    monkeypatch.setattr(bridge, "bus_write", lambda p, c: True)

    args = {"q": "what is the retry contract?", "a": "receipt-based", "by": "coord-boss",
            "type": "factual"}
    assert bridge.cmd_capture(dict(args)) == 0
    assert calls == {"create": 1, "update": 0}
    # retry / re-run: the existing card is found via the shard path in its body
    assert bridge.cmd_capture(dict(args)) == 0
    assert calls == {"create": 1, "update": 1}, "re-run must update, never duplicate"


def test_promote_finalize_failure_never_double_files(bridge, monkeypatch):
    """Linear finalize fails after the bus task exists -> receipt retained; the
    retry run must NOT invoke `coord-engine later` again, and must finalize."""
    card = _card("BUS-91", "body", labels=("promote",))
    receipts = {}
    later_calls = {"n": 0}
    finalize = {"fail": True, "updates": 0}

    def fake_issues():
        return [card]

    def fake_gql(query, variables=None):
        if "issueUpdate" in query or "commentCreate" in query:
            if finalize["fail"]:
                raise RuntimeError("linear 500")
            finalize["updates"] += 1
            return {"ok": True}
        raise AssertionError("unexpected gql in promote test: " + query[:40])

    def fake_run(argv, **kw):
        if argv[0] == "coord-engine" and argv[1] == "later":
            later_calls["n"] += 1
            return types.SimpleNamespace(returncode=0,
                                         stdout="directive promoted-task-abc123 -> @backlog\n",
                                         stderr="")
        raise AssertionError("unexpected subprocess: " + " ".join(argv[:3]))

    monkeypatch.setattr(bridge, "_project_issues", fake_issues)
    monkeypatch.setattr(bridge, "gql", fake_gql)
    monkeypatch.setattr(bridge.subprocess, "run", fake_run)
    monkeypatch.setattr(bridge, "bus_write", lambda p, c: receipts.__setitem__(p, c) or True)
    monkeypatch.setattr(bridge, "bus_read", lambda p: receipts.get(p))

    # run 1: task filed, receipt written, finalize blows up -> degraded exit
    assert bridge.cmd_promote({}) == 2
    assert later_calls["n"] == 1
    assert len(receipts) == 1 and "slug: promoted-task-abc123" in next(iter(receipts.values()))

    # run 2 (Linear healthy again): must reuse the receipt, not re-file
    finalize["fail"] = False
    assert bridge.cmd_promote({}) == 0
    assert later_calls["n"] == 1, "retry must not create a second bus task"
    assert finalize["updates"] == 2, "retry must complete the finalize (update+comment)"


def test_promote_receipt_write_failure_blocks_finalize(bridge, monkeypatch):
    """If the receipt itself cannot be written after the task was filed, promote
    must fail loud and NOT finalize (finalizing would mask a double-file risk)."""
    card = _card("BUS-92", "body", labels=("promote",))
    monkeypatch.setattr(bridge, "_project_issues", lambda: [card])
    monkeypatch.setattr(bridge, "bus_read", lambda p: None)
    monkeypatch.setattr(bridge, "bus_write", lambda p, c: False)
    monkeypatch.setattr(bridge.subprocess, "run", lambda argv, **kw: types.SimpleNamespace(
        returncode=0, stdout="directive x-1 -> @backlog\n", stderr=""))

    def fail_gql(query, variables=None):
        raise AssertionError("finalize must not run when the receipt failed")

    monkeypatch.setattr(bridge, "gql", fail_gql)
    assert bridge.cmd_promote({}) == 2
