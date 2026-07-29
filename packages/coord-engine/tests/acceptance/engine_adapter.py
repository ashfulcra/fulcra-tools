"""Bind the independent queue contract to coord-engine's real Slice 2 code.

The acceptance harness deliberately defines a tiny ``QueueLike`` surface.
This adapter implements that surface by invoking the production CLI with a
transport backed by the harness's interleavable CAS store.  It therefore keeps
the independent gate bodies unchanged while exercising the real config parser,
event parser, cursor loader, staging CAS, delivery rendering, and commit path.
"""

from __future__ import annotations

import contextlib
import io
import json
from datetime import datetime, timezone
from typing import Any, Optional

from coord_engine import cli, records

from acceptance.contract import (
    CommitOutcome,
    FakeStore,
    ReadResult,
    ReadState,
    TransportUnknown,
)


AUTHORITY_GENERATION = 3


def engine_cursor_path(team: str, agent: str) -> str:
    """The production generation-scoped cursor selected by the authority."""
    return records.v2_cursor_path(team, agent, AUTHORITY_GENERATION)


class _HarnessTransport:
    """Production transport surface backed by the harness's CAS store."""

    def __init__(self, owner: "EngineQueueAdapter") -> None:
        self.owner = owner

    def read(self, path: str) -> Optional[str]:
        return self.owner.store.read(path)

    def read_classified(self, path: str) -> tuple[Optional[str], str]:
        try:
            value = self.read(path)
        except TransportUnknown:
            return None, "error"
        return (value, "ok") if value is not None else (None, "absent")

    def records(self, _data_type: str, _since: str, _until: str) -> list[dict[str, Any]]:
        rows = []
        for event in self.owner.records:
            slug = str(event.get("slug") or event["id"])
            rows.append({
                "id": event["id"],
                "recorded_at": event.get("recorded_at"),
                "sources": ["coord-boss"],
                "note": records.build_payload(
                    to=self.owner.agent,
                    kind="directive",
                    priority="P0",
                    slug=slug,
                ),
            })
        return rows

    def compare_and_swap(
            self, path: str, expected_raw: Optional[str], new_raw: str) -> bool:
        try:
            current = self.owner.store.read(path)
            generation = self.owner.store.generation(path)
        except TransportUnknown:
            return False
        if current != expected_raw:
            return False
        return self.owner.store.write_cas(path, new_raw, generation)


class EngineQueueAdapter:
    """The harness ``QueueLike`` API backed by production coord-engine."""

    def __init__(
            self, store: FakeStore, team: str, agent: str,
            records: Optional[list[dict[str, Any]]] = None) -> None:
        self.store = store
        self.team = team
        self.agent = agent
        self.records = list(records or [])
        self.transport = _HarnessTransport(self)
        self._activate_v2_if_legacy_fixture()

    def _activate_v2_if_legacy_fixture(self) -> None:
        """Upgrade only the harness's valid two-field legacy fixture.

        Corrupt or partially versioned configs are deliberately left byte-for-
        byte untouched so the production parser, rather than this adapter,
        decides INVALID versus UNKNOWN.
        """
        path = records.config_path(self.team)
        try:
            raw = self.store.read(path)
            doc = json.loads(raw) if raw is not None else None
        except (TransportUnknown, TypeError, ValueError):
            return
        if not isinstance(doc, dict) or set(doc) - {"data_type", "api_version"}:
            return
        if not isinstance(doc.get("data_type"), str):
            return
        doc.update({
            "protocol_version": records.PROTOCOL_VERSION,
            "cursor_schema_version": records.CURSOR_SCHEMA_VERSION,
            "minimum_reader_version": "1.9.0",
            "minimum_writer_version": "1.9.0",
            "cursor_generation": AUTHORITY_GENERATION,
            "cursor_activated_at": "2026-07-29T09:59:00Z",
        })
        # Fixture setup is outside the protocol under test.  ``seed`` avoids
        # adding an authority write to the gate's cursor-CAS evidence.
        self.store.seed(path, json.dumps(doc, sort_keys=True))

    def _clock(self) -> datetime:
        timestamps = [
            event.get("recorded_at") for event in self.records
            if isinstance(event.get("recorded_at"), str)
        ]
        if timestamps:
            return datetime.fromisoformat(max(timestamps).replace("Z", "+00:00"))
        return datetime(2026, 7, 29, 10, 5, tzinfo=timezone.utc)

    def _run(self, argv: list[str]) -> tuple[int, list[dict[str, Any]], str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        original_now = cli._now
        cli._now = self._clock
        try:
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                rc = cli.main(argv, transport=self.transport)
        finally:
            cli._now = original_now
        rows = [
            json.loads(line) for line in stdout.getvalue().splitlines()
            if line.strip()
        ]
        return rc, rows, stderr.getvalue()

    def read(self, _now: float) -> ReadResult:
        rc, rows, error = self._run([
            "queue", self.team, "--agent", self.agent, "--json",
        ])
        if rc != 0:
            envelope = rows[-1] if rows else {}
            machine_state = envelope.get("state")
            state = {
                "INVALID": ReadState.INVALID,
                "UNKNOWN": ReadState.UNKNOWN,
            }.get(machine_state, ReadState.UNKNOWN)
            return ReadResult(state=state, detail=error.strip())

        if not rows or rows[-1].get("type") != "queue-delivery":
            return ReadResult(state=ReadState.UNKNOWN, detail="missing delivery envelope")
        envelope, event_rows = rows[-1], rows[:-1]
        events = [
            {
                "id": row["record_id"],
                "recorded_at": row.get("recorded_at"),
                "slug": row.get("slug"),
            }
            for row in event_rows
        ]
        return ReadResult(
            state=ReadState.DATA if events else ReadState.CLEAR,
            events=events,
            token=envelope.get("token"),
            generation=envelope.get("cursor_revision"),
            detail=(
                "lost-race-adopted-peer-batch"
                if envelope.get("outcome") == "replayed"
                else "staged"
            ),
        )

    def commit(self, token: str, _now: float) -> CommitOutcome:
        cursor, _raw, status = records.load_v2_cursor_classified(
            self.transport, self.team, self.agent, AUTHORITY_GENERATION)
        event_ids = []
        if status == "ok" and cursor is not None:
            pending = cursor.get("pending")
            if isinstance(pending, dict):
                event_ids = [
                    event["record_id"] for event in pending.get("events", [])
                    if isinstance(event.get("record_id"), str)
                ]
        argv = [
            "queue", "commit", self.team,
            "--agent", self.agent,
            "--token", token,
            "--json",
        ]
        for record_id in event_ids:
            argv.extend(["--result", f"{record_id}=completed"])
        rc, rows, _error = self._run(argv)
        if rc != 0:
            return CommitOutcome.UNKNOWN_TOKEN
        outcome = rows[-1].get("outcome") if rows else None
        return (
            CommitOutcome.OK
            if outcome == "committed"
            else CommitOutcome.IDEMPOTENT
        )

    def coverage(self) -> Optional[str]:
        cursor, _raw, status = records.load_v2_cursor_classified(
            self.transport, self.team, self.agent, AUTHORITY_GENERATION)
        if status != "ok" or cursor is None:
            return None
        return cursor["committed"].get("last_read")
