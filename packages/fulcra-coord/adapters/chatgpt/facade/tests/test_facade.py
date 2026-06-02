"""Tests for the Fulcra Coordination write facade.

These exercise the facade against the package's REAL write+rebuild path
(``cli._write_task_and_views`` -> ``views.build_all_views``) by pointing the
package's backend at a stateful local fake of ``fulcra-api file`` (see
``fake_fulcra_backend.py``). Nothing here touches live Fulcra.

Covered:
  * POST report creates a task + rebuilds views (active.json materializes it).
  * Second POST with same (agent_id, session_key) updates the SAME task — no
    duplicate is created.
  * GET status returns the created task.
  * Missing / bad token -> 401.
  * Malformed body -> 422.

Run:  pytest adapters/chatgpt/facade/tests -v
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# Make the facade package importable (adapters/chatgpt/facade) and the repo root
# (for fulcra_coord) regardless of where pytest is invoked from.
_FACADE_DIR = Path(__file__).resolve().parents[1]
_REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(_FACADE_DIR))
sys.path.insert(0, str(_REPO_ROOT))

import app as facade_app  # noqa: E402
from fulcra_coord import remote  # noqa: E402


FACADE_TOKEN = "test-token-abc123"
FAKE_BACKEND_SCRIPT = str(Path(__file__).resolve().parent / "fake_fulcra_backend.py")


@pytest.fixture
def client(monkeypatch, tmp_path):
    """A TestClient wired to an isolated fake Fulcra remote + cache.

    Each test gets:
      * a fresh fake-remote dir (FULCRA_FAKE_ROOT),
      * a fresh package cache (XDG_CACHE_HOME) so cached tasks don't leak,
      * the backend override pointed at the stateful fake script,
      * the inbound facade token configured.
    """
    fake_root = tmp_path / "remote"
    fake_root.mkdir()
    cache_home = tmp_path / "cache"
    cache_home.mkdir()

    monkeypatch.setenv("FULCRA_FAKE_ROOT", str(fake_root))
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache_home))
    monkeypatch.setenv(facade_app.FACADE_TOKEN_ENV, FACADE_TOKEN)
    # Keep remote root at the default so paths are predictable.
    monkeypatch.delenv("FULCRA_COORD_REMOTE_ROOT", raising=False)

    # Point the package's backend at our stateful fake (a real subprocess).
    backend = [sys.executable, FAKE_BACKEND_SCRIPT]
    monkeypatch.setattr(facade_app, "_BACKEND_OVERRIDE", backend)

    # The package caches its cache root at import time of some helpers; force a
    # clean read of env by clearing any module-level memoization is unnecessary
    # here because cache_root() reads the env each call.

    return TestClient(facade_app.app)


def _auth():
    return {"Authorization": f"Bearer {FACADE_TOKEN}"}


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

def test_healthz_open(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def test_report_missing_token_401(client):
    r = client.post("/coordination/report", json={
        "agent_id": "chatgpt:fulcra-coord:ash",
        "session_key": "20260601T1730Z-r7q2",
        "summary": "did a thing",
    })
    assert r.status_code == 401


def test_report_bad_token_401(client):
    r = client.post(
        "/coordination/report",
        headers={"Authorization": "Bearer wrong-token"},
        json={
            "agent_id": "chatgpt:fulcra-coord:ash",
            "session_key": "20260601T1730Z-r7q2",
            "summary": "did a thing",
        },
    )
    assert r.status_code == 401


def test_status_missing_token_401(client):
    r = client.get("/coordination/status")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# Malformed body
# ---------------------------------------------------------------------------

def test_report_malformed_body_422(client):
    # Missing required 'summary'.
    r = client.post(
        "/coordination/report",
        headers=_auth(),
        json={"agent_id": "a", "session_key": "s"},
    )
    assert r.status_code == 422


def test_report_empty_summary_422(client):
    r = client.post(
        "/coordination/report",
        headers=_auth(),
        json={"agent_id": "a", "session_key": "s", "summary": ""},
    )
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# Create + rebuild views
# ---------------------------------------------------------------------------

def test_report_creates_task_and_rebuilds_views(client, tmp_path):
    r = client.post(
        "/coordination/report",
        headers=_auth(),
        json={
            "agent_id": "chatgpt:fulcra-coord:ash",
            "session_key": "20260601T1730Z-r7q2",
            "summary": "Started the chatgpt facade",
            "next_action": "write tests",
            "workstream": "fulcra",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["created"] is True
    assert body["status"] == "active"
    task_id = body["task_id"]
    assert task_id.startswith("TASK-")

    # The task file must exist in the fake remote.
    fake_root = Path(os.environ["FULCRA_FAKE_ROOT"])
    task_file = fake_root / "coordination" / "tasks" / f"{task_id}.json"
    assert task_file.exists(), "task file not uploaded"

    # Views must have been rebuilt: active.json should contain the task.
    active = json.loads((fake_root / "coordination" / "views" / "active.json").read_text())
    ids = [t["id"] for t in active["tasks"]]
    assert task_id in ids, "task not present in rebuilt active view"

    # index.json should also exist and count it.
    index = json.loads((fake_root / "coordination" / "index.json").read_text())
    assert any(t["id"] == task_id for t in index["active"])


def test_second_report_same_session_updates_same_task(client):
    payload = {
        "agent_id": "chatgpt:fulcra-coord:ash",
        "session_key": "SESS-dedupe-1",
        "summary": "first report",
        "workstream": "fulcra",
    }
    r1 = client.post("/coordination/report", headers=_auth(), json=payload)
    assert r1.status_code == 200, r1.text
    first_id = r1.json()["task_id"]
    assert r1.json()["created"] is True

    payload2 = dict(payload)
    payload2["summary"] = "second report, same session"
    r2 = client.post("/coordination/report", headers=_auth(), json=payload2)
    assert r2.status_code == 200, r2.text
    second_id = r2.json()["task_id"]

    assert second_id == first_id, "second report should update the same task"
    assert r2.json()["created"] is False

    # The active view must contain exactly ONE task for this session (no dup).
    fake_root = Path(os.environ["FULCRA_FAKE_ROOT"])
    active = json.loads((fake_root / "coordination" / "views" / "active.json").read_text())
    matching = [t for t in active["tasks"] if t["id"] == first_id]
    assert len(matching) == 1
    # And its summary reflects the second update.
    assert matching[0]["current_summary"] == "second report, same session"


def test_status_returns_created_task(client):
    client.post(
        "/coordination/report",
        headers=_auth(),
        json={
            "agent_id": "chatgpt:fulcra-coord:ash",
            "session_key": "SESS-status-1",
            "summary": "visible in status",
            "workstream": "fulcra",
        },
    )
    r = client.get("/coordination/status", headers=_auth())
    assert r.status_code == 200, r.text
    index = r.json()
    summaries = [t["current_summary"] for t in index["active"]]
    assert "visible in status" in summaries


def test_status_filters_by_agent(client):
    client.post("/coordination/report", headers=_auth(), json={
        "agent_id": "agent-A", "session_key": "s-a", "summary": "task A", "workstream": "fulcra",
    })
    client.post("/coordination/report", headers=_auth(), json={
        "agent_id": "agent-B", "session_key": "s-b", "summary": "task B", "workstream": "fulcra",
    })
    r = client.get("/coordination/status", headers=_auth(), params={"agent_id": "agent-A"})
    assert r.status_code == 200
    index = r.json()
    agents = {t["owner_agent"] for t in index["active"]}
    assert agents == {"agent-A"}


def test_report_explicit_task_id_not_found_404(client):
    r = client.post(
        "/coordination/report",
        headers=_auth(),
        json={
            "agent_id": "chatgpt:fulcra-coord:ash",
            "session_key": "s",
            "summary": "update nonexistent",
            "task_id": "TASK-20260601-nope-deadbeef",
        },
    )
    assert r.status_code == 404


def test_report_status_transition_to_done_via_field(client):
    # Create active, then a follow-up report transitioning to waiting.
    payload = {
        "agent_id": "chatgpt:fulcra-coord:ash",
        "session_key": "SESS-transition-1",
        "summary": "working",
        "workstream": "fulcra",
    }
    r1 = client.post("/coordination/report", headers=_auth(), json=payload)
    assert r1.status_code == 200
    tid = r1.json()["task_id"]

    r2 = client.post("/coordination/report", headers=_auth(), json={
        **payload,
        "summary": "parking it",
        "next_action": "resume tomorrow",
        "status": "waiting",
    })
    assert r2.status_code == 200, r2.text
    assert r2.json()["task_id"] == tid
    assert r2.json()["status"] == "waiting"


# ---------------------------------------------------------------------------
# C2: deterministic session task id (duplicate-task race)
# ---------------------------------------------------------------------------

def test_session_task_id_is_deterministic(client):
    """Two creates for the same (agent_id, session_key) must derive the SAME
    task id, so concurrent find-or-create races target one remote path and the
    optimistic-concurrency/merge layer serializes them instead of producing two
    differently-suffixed duplicate tasks.
    """
    aid = "chatgpt:fulcra-coord:ash"
    skey = "SESS-determinism-1"
    id1 = facade_app._session_task_id(aid, skey)
    id2 = facade_app._session_task_id(aid, skey)
    assert id1 == id2, "session task id must be stable for a given (agent, session)"
    # And it must satisfy the repo's TASK-id shape.
    import re as _re
    assert _re.match(r"^TASK-\d{8}-[a-z0-9-]+-[0-9a-f]{8}$", id1), id1
    # Different session → different id.
    assert facade_app._session_task_id(aid, "OTHER-SESS") != id1
    # Different agent → different id.
    assert facade_app._session_task_id("other-agent", skey) != id1


def test_concurrent_create_same_session_yields_single_task(client):
    """Simulate the find-or-create race: call the create path twice in sequence
    against a fresh backend WITHOUT the first having stamped a discoverable tag
    in time. Because the task id is now derived deterministically from
    (agent_id, session_key), both writes target the same remote path and the
    merge/optimistic-concurrency logic collapses them into ONE task file.
    """
    payload = {
        "agent_id": "chatgpt:fulcra-coord:ash",
        "session_key": "SESS-race-1",
        "summary": "racing report",
        "workstream": "fulcra",
    }
    r1 = client.post("/coordination/report", headers=_auth(), json=payload)
    r2 = client.post(
        "/coordination/report",
        headers=_auth(),
        json={**payload, "summary": "second racer"},
    )
    assert r1.status_code == 200, r1.text
    assert r2.status_code == 200, r2.text
    assert r1.json()["task_id"] == r2.json()["task_id"]

    # Exactly one task file on the fake remote.
    fake_root = Path(os.environ["FULCRA_FAKE_ROOT"])
    tasks_dir = fake_root / "coordination" / "tasks"
    task_files = list(tasks_dir.glob("TASK-*.json"))
    assert len(task_files) == 1, f"expected one task file, got {task_files}"


# ---------------------------------------------------------------------------
# I1: status enum restricted to facade-satisfiable values
# ---------------------------------------------------------------------------

def test_status_done_rejected_at_schema_layer_422(client):
    """`done` (and other CLI-only statuses) must be rejected by the request
    schema (422) — never reach the transition engine and surface as a 400 the
    GPT can't act on. `done` requires evidence/verification the facade can't
    supply.
    """
    r = client.post(
        "/coordination/report",
        headers=_auth(),
        json={
            "agent_id": "a",
            "session_key": "s",
            "summary": "trying to finish",
            "status": "done",
        },
    )
    assert r.status_code == 422, r.text


def test_status_blocked_rejected_at_schema_layer_422(client):
    r = client.post(
        "/coordination/report",
        headers=_auth(),
        json={
            "agent_id": "a",
            "session_key": "s2",
            "summary": "blocked attempt",
            "status": "blocked",
        },
    )
    assert r.status_code == 422, r.text


def test_status_active_and_waiting_accepted(client):
    r1 = client.post(
        "/coordination/report",
        headers=_auth(),
        json={"agent_id": "a", "session_key": "s-active", "summary": "go", "status": "active", "workstream": "fulcra"},
    )
    assert r1.status_code == 200, r1.text
    assert r1.json()["status"] == "active"

    # waiting from a fresh create is a legal proposed->waiting transition.
    r2 = client.post(
        "/coordination/report",
        headers=_auth(),
        json={"agent_id": "a", "session_key": "s-wait", "summary": "park", "status": "waiting", "workstream": "fulcra"},
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["status"] == "waiting"


# ---------------------------------------------------------------------------
# I2: read path returns 503 on a Fulcra outage (not a misleading empty 200)
# ---------------------------------------------------------------------------

def test_status_503_when_backend_unreachable(client, monkeypatch):
    """A broken/unreachable backend must surface as 503, not a 200 with an
    empty index that looks like 'no work' to the GPT.
    """
    # Point the backend at a nonexistent executable -> probe FileNotFoundError.
    monkeypatch.setattr(
        facade_app, "_BACKEND_OVERRIDE", ["/nonexistent/fulcra-api-broken"]
    )
    r = client.get("/coordination/status", headers=_auth())
    assert r.status_code == 503, r.text
    assert "detail" in r.json()


def test_status_200_empty_when_backend_reachable_but_empty(client):
    """A reachable backend with no tasks yet must return 200 + empty active
    list — the genuinely-empty case must NOT be confused with an outage.
    """
    r = client.get("/coordination/status", headers=_auth())
    assert r.status_code == 200, r.text
    assert r.json()["active"] == []


# ---------------------------------------------------------------------------
# I3: length caps to prevent view bloat
# ---------------------------------------------------------------------------

def test_overlong_summary_rejected_422(client):
    r = client.post(
        "/coordination/report",
        headers=_auth(),
        json={
            "agent_id": "a",
            "session_key": "s",
            "summary": "x" * 4001,
        },
    )
    assert r.status_code == 422, r.text


def test_overlong_title_rejected_422(client):
    r = client.post(
        "/coordination/report",
        headers=_auth(),
        json={
            "agent_id": "a",
            "session_key": "s",
            "summary": "ok",
            "title": "t" * 257,
        },
    )
    assert r.status_code == 422, r.text


# ---------------------------------------------------------------------------
# M1: whitespace-only summary/title rejected
# ---------------------------------------------------------------------------

def test_whitespace_only_summary_rejected_422(client):
    r = client.post(
        "/coordination/report",
        headers=_auth(),
        json={"agent_id": "a", "session_key": "s", "summary": "   \t  "},
    )
    assert r.status_code == 422, r.text


def test_whitespace_only_title_rejected_422(client):
    r = client.post(
        "/coordination/report",
        headers=_auth(),
        json={"agent_id": "a", "session_key": "s", "summary": "ok", "title": "   "},
    )
    assert r.status_code == 422, r.text
