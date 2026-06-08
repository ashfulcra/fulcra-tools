from __future__ import annotations

import argparse
import json
from unittest.mock import patch

import pytest

from fulcra_coord import cli, continuity


def _task() -> dict:
    return {
        "schema_version": "fulcra.coordination.task.v1",
        "id": "TASK-20260607-demo-12345678",
        "title": "Demo continuity bridge",
        "status": "active",
        "priority": "P2",
        "workstream": "openclaw:discord:main-comms",
        "owner_agent": "openclaw:discord:main-comms",
        "current_summary": "Keep enough state to resume",
        "next_action": "Pick up from latest checkpoint",
        "updated_at": "2026-06-07T12:00:00Z",
    }


def test_continuity_latest_path_is_keyed_by_shared_identity(monkeypatch) -> None:
    monkeypatch.setenv("FULCRA_COORD_REMOTE_ROOT", "/coordination-test")
    identity = continuity.identity_for_task(_task(), agent="arc")

    path = continuity.latest_remote_path(identity)

    assert path.startswith("/coordination-test/continuity/")
    assert "/arc/" in path
    assert path.endswith("/task-20260607-demo-12345678/latest.json")


def test_pause_snapshot_writes_latest_and_archived_checkpoint(capsys) -> None:
    task = _task()
    args = argparse.Namespace(
        task_id=task["id"],
        next="Resume from checkpoint",
        agent="arc",
        snapshot=True,
    )
    uploaded = []

    def upload_json(data, path, **_kwargs):
        uploaded.append((data, path))
        return True

    with patch("fulcra_coord.lifecycle._load_task", return_value=task), \
         patch("fulcra_coord.lifecycle._write_task_and_views", return_value=True), \
         patch("fulcra_coord.lifecycle.cache.write_cached_task"), \
         patch("fulcra_coord.continuity.remote.upload_json", side_effect=upload_json):
        rc = cli.cmd_pause(args)

    out = capsys.readouterr().out
    assert rc == 0
    assert "Continuity snapshot:" in out
    assert len(uploaded) == 2
    paths = [path for _data, path in uploaded]
    assert any(path.endswith("/latest.json") for path in paths)
    assert any("/checkpoints/chk-" in path for path in paths)
    checkpoint = uploaded[0][0]
    assert checkpoint["schema_version"] == continuity.SCHEMA_VERSION
    assert checkpoint["identity"]["coord_task_id"] == task["id"]
    assert checkpoint["bootstrap_primer"]["what_this_is"].startswith(
        "This is a Fulcra Continuity checkpoint"
    )
    assert checkpoint["session_context"]["overall_goal"]
    assert checkpoint["session_context"]["current_state"]
    assert checkpoint["next_actions"] == ["Resume from checkpoint"]


def test_snapshot_writes_checkpoint_without_task_transition(capsys) -> None:
    task = _task()
    args = argparse.Namespace(
        task_id=task["id"],
        reason="pre-compact",
        next=None,
        transcript_path="/tmp/session.jsonl",
        decision=[],
        open_question=[],
        artifact=[],
        memory=[],
        session_goal="",
        why_continuity="",
        session_state="",
        session_followup="",
        agent="arc",
    )
    uploaded = []

    def upload_json(data, path, **_kwargs):
        uploaded.append((data, path))
        return True

    with patch("fulcra_coord.lifecycle._load_task", return_value=task), \
         patch("fulcra_coord.lifecycle._write_task_and_views") as write_task, \
         patch("fulcra_coord.continuity.remote.upload_json", side_effect=upload_json):
        rc = cli.cmd_snapshot(args)

    out = capsys.readouterr().out
    assert rc == 0
    assert "Continuity snapshot:" in out
    write_task.assert_not_called()
    assert len(uploaded) == 2
    checkpoint = uploaded[0][0]
    assert checkpoint["source"] == "fulcra-coord:pre-compact"
    assert checkpoint["transcript_path"] == "/tmp/session.jsonl"
    assert checkpoint["bootstrap_primer"]["relationship_to_coord"].startswith(
        "fulcra-coord owns task/event coordination"
    )
    assert checkpoint["session_context"]["why_continuity_matters"]
    assert checkpoint["next_actions"] == ["Pick up from latest checkpoint"]


