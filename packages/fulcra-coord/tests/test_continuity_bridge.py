from __future__ import annotations

import argparse
import json
from unittest.mock import patch

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
    assert checkpoint["next_actions"] == ["Resume from checkpoint"]


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
