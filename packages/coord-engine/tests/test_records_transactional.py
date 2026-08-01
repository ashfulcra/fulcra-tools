"""Bus-v3 cursor v2: stage/process/commit and CAS race acceptance gates."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from coord_engine import cli, obligations as obligations_mod, records
from coord_engine_test_helpers import FakeTransport


NOW = datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc)


def _config(**overrides):
    doc = {
        "data_type": "MomentAnnotation/x",
        "api_version": "v1alpha1",
        "protocol_version": 1,
        "cursor_schema_version": 2,
        "minimum_reader_version": "1.9.0",
        "minimum_writer_version": "1.9.0",
        "cursor_generation": 3,
        "cursor_activated_at": "2026-07-29T11:00:00Z",
    }
    doc.update(overrides)
    return doc


def _event(rid="r1", slug="job"):
    return {
        "id": rid,
        "recorded_at": "2026-07-29T11:30:00Z",
        "sources": ["boss"],
        "note": records.build_payload(
            to="amy", kind="directive", priority="P0", slug=slug),
    }


class CasTransport(FakeTransport):
    def __init__(self, window=None):
        super().__init__()
        self.window = [] if window is None else window
        self.queries = []
        self.cas_calls = []
        self.reject_next_cas = False
        self.apply_but_report_failure = False

    def read_classified(self, path):
        value = self.read(path)
        return (value, "ok") if value is not None else (None, "absent")

    def records(self, data_type, since, until):
        self.queries.append((data_type, since, until))
        return self.window

    def compare_and_swap(self, path, expected_raw, new_raw):
        self.cas_calls.append((path, expected_raw, new_raw))
        if self.reject_next_cas:
            self.reject_next_cas = False
            return False
        if self.read(path) != expected_raw:
            return False
        self.put(path, new_raw)
        if self.apply_but_report_failure:
            self.apply_but_report_failure = False
            return False
        return True


def _setup(t, config=None):
    t.put(records.config_path("r"), json.dumps(config or _config()))
    return t


def _json_lines(capsys):
    return [json.loads(line) for line in capsys.readouterr().out.splitlines()]


def _stage(monkeypatch, capsys, t):
    monkeypatch.setattr(cli, "_now", lambda: NOW)
    assert cli.main(
        ["queue", "r", "--agent", "amy", "--json"], transport=t) == 0
    rows = _json_lines(capsys)
    assert rows[-1]["type"] == "queue-delivery"
    return rows[-1]["token"], rows


@pytest.mark.parametrize("state", ["UNKNOWN", "INVALID"])
def test_empty_v2_stage_reports_obligation_fold_without_changing_rc(
        monkeypatch, capsys, state):
    t = _setup(CasTransport([]))
    monkeypatch.setattr(cli, "_now", lambda: NOW)

    def fake_fold(*args, **kwargs):
        return obligations_mod.ObligationResult(
            state=obligations_mod.ObligationState[state],
            degraded=["reviews"] if state == "UNKNOWN" else [],
            malformed=[] if state == "UNKNOWN" else ["tasks"],
        )

    monkeypatch.setattr(obligations_mod, "fold", fake_fold)
    rc = cli.main(
        ["queue", "r", "--agent", "amy", "--json"], transport=t)
    row = _json_lines(capsys)[-1]

    # 2026-08-01 rc split: fold degradation is a report, not a failure.
    assert rc == 0
    assert row["type"] == "queue-delivery"
    assert row["event_count"] == 0
    assert row["obligations"]["state"] == state


def test_empty_v2_stage_no_obligations_skips_fold_and_keeps_shape(
        monkeypatch, capsys):
    t = _setup(CasTransport([]))
    monkeypatch.setattr(cli, "_now", lambda: NOW)
    monkeypatch.setattr(
        obligations_mod, "fold",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("--no-obligations must skip the fold")),
    )

    rc = cli.main([
        "queue", "r", "--agent", "amy", "--json", "--no-obligations",
    ], transport=t)
    row = _json_lines(capsys)[-1]

    assert rc == 0
    assert row["type"] == "queue-delivery"
    assert "obligations" not in row


def _commit(capsys, t, token, *, include_results=True):
    args = [
        "queue", "commit", "r", "--agent", "amy", "--token", token, "--json",
    ]
    if include_results:
        raw = t.read(records.v2_cursor_path("r", "amy", 3))
        state = json.loads(raw) if raw else {}
        pending = state.get("pending")
        if isinstance(pending, dict):
            for event in pending["events"]:
                args.extend(["--result", f"{event['record_id']}=completed"])
    rc = cli.main(args, transport=t)
    rows = _json_lines(capsys) if rc == 0 else []
    return rc, rows


def test_pre_upgrade_legacy_state_seeds_v2_without_mutating_legacy(
        monkeypatch, capsys):
    legacy_path = records.cursor_path("r", "amy")
    legacy_raw = (
        '{"v":1,"last_read":"2026-07-29T10:00:00Z","seen_ids":["old"]}'
    )
    t = _setup(CasTransport([_event()]))
    t.put(legacy_path, legacy_raw)

    token, _rows = _stage(monkeypatch, capsys, t)
    rc, rows = _commit(capsys, t, token)

    assert rc == 0 and rows[-1]["outcome"] == "committed"
    assert t.read(legacy_path) == legacy_raw
    v2 = json.loads(t.read(records.v2_cursor_path("r", "amy", 3)))
    assert v2["committed"]["last_read"] == "2026-07-29T12:00:00Z"
    assert set(v2["committed"]["seen_ids"]) == {"old", "r1"}


def test_unreadable_legacy_cursor_blocks_first_v2_stage(monkeypatch, capsys):
    class LegacyReadFails(CasTransport):
        def read_classified(self, path):
            if path == records.cursor_path("r", "amy"):
                return None, "error"
            return super().read_classified(path)

    t = _setup(LegacyReadFails([_event()]))
    monkeypatch.setattr(cli, "_now", lambda: NOW)

    assert cli.main(["queue", "r", "--agent", "amy"], transport=t) == 3
    assert "legacy cursor is error" in capsys.readouterr().err
    assert t.queries == []
    assert t.read(records.v2_cursor_path("r", "amy", 3)) is None


def test_processing_failure_and_interruption_replay_same_token_and_batch(
        monkeypatch, capsys):
    t = _setup(CasTransport([_event()]))
    token, first = _stage(monkeypatch, capsys, t)

    # Simulate crash/processing failure: no commit command is run.
    assert cli.main(
        ["queue", "r", "--agent", "amy", "--json"], transport=t) == 0
    replay = _json_lines(capsys)

    assert replay[-1]["outcome"] == "replayed"
    assert replay[-1]["token"] == token
    assert replay[:-1] == first[:-1]
    assert len(t.queries) == 1, "pending batch replays without a new window"


def test_abandoned_batch_replays_after_time_has_passed(monkeypatch, capsys):
    t = _setup(CasTransport([_event()]))
    token, _rows = _stage(monkeypatch, capsys, t)
    monkeypatch.setattr(
        cli, "_now",
        lambda: datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc))

    assert cli.main(
        ["queue", "r", "--agent", "amy", "--json"], transport=t) == 0
    replay = _json_lines(capsys)

    assert replay[-1]["token"] == token
    assert replay[-1]["outcome"] == "replayed"
    assert len(t.queries) == 1


def test_acknowledgement_failure_keeps_pending_batch(monkeypatch, capsys):
    t = _setup(CasTransport([_event()]))
    token, _rows = _stage(monkeypatch, capsys, t)
    t.reject_next_cas = True

    rc, _rows = _commit(capsys, t, token)

    assert rc == 3
    state = json.loads(t.read(records.v2_cursor_path("r", "amy", 3)))
    assert state["pending"]["token"] == token
    assert state["revision"] == 0


def test_commit_refuses_unclassified_events(monkeypatch, capsys):
    t = _setup(CasTransport([_event()]))
    token, _rows = _stage(monkeypatch, capsys, t)

    rc, _rows = _commit(capsys, t, token, include_results=False)

    assert rc == 2
    assert "every staged event requires exactly one --result" in \
        capsys.readouterr().err
    state = json.loads(t.read(records.v2_cursor_path("r", "amy", 3)))
    assert state["pending"]["token"] == token
    assert state["revision"] == 0


def test_lost_commit_response_is_verified_as_idempotent_success(
        monkeypatch, capsys):
    t = _setup(CasTransport([_event()]))
    token, _rows = _stage(monkeypatch, capsys, t)
    t.apply_but_report_failure = True

    rc, rows = _commit(capsys, t, token)

    assert rc == 0
    assert rows[-1]["outcome"] == "idempotent"


def test_concurrent_stage_cas_has_one_winner_and_one_loser():
    t = CasTransport()
    c1 = records.initial_v2_cursor(3)
    c2 = records.initial_v2_cursor(3)
    kwargs = {
        "staged_at": "2026-07-29T12:00:00Z",
        "window_start": "2026-07-29T11:00:00Z",
        "window_end": "2026-07-29T12:00:00Z",
        "events": [],
    }

    first = records.stage_v2_delivery(
        t, "r", "amy", 3, cursor=c1, expected_raw=None, **kwargs)
    second = records.stage_v2_delivery(
        t, "r", "amy", 3, cursor=c2, expected_raw=None, **kwargs)

    assert first["status"] == "staged"
    assert second["status"] == "lost"


def test_concurrent_wake_loser_replays_verified_winner(monkeypatch, capsys):
    class RivalWins(CasTransport):
        def compare_and_swap(self, path, expected_raw, new_raw):
            proposed = json.loads(new_raw)
            rival = json.loads(new_raw)
            rival["pending"]["token"] = "rival-token"
            rival["pending"]["events"] = [
                records.events_for([_event("r2", "rival-job")], "amy")[0]
            ]
            self.put(path, json.dumps(rival, sort_keys=True, separators=(",", ":")))
            self.cas_calls.append((path, expected_raw, new_raw))
            assert proposed["pending"]["token"] != "rival-token"
            return False

    t = _setup(RivalWins([_event()]))
    monkeypatch.setattr(cli, "_now", lambda: NOW)

    assert cli.main(
        ["queue", "r", "--agent", "amy", "--json"], transport=t) == 0
    rows = _json_lines(capsys)

    assert rows[0]["slug"] == "rival-job"
    assert rows[-1]["token"] == "rival-token"
    assert rows[-1]["outcome"] == "replayed"


def test_stale_token_cannot_advance_after_commit(monkeypatch, capsys):
    t = _setup(CasTransport([_event()]))
    token, _rows = _stage(monkeypatch, capsys, t)
    assert _commit(capsys, t, token)[0] == 0

    rc, _rows = _commit(capsys, t, "not-the-delivery-token")

    assert rc == 3
    state = json.loads(t.read(records.v2_cursor_path("r", "amy", 3)))
    assert state["revision"] == 1
    assert state["committed"]["last_token"] == token


def test_commit_retry_remains_idempotent(monkeypatch, capsys):
    t = _setup(CasTransport([_event()]))
    token, _rows = _stage(monkeypatch, capsys, t)
    assert _commit(capsys, t, token)[0] == 0

    rc, rows = _commit(capsys, t, token)

    assert rc == 0
    assert rows[-1]["outcome"] == "idempotent"


def test_v2_fails_closed_when_transport_cannot_prove_cas(monkeypatch, capsys):
    class NoCas(CasTransport):
        compare_and_swap = None

    t = _setup(NoCas([_event()]))
    monkeypatch.setattr(cli, "_now", lambda: NOW)

    assert cli.main(["queue", "r", "--agent", "amy"], transport=t) == 3
    assert "requires a proven atomic compare-and-swap" in capsys.readouterr().err
    assert t.read(records.v2_cursor_path("r", "amy", 3)) is None


def test_doctor_marks_active_v2_unhealthy_without_transport_cas(
        monkeypatch, capsys):
    import shutil

    class NoCas(CasTransport):
        compare_and_swap = None

    t = _setup(NoCas([]))
    monkeypatch.setattr(shutil, "which", lambda _name: "/bin/fulcra-api")

    assert cli.main(["doctor", "r"], transport=t) == 1
    assert "cannot prove atomic CAS" in capsys.readouterr().err


def test_rollback_read_replays_pending_even_when_writer_floor_blocks(
        monkeypatch, capsys):
    t = _setup(CasTransport([_event()]))
    token, _rows = _stage(monkeypatch, capsys, t)
    cfg = _config(minimum_writer_version="9.0.0")
    t.put(records.config_path("r"), json.dumps(cfg))

    assert cli.main(
        ["queue", "r", "--agent", "amy", "--json"], transport=t) == 0
    rows = _json_lines(capsys)

    assert rows[-1]["token"] == token
    assert rows[-1]["outcome"] == "replayed"
    assert len(t.cas_calls) == 1


def test_v18_is_rejected_by_schema_floor_even_if_authority_floor_is_too_low():
    cfg = dict(_config(
        minimum_reader_version="1.8.0",
        minimum_writer_version="1.8.0"), authority_mode="versioned")
    gate = records.compatibility(
        cfg, engine_version="1.8.0", write_cursor=False)
    assert gate["ok"] is False
    assert "predates cursor schema v2" in gate["reason"]