def test_snapshot_accepts_rich_handoff_context(capsys) -> None:
    task = _task()
    args = argparse.Namespace(
        task_id=task["id"],
        reason="manual",
        next="Continue listener fix",
        transcript_path="",
        decision=["Continuity needs a primer"],
        open_question=["How should listeners claim work?"],
        artifact=["https://example.test/pr=merged PR"],
        memory=["Remember the broader session"],
        session_goal="Build fulcra-coord",
        why_continuity="Cold agents need the story",
        session_state="Docs merged",
        session_followup="Fix listener pickup",
        agent="arc",
    )
    uploaded = []

    def upload_json(data, path, **_kwargs):
        uploaded.append((data, path))
        return True

    with patch("fulcra_coord.lifecycle._load_task", return_value=task), \
         patch("fulcra_coord.continuity.remote.upload_json", side_effect=upload_json):
        rc = cli.cmd_snapshot(args)

    assert rc == 0
    assert "Continuity snapshot:" in capsys.readouterr().out
    checkpoint = uploaded[0][0]
    assert checkpoint["decisions"] == ["Continuity needs a primer"]
    assert checkpoint["open_questions"] == ["How should listeners claim work?"]
    assert checkpoint["artifacts"] == [
        {"path": "https://example.test/pr", "note": "merged PR"}
    ]
    assert checkpoint["memory_writes"] == ["Remember the broader session"]
    assert checkpoint["session_context"] == {
        "overall_goal": "Build fulcra-coord",
        "why_continuity_matters": "Cold agents need the story",
        "current_state": "Docs merged",
        "immediate_followup": "Fix listener pickup",
    }
    assert checkpoint["next_actions"] == ["Continue listener fix"]


def test_resume_json_can_include_latest_continuity_snapshot(capsys) -> None:
    task = _task()
    checkpoint = continuity.make_checkpoint(
        task,
        agent="openclaw:discord:main-comms",
        reason="pause",
        next_actions=["Resume from checkpoint"],
    )
    args = argparse.Namespace(
        agent="openclaw:discord:main-comms",
        format="json",
        with_continuity=True,
    )

    with patch("fulcra_coord.query._load_task_summaries", return_value=[task]), \
         patch("fulcra_coord.query.identity.resolve_agent", return_value="openclaw:discord:main-comms"), \
         patch("fulcra_coord.query.identity.resolve_human", return_value="ash"), \
         patch("fulcra_coord.query.remote.download_json", return_value=None), \
         patch("fulcra_coord.continuity.remote.download_json", return_value=checkpoint):
        rc = cli.cmd_resume(args)

    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["continuity_snapshots"][0]["checkpoint_id"] == checkpoint["checkpoint_id"]
    assert payload["continuity_snapshots"][0]["next_actions"] == ["Resume from checkpoint"]


def test_continuity_schema_interop_with_standalone_package() -> None:
    """Lock the by-convention schema contract between the in-package bridge
    (``fulcra_coord.continuity``) and the standalone ``fulcra_continuity``
    package. The two deliberately do NOT import each other, so without this
    test either side could drift its SCHEMA_VERSION or top-level checkpoint
    shape silently. An audit confirmed they currently agree; this guards that.
    """
    # Skip cleanly in a bare ``pip install fulcra-coord`` where the standalone
    # package isn't present. It IS importable in the dev workspace.
    fc = pytest.importorskip("fulcra_continuity")
    from fulcra_coord import continuity as bridge

    # --- SCHEMA_VERSION parity ---
    assert bridge.SCHEMA_VERSION == fc.SCHEMA_VERSION == "fulcra.continuity.checkpoint.v1"

    # --- Top-level key-set parity ---
    sample_task = _task()
    bridge_ckpt = bridge.make_checkpoint(sample_task, agent="claude-code:Host:purpose")

    # Build the standalone checkpoint WITH a non-empty identity so its
    # ``to_dict()`` does NOT drop the ``identity`` key (it omits identity only
    # when WorkstreamIdentity.is_empty()). The bridge always emits ``identity``,
    # so populating it here keeps both key sets aligned and avoids a benign
    # false-positive on that one known asymmetry the audit found.
    standalone_dict = fc.make_checkpoint(
        task_id=sample_task["id"],
        title=sample_task["title"],
        objective=sample_task["current_summary"],
        owner_agent=sample_task["owner_agent"],
        workstream_id=sample_task["workstream"],
        agent_id="claude-code:Host:purpose",
        coord_task_id=sample_task["id"],
        coord_owner_agent=sample_task["owner_agent"],
        next_actions=[sample_task["next_action"]],
    ).to_dict()

    bridge_keys = set(bridge_ckpt)
    standalone_keys = set(standalone_dict)
    assert bridge_keys == standalone_keys, (
        "Continuity checkpoint schema drift between fulcra_coord.continuity "
        "(bridge) and the standalone fulcra_continuity package.\n"
        f"  bridge keys:     {sorted(bridge_keys)}\n"
        f"  standalone keys: {sorted(standalone_keys)}\n"
        f"  symmetric diff:  {sorted(bridge_keys ^ standalone_keys)}"
    )
