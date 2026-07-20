"""Tests for the answers bridge's idempotency + partial-failure contracts.

Run: uv run pytest tools/answers-bridge/ -q   (not part of the packages/ CI
sweep; exercised in PR validation). Covers the review findings:
  1. capture is idempotent by answer id (re-run/retry updates, never duplicates)
  2. promote has DURABLE dedupe across every partial-failure window:
     - degraded receipts listing -> whole pass skipped fail-closed
     - intent-write failure -> task never created
     - filed-receipt-write failure after create -> next run adopts via board,
       never re-runs `later`
     - Linear finalize failure -> retry finalizes only
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


class BusSim:
    """Simulated receipt store: bus_write/bus_read/bus_list against one dict,
    with injectable failures."""

    def __init__(self):
        self.files = {}
        self.fail_writes = False
        self.fail_list = False

    def write(self, path, content):
        if self.fail_writes:
            return False
        self.files[path] = content
        return True

    def read(self, path):
        return self.files.get(path)

    def list(self, dirpath):
        if self.fail_list:
            return None
        return {p.rsplit("/", 1)[-1] for p in self.files if p.startswith(dirpath)}

    def install(self, bridge, monkeypatch):
        monkeypatch.setattr(bridge, "bus_write", self.write)
        monkeypatch.setattr(bridge, "bus_read", self.read)
        monkeypatch.setattr(bridge, "bus_list", self.list)


def _later_runner(later_calls):
    def fake_run(argv, **kw):
        if argv[0] == "coord-engine" and argv[1] == "later":
            later_calls["n"] += 1
            return types.SimpleNamespace(returncode=0,
                                         stdout="directive promoted-task-abc123 -> @backlog\n",
                                         stderr="")
        raise AssertionError("unexpected subprocess: " + " ".join(argv[:3]))
    return fake_run


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

    args = {"q": "what is the retry contract?", "a": "receipt-based", "by": "coord-boss",
            "type": "factual"}
    assert bridge.cmd_capture(dict(args)) == 0
    assert calls == {"create": 1, "update": 0}
    assert bridge.cmd_capture(dict(args)) == 0
    assert calls == {"create": 1, "update": 1}, "re-run must update, never duplicate"


def test_promote_degraded_listing_skips_pass_fail_closed(bridge, monkeypatch):
    """A degraded receipts listing = UNKNOWN existence -> no later, no finalize."""
    sim = BusSim(); sim.fail_list = True
    sim.install(bridge, monkeypatch)
    monkeypatch.setattr(bridge, "_project_issues",
                        lambda: (_ for _ in ()).throw(AssertionError("must not list issues")))
    monkeypatch.setattr(bridge.subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no subprocess")))
    assert bridge.cmd_promote({}) == 2


def test_promote_intent_write_failure_blocks_creation(bridge, monkeypatch):
    """If the intent receipt cannot land, the task must never be created."""
    sim = BusSim(); sim.fail_writes = True
    sim.install(bridge, monkeypatch)
    monkeypatch.setattr(bridge, "_project_issues", lambda: [_card("BUS-92", "b", ("promote",))])
    monkeypatch.setattr(bridge.subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("later must not run")))
    monkeypatch.setattr(bridge, "gql",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no finalize")))
    assert bridge.cmd_promote({}) == 2


def test_promote_filed_receipt_failure_resolves_via_board_not_rerun(bridge, monkeypatch):
    """`later` succeeded but the filed-receipt write failed: the receipt stays
    pending; the NEXT run must resolve via the board and adopt — never re-run
    `later`."""
    sim = BusSim()
    sim.install(bridge, monkeypatch)
    card = _card("BUS-93", "b", ("promote",))
    monkeypatch.setattr(bridge, "_project_issues", lambda: [card])
    later_calls = {"n": 0}
    monkeypatch.setattr(bridge.subprocess, "run", _later_runner(later_calls))
    finalize = {"updates": 0}

    def fake_gql(query, variables=None):
        finalize["updates"] += 1
        return {"ok": True}

    monkeypatch.setattr(bridge, "gql", fake_gql)

    # run 1: intent lands, later runs, then ALL further writes fail -> filed
    # receipt never lands, finalize must not run for this card
    real_write = sim.write
    writes = {"n": 0}

    def write_intent_only(path, content):
        writes["n"] += 1
        if writes["n"] == 1:
            return real_write(path, content)   # intent
        return False                            # filed-receipt write fails

    monkeypatch.setattr(bridge, "bus_write", write_intent_only)
    assert bridge.cmd_promote({}) == 2
    assert later_calls["n"] == 1
    assert finalize["updates"] == 0, "must not finalize without a filed receipt"
    assert "status: pending" in next(iter(sim.files.values()))

    # run 2: writes healthy; board shows the task -> adopt, finalize, NO later
    monkeypatch.setattr(bridge, "bus_write", real_write)
    monkeypatch.setattr(bridge, "_board_has_title", lambda t: True)
    assert bridge.cmd_promote({}) == 0
    assert later_calls["n"] == 1, "retry must adopt via board, never re-run later"
    assert finalize["updates"] == 2, "retry must complete the finalize"
    assert "status: filed" in sim.files[
        f"team/{bridge.TEAM}/answers/_promotions/BUS-93.md"]


def test_promote_pending_with_board_unavailable_fails_closed(bridge, monkeypatch):
    """pending receipt + board fold unavailable -> skip card, no later, exit 2."""
    sim = BusSim()
    sim.files["team/fulcra/answers/_promotions/BUS-94.md"] = \
        "---\ntype: PromotionReceipt\ncard: BUS-94\nstatus: pending\nslug: \n---\n"
    sim.install(bridge, monkeypatch)
    monkeypatch.setattr(bridge, "_project_issues", lambda: [_card("BUS-94", "b", ("promote",))])
    monkeypatch.setattr(bridge, "_board_has_title", lambda t: None)
    monkeypatch.setattr(bridge.subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("later must not run")))
    monkeypatch.setattr(bridge, "gql",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no finalize")))
    assert bridge.cmd_promote({}) == 2


def test_board_degraded_fold_is_unknown_not_absence(bridge, monkeypatch):
    """An rc-0 board fold carrying a degraded marker must return None (absence
    unproven), never False — presence stays provable from the same fold."""
    def board_json(payload):
        def fake_run(argv, **kw):
            assert argv[:2] == ["coord-engine", "board"]
            return types.SimpleNamespace(returncode=0, stdout=payload, stderr="")
        return fake_run

    import json as _json
    # degraded marker row + title absent -> None
    monkeypatch.setattr(bridge.subprocess, "run", board_json(_json.dumps(
        {"active": [{"type": "read-degraded", "reason": "summaries unreadable"}]})))
    assert bridge._board_has_title("the promoted question") is None
    # degraded lane key + title absent -> None
    monkeypatch.setattr(bridge.subprocess, "run", board_json(_json.dumps(
        {"read-degraded": [{"type": "read-degraded", "reason": "x"}], "active": []})))
    assert bridge._board_has_title("the promoted question") is None
    # degraded marker BUT title present -> True (presence provable from partial fold)
    monkeypatch.setattr(bridge.subprocess, "run", board_json(_json.dumps(
        {"active": [{"type": "review-fold-degraded", "scanned": 1, "total": 9},
                    {"title": "the promoted question", "id": "x-1"}]})))
    assert bridge._board_has_title("the promoted question") is True
    # clean fold, absent -> False (the only provable absence)
    monkeypatch.setattr(bridge.subprocess, "run", board_json(_json.dumps(
        {"active": [{"title": "other", "id": "y"}], "waiting": []})))
    assert bridge._board_has_title("the promoted question") is False


def test_promote_adoption_receipt_failure_blocks_finalize(bridge, monkeypatch):
    """Board resolution proves the task exists, but the adopted (filed) receipt
    cannot be persisted -> the finalize must NOT run (filed receipt is its
    precondition), pass exits degraded."""
    sim = BusSim()
    sim.files["team/fulcra/answers/_promotions/BUS-95.md"] = \
        "---\ntype: PromotionReceipt\ncard: BUS-95\nstatus: pending\nslug: \n---\n"
    sim.install(bridge, monkeypatch)
    monkeypatch.setattr(bridge, "bus_write", lambda p, c: False)
    monkeypatch.setattr(bridge, "_project_issues", lambda: [_card("BUS-95", "b", ("promote",))])
    monkeypatch.setattr(bridge, "_board_has_title", lambda t: True)
    monkeypatch.setattr(bridge.subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("later must not run")))
    monkeypatch.setattr(bridge, "gql",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no finalize")))
    assert bridge.cmd_promote({}) == 2


def test_promote_finalize_failure_never_double_files(bridge, monkeypatch):
    """Linear finalize fails after a clean file -> retry finalizes only."""
    sim = BusSim()
    sim.install(bridge, monkeypatch)
    card = _card("BUS-91", "b", ("promote",))
    monkeypatch.setattr(bridge, "_project_issues", lambda: [card])
    later_calls = {"n": 0}
    monkeypatch.setattr(bridge.subprocess, "run", _later_runner(later_calls))
    finalize = {"fail": True, "updates": 0}

    def fake_gql(query, variables=None):
        if finalize["fail"]:
            raise RuntimeError("linear 500")
        finalize["updates"] += 1
        return {"ok": True}

    monkeypatch.setattr(bridge, "gql", fake_gql)

    assert bridge.cmd_promote({}) == 2
    assert later_calls["n"] == 1
    assert "status: filed" in sim.files[
        f"team/{bridge.TEAM}/answers/_promotions/BUS-91.md"]

    finalize["fail"] = False
    assert bridge.cmd_promote({}) == 0
    assert later_calls["n"] == 1, "retry must not create a second bus task"
    assert finalize["updates"] == 2
