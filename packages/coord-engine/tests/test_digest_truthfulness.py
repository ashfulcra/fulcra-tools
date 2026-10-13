"""A persisted digest must carry the same read uncertainty as its JSON result."""

import json
from datetime import datetime, timezone

import pytest

from coord_engine import cli, tasks
from coord_engine.transport import TransportError
from coord_engine_test_helpers import FakeTransport

NOW = datetime(2026, 10, 6, 16, 0, tzinfo=timezone.utc)
SOURCE_AT = "2026-10-05T16:00:00Z"


@pytest.fixture(autouse=True)
def _pin_clock(monkeypatch):
    monkeypatch.setattr(cli, "_now", lambda: NOW)
    monkeypatch.setattr(cli, "_host", lambda: "synthetic-host")


class OverlayUnavailable(FakeTransport):
    def data_updates(self, since, *, deadline=None):
        return None

    def list_dir(self, prefix):
        if prefix == "team/r/task/":
            raise TransportError("task source unavailable")
        return super().list_dir(prefix)


def _last_known_transport():
    t = OverlayUnavailable()
    t.put("team/r/_coord/summaries.json", json.dumps({
        "generated_at": SOURCE_AT,
        "rows": [{"name": "review", "title": "Review retained work",
                  "status": "blocked", "priority": "P1", "assignee": "user",
                  "tags": [], "timestamp": SOURCE_AT}],
    }))
    return t


def _capture_emit(monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "_emit_digest_timeline",
                        lambda **kw: (calls.append(kw), True)[1])
    return calls


@pytest.mark.parametrize("json_mode", [False, True])
def test_degraded_warning_is_in_stored_and_timeline_body(monkeypatch, capsys, json_mode):
    # Dropping the read marker before rendering must fail on the actual outputs,
    # even when JSON stdout itself still contains a marker.
    t = _last_known_transport()
    calls = _capture_emit(monkeypatch)
    argv = ["digest", "r", "--human", "user", "--emit-timeline"] + (["--json"] if json_mode else [])
    assert cli.main(argv, transport=t) == 0
    captured = capsys.readouterr()
    body = t.store["team/r/_coord/digests/2026-10-06-evening.md"]
    assert "UNKNOWN" in body
    assert "task-dir overlay unreadable" in body
    assert "last-known/unverified" in body
    assert "Review retained work" in body
    assert SOURCE_AT in body and "24.00h" in body
    assert calls[0]["note"] == body
    if json_mode:
        payload = json.loads(captured.out)
        assert payload["read-degraded"]["reason"] == "task-dir overlay unreadable"
        assert payload["source"]["at"] == SOURCE_AT
    else:
        assert body == captured.out


def test_unreadable_index_cannot_render_presence_as_no_open_work(monkeypatch, capsys):
    t = FakeTransport()
    t.put("team/r/_coord/summaries.json", "invalid JSON")
    t.put(f"team/r/presence/{tasks.agent_key('alice')}.md",
          "---\ntype: Presence\nagent: alice\ntimestamp: 2026-10-06T15:50:00Z\n---\n")
    calls = _capture_emit(monkeypatch)
    assert cli.main(["digest", "r", "--emit-timeline"], transport=t) == 0
    body = capsys.readouterr().out
    assert "UNKNOWN" in body and "summaries index unreadable" in body
    assert "no open work" not in body
    assert "open work unknown" in body
    assert "Source: unknown" in body
    assert calls[0]["note"] == body


def test_existing_clean_window_body_is_preserved_but_emitted_note_warns(monkeypatch, capsys):
    t = _last_known_transport()
    path = "team/r/_coord/digests/2026-10-06-evening.md"
    historical = "# Digest — 2026-10-06T12:00:00Z\n- alice — no open work\n"
    t.put(path, historical)
    calls = _capture_emit(monkeypatch)
    assert cli.main(["digest", "r", "--emit-timeline"], transport=t) == 0
    assert t.store[path] == historical
    assert calls
    assert "UNKNOWN" in calls[0]["note"]
    assert "task-dir overlay unreadable" in calls[0]["note"]
    assert "last-known/unverified" in calls[0]["note"]
    assert "no open work" not in calls[0]["note"]
    assert calls[0]["note"] != historical


def test_degraded_emit_failure_retries_with_warning_and_same_id(monkeypatch, capsys):
    t = _last_known_transport()
    calls = []
    accepted = {"value": False}
    monkeypatch.setattr(cli, "_emit_digest_timeline",
                        lambda **kw: (calls.append(kw), accepted["value"])[1])
    assert cli.main(["digest", "r", "--emit-timeline"], transport=t) == 0
    path = "team/r/_coord/digests/2026-10-06-evening.md"
    historical = t.store[path]
    assert not any(p.endswith(".emitted") for p in t.store)
    accepted["value"] = True
    monkeypatch.setattr(cli, "_now", lambda: datetime(2026, 10, 6, 16, 1, tzinfo=timezone.utc))
    assert cli.main(["digest", "r", "--emit-timeline"], transport=t) == 0
    assert len(calls) == 2
    assert calls[0]["record_id"] == calls[1]["record_id"]
    assert "UNKNOWN" in calls[1]["note"] and "last-known/unverified" in calls[1]["note"]
    assert SOURCE_AT in calls[1]["note"]
    assert t.store[path] == historical
    assert path.replace(".md", ".emitted") in t.store
    cli.main(["digest", "r", "--emit-timeline"], transport=t)
    assert len(calls) == 2  # confirmed emission remains untouched


@pytest.mark.parametrize("suffix", [".md", ".emitted"])
def test_unknown_existing_marker_read_preserves_history_and_withholds_emit(monkeypatch, capsys, suffix):
    path = f"team/r/_coord/digests/2026-10-06-evening{suffix}"

    class MarkerUnreadable(FakeTransport):
        def read(self, name):
            return None if name == path else super().read(name)

        def read_classified(self, name, *, deadline=None):
            if name == path:
                return None, "error"
            return super().read_classified(name, deadline=deadline)

    t = MarkerUnreadable()
    historical = "previously persisted snapshot or confirmed emission"
    t.put(path, historical)
    calls = _capture_emit(monkeypatch)
    assert cli.main(["digest", "r", "--emit-timeline"], transport=t) == 3
    assert t.store[path] == historical
    assert calls == []
    assert "withheld" in capsys.readouterr().err


@pytest.mark.parametrize("source_at", [None, "invalid-time", 123])
def test_missing_or_invalid_source_time_is_unknown_not_digest_clock(capsys, source_at):
    t = FakeTransport()
    t.put("team/r/_coord/summaries.json", json.dumps({
        "generated_at": source_at, "rows": [],
    }))
    assert cli.main(["digest", "r", "--json"], transport=t) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["source"] == {"kind": "summaries", "at": None, "age_hours": None}


def test_failed_digest_write_is_not_claimed_or_emitted(monkeypatch, capsys):
    class WriteRefused(FakeTransport):
        def write(self, path, content):
            return False

    calls = _capture_emit(monkeypatch)
    t = WriteRefused()
    assert cli.main(["digest", "r", "--emit-timeline"], transport=t) == 3
    err = capsys.readouterr().err
    assert "stored digest ->" not in err
    assert calls == []


def test_unverified_digest_readback_is_not_emitted(monkeypatch, capsys):
    class WriteNotPersisted(FakeTransport):
        def write(self, path, content):
            return True  # admission without stored bytes

    calls = _capture_emit(monkeypatch)
    assert cli.main(["digest", "r", "--emit-timeline"], transport=WriteNotPersisted()) == 3
    assert "stored digest ->" not in capsys.readouterr().err
    assert calls == []


def test_refused_emit_marker_is_not_reported_as_confirmed(monkeypatch, capsys):
    class MarkerRefused(FakeTransport):
        def write(self, path, content):
            return False if path.endswith(".emitted") else super().write(path, content)

    calls = _capture_emit(monkeypatch)
    assert cli.main(["digest", "r", "--emit-timeline"], transport=MarkerRefused()) == 3
    err = capsys.readouterr().err
    assert len(calls) == 1  # native admission is retained, not confirmation
    assert "confirmation unverified" in err
    assert "emitted digest timeline moment" not in err


def test_validated_digest_source_is_coverage_horizon_not_generated_now(monkeypatch, capsys):
    from coord_engine import public_read
    from coord_engine.outcome import OutcomeState

    # Exercise the real validated handler seam without a second summaries read.
    authority = public_read.PublicReadResult(
        OutcomeState.CLEAR, (), {"tasks": {"rows": []}, "presence": {"records": []}},
        "2026-10-06T15:59:30Z", "synthetic-generation", SOURCE_AT,
    )
    token = cli._PUBLIC_READ_CONTEXT.set(authority)
    try:
        assert cli.main(["digest", "r", "--json"], transport=FakeTransport()) == 0
    finally:
        cli._PUBLIC_READ_CONTEXT.reset(token)
    source = json.loads(capsys.readouterr().out)["source"]
    assert source == {"kind": "coverage-horizon", "at": "2026-10-06T15:59:30Z",
                      "age_hours": 30 / 3600}
