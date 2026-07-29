"""Executable form of the coord v3 read/process/commit contract.

Normative source: ``reports/2026-07-29-coordv3-r2-spec-codex-coder.md`` — the
six-step contract in "Processing acknowledgment remains the correctness
boundary", the CAS requirements in "Concurrent wakes require CAS", and the ten
acceptance gates.

Why a reference model lives in the test tree
--------------------------------------------
The acceptance gates have to exist before the engine internals do, or slice 2
lands with its own author's idea of what passing means. But gates written
against nothing are unfalsifiable: they can be subtly wrong for weeks and nobody
finds out until they are pointed at real code and quietly pass.

So this module ships two things:

* :class:`ReferenceQueue` — a minimal, correct implementation of the contract.
  The gates run against it and must pass. That proves the gates are *satisfiable*
  and that the harness itself works.
* A family of deliberately BROKEN implementations, each wrong in exactly one way
  the spec names (advances coverage on read, last-writer-wins instead of CAS,
  non-idempotent commit, collapses INVALID into ABSENT, collapses UNKNOWN into
  empty). Each must FAIL its corresponding gate.

A gate that passes the reference and fails every mutant has teeth. A gate that
passes both is decoration, and the mutation matrix is what stops us shipping
decoration. None of this constrains codex-coder's engine internals — the binding
to the real implementation is the small :class:`QueueLike` surface at the bottom,
and only that surface is a shared decision.

The store fake models the one property the whole contract rests on: a write is
conditional on the generation you read. Nothing here talks to a network.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional, Protocol


class TransportUnknown(Exception):
    """The store could not be consulted. Distinct from "the store said no".

    The spec is emphatic that this must never decay into ABSENT or into an empty
    result: "block network/DNS/auth; every layer reports UNKNOWN and preserves
    coverage". Modeling it as an exception rather than a return value keeps a
    careless implementation from accidentally treating it as data.
    """


class ReadState(str, Enum):
    """Terminal state of a read. Three, never two."""

    DATA = "data"
    CLEAR = "clear"        # affirmatively nothing owed
    UNKNOWN = "unknown"    # could not be established
    INVALID = "invalid"    # config/state exists but is malformed (gate 5)


class CommitOutcome(str, Enum):
    OK = "ok"                  # coverage advanced
    IDEMPOTENT = "idempotent"  # already committed; no further advance
    STALE = "stale"            # generation moved on; refused
    UNKNOWN_TOKEN = "unknown"  # never issued, or long expired


@dataclass
class ReadResult:
    """What ``queue read`` returns: events PLUS the delivery token."""

    state: ReadState
    events: list[dict[str, Any]] = field(default_factory=list)
    token: Optional[str] = None
    generation: Optional[int] = None
    detail: str = ""


@dataclass
class _Entry:
    content: str
    generation: int


class FakeStore:
    """In-memory store with per-path generations and injectable faults.

    ``write_cas`` is the whole point: it refuses a write whose expected
    generation no longer matches. A store without that refusal cannot tell a
    correct implementation from a last-writer-wins one, so the fake has to model
    it even though the real File Store's CAS is slice 2's problem to arrange.
    """

    def __init__(self) -> None:
        self._data: dict[str, _Entry] = {}
        #: paths whose reads raise UNKNOWN (network/DNS/auth injection)
        self.unreadable: set[str] = set()
        #: paths whose writes raise UNKNOWN
        self.unwritable: set[str] = set()
        self.write_log: list[tuple[str, int]] = []
        #: One-shot hook fired just BEFORE a write commits. This is how a test
        #: interleaves two writers for real: without it, calling reader A then
        #: reader B is merely sequential — B observes A's completed write and
        #: never races it, so a CAS gate written that way passes against a
        #: last-writer-wins implementation. The hook lets B do its whole read
        #: while A is still in flight, which is the actual concurrent wake.
        self.before_write: Optional[Any] = None
        self._in_hook = False

    def _fire_before_write(self, path: str) -> None:
        hook, self.before_write = self.before_write, None
        if hook is None or self._in_hook:
            return
        self._in_hook = True
        try:
            hook(path)
        finally:
            self._in_hook = False

    # --- faults -------------------------------------------------------
    def break_reads(self, *paths: str) -> None:
        self.unreadable.update(paths)

    def break_writes(self, *paths: str) -> None:
        self.unwritable.update(paths)

    def heal(self) -> None:
        self.unreadable.clear()
        self.unwritable.clear()

    # --- store --------------------------------------------------------
    def read(self, path: str) -> Optional[str]:
        if path in self.unreadable:
            raise TransportUnknown(path)
        entry = self._data.get(path)
        return None if entry is None else entry.content

    def generation(self, path: str) -> int:
        if path in self.unreadable:
            raise TransportUnknown(path)
        entry = self._data.get(path)
        return 0 if entry is None else entry.generation

    def write_cas(self, path: str, content: str, expected_generation: int) -> bool:
        """Conditional write. False means someone else moved first."""
        if path in self.unwritable:
            raise TransportUnknown(path)
        self._fire_before_write(path)
        current = self._data.get(path)
        current_gen = 0 if current is None else current.generation
        if current_gen != expected_generation:
            return False
        self._data[path] = _Entry(content, current_gen + 1)
        self.write_log.append((path, current_gen + 1))
        return True

    def write_blind(self, path: str, content: str) -> bool:
        """Unconditional write — what a pre-CAS (or old) writer does.

        Present so the harness can play the antagonist in gates 3 and 7 without
        pretending an old client would politely use CAS.
        """
        if path in self.unwritable:
            raise TransportUnknown(path)
        self._fire_before_write(path)
        current = self._data.get(path)
        gen = (0 if current is None else current.generation) + 1
        self._data[path] = _Entry(content, gen)
        self.write_log.append((path, gen))
        return True

    def seed(self, path: str, content: str) -> None:
        self._data[path] = _Entry(content, 1)


def cursor_path(team: str, agent: str) -> str:
    return f"team/{team}/_coord/agents/{agent}/records-cursor-v2.json"


def config_path(team: str) -> str:
    return f"team/{team}/_coord/bus-v3/records.json"


# --- the reference implementation -------------------------------------------

class ReferenceQueue:
    """A correct, minimal read/process/commit implementation.

    Deliberately dumb about everything except the contract: it is here to be the
    yardstick the gates are calibrated against, not to be efficient or to
    anticipate codex-coder's internals.
    """

    #: A staged batch older than this is abandoned and replays (spec step 5).
    STAGE_TIMEOUT_S = 900

    #: Protocol version this implementation writes. Gate 7's antagonist writes a
    #: lower one, which a v2 reader must refuse to trust.
    PROTOCOL_VERSION = 2

    def __init__(self, store: FakeStore, team: str, agent: str,
                 records: Optional[list[dict[str, Any]]] = None) -> None:
        self.store = store
        self.team = team
        self.agent = agent
        self.records = list(records or [])
        self.audit: list[dict[str, Any]] = []

    # --- internals ----------------------------------------------------
    def _load(self) -> tuple[Optional[dict[str, Any]], int, ReadState]:
        path = cursor_path(self.team, self.agent)
        try:
            raw = self.store.read(path)
            gen = self.store.generation(path)
        except TransportUnknown:
            return None, -1, ReadState.UNKNOWN
        if raw is None:
            return {"v": self.PROTOCOL_VERSION, "last_read": None,
                    "staged": None, "committed": []}, gen, ReadState.DATA
        try:
            doc = json.loads(raw)
        except ValueError:
            return None, gen, ReadState.INVALID
        if not isinstance(doc, dict):
            return None, gen, ReadState.INVALID
        version = doc.get("v")
        if not isinstance(version, int) or version > self.PROTOCOL_VERSION:
            # A document written by something newer than us. Refusing is the
            # only safe move: we cannot know which fields we would destroy.
            return None, gen, ReadState.INVALID
        if version < self.PROTOCOL_VERSION:
            # Gate 7: legacy-shaped state must not be silently adopted as v2
            # coverage. It is INVALID for our purposes, not a starting point.
            return None, gen, ReadState.INVALID
        return doc, gen, ReadState.DATA

    def _config_ok(self) -> ReadState:
        try:
            raw = self.store.read(config_path(self.team))
        except TransportUnknown:
            return ReadState.UNKNOWN
        if raw is None:
            return ReadState.INVALID  # no stream configured: not "no work"
        try:
            doc = json.loads(raw)
        except ValueError:
            return ReadState.INVALID  # gate 5: malformed is INVALID, not ABSENT
        if not isinstance(doc, dict) or not doc.get("data_type"):
            return ReadState.INVALID
        return ReadState.DATA

    # --- contract -----------------------------------------------------
    def read(self, now: float) -> ReadResult:
        """Steps 1-2: return events + token; NEVER advance durable coverage."""
        cfg = self._config_ok()
        if cfg is not ReadState.DATA:
            return ReadResult(state=cfg, detail="config")

        doc, gen, state = self._load()
        if state is not ReadState.DATA or doc is None:
            return ReadResult(state=state, generation=None, detail="cursor")

        staged = doc.get("staged")
        if isinstance(staged, dict):
            age = now - float(staged.get("staged_at") or 0)
            if age <= self.STAGE_TIMEOUT_S:
                # Step 5: an uncommitted batch replays. Same token, same events —
                # a crash between print and process must not consume the wake.
                return ReadResult(state=ReadState.DATA,
                                  events=list(staged.get("events") or []),
                                  token=staged.get("token"),
                                  generation=gen, detail="replay")

        last = doc.get("last_read")
        fresh = [r for r in self.records
                 if last is None or str(r.get("recorded_at", "")) > str(last)]
        if not fresh:
            return ReadResult(state=ReadState.CLEAR, generation=gen)

        token = f"{self.agent}-{gen}-{len(fresh)}"
        window_end = max(str(r.get("recorded_at", "")) for r in fresh)
        doc["staged"] = {"token": token, "events": fresh,
                         "window_end": window_end, "staged_at": now}
        ok = self.store.write_cas(cursor_path(self.team, self.agent),
                                  json.dumps(doc), gen)
        if not ok:
            # Someone else staged first. Re-read rather than clobber: for one
            # agent there is one queue, and the other wake's batch is as valid
            # as ours would have been.
            doc2, gen2, state2 = self._load()
            if state2 is not ReadState.DATA or doc2 is None:
                return ReadResult(state=state2, detail="conflict-reload")
            other = doc2.get("staged")
            if isinstance(other, dict):
                return ReadResult(state=ReadState.DATA,
                                  events=list(other.get("events") or []),
                                  token=other.get("token"), generation=gen2,
                                  detail="lost-race-adopted-peer-batch")
            return ReadResult(state=ReadState.UNKNOWN, detail="conflict")
        return ReadResult(state=ReadState.DATA, events=fresh, token=token,
                          generation=gen + 1)

    def commit(self, token: str, now: float) -> CommitOutcome:
        """Step 4/6: advance coverage under CAS; idempotent; refuse stale."""
        doc, gen, state = self._load()
        if state is not ReadState.DATA or doc is None:
            raise TransportUnknown("cursor unreadable at commit")

        if token in (doc.get("committed") or []):
            return CommitOutcome.IDEMPOTENT

        staged = doc.get("staged")
        if not isinstance(staged, dict) or staged.get("token") != token:
            # Either never issued, or superseded by a newer batch. Either way it
            # must not move coverage.
            return CommitOutcome.UNKNOWN_TOKEN

        doc["last_read"] = staged.get("window_end")
        doc["staged"] = None
        doc["committed"] = ([*(doc.get("committed") or []), token])[-64:]
        ok = self.store.write_cas(cursor_path(self.team, self.agent),
                                  json.dumps(doc), gen)
        return CommitOutcome.OK if ok else CommitOutcome.STALE

    def coverage(self) -> Optional[str]:
        doc, _gen, state = self._load()
        return None if (state is not ReadState.DATA or doc is None) else doc.get("last_read")

    def takeover(self, actor: str, reason: str, now: float) -> bool:
        """Deliberate ``--consume``-style takeover with a durable audit record.

        Gate 9. The audit is attributive, not authoritative (no authenticated
        principals yet) — but an attributive record still makes takeover visible,
        which is the property the spec asks for.
        """
        doc, gen, state = self._load()
        if state is not ReadState.DATA or doc is None:
            raise TransportUnknown("cursor unreadable at takeover")
        staged = doc.get("staged")
        entry = {"actor": actor, "target": self.agent, "reason": reason,
                 "at": now, "prior_generation": gen, "new_generation": gen + 1,
                 "token": (staged or {}).get("token") if isinstance(staged, dict) else None}
        doc["staged"] = None
        ok = self.store.write_cas(cursor_path(self.team, self.agent),
                                  json.dumps(doc), gen)
        if ok:
            self.audit.append(entry)
        return ok


# --- the obligation fold (gates 8 and 10) -----------------------------------

class ObligationFold:
    """Deterministic "do I owe anything?" across components.

    Spec item 3: the fold "must return DATA/CLEAR only after all component
    listings and documents are known. Any unreadable component makes the answer
    UNKNOWN." Modeled separately from the queue because the spec is explicit
    that an empty queue read is NOT an answer to this question.
    """

    def __init__(self, store: FakeStore, components: dict[str, str]) -> None:
        self.store = store
        self.components = components  # name -> store path

    def owed(self) -> tuple[ReadState, list[str]]:
        found: list[str] = []
        for name, path in sorted(self.components.items()):
            try:
                raw = self.store.read(path)
            except TransportUnknown:
                # One unreadable component and "nothing owed" becomes
                # unsayable. This is the whole point of gate 10.
                return ReadState.UNKNOWN, []
            if raw is None:
                continue
            try:
                doc = json.loads(raw)
            except ValueError:
                return ReadState.INVALID, []
            if doc.get("open"):
                found.append(name)
        return (ReadState.DATA if found else ReadState.CLEAR), found


# --- deliberately broken implementations (the mutation matrix) --------------

class AdvanceOnReadQueue(ReferenceQueue):
    """Wrong in the way today's engine is wrong: coverage moves at print time."""

    def read(self, now: float) -> ReadResult:
        result = super().read(now)
        if result.state is ReadState.DATA and result.events:
            doc, gen, state = self._load()
            if state is ReadState.DATA and doc is not None:
                staged = doc.get("staged") or {}
                doc["last_read"] = staged.get("window_end") or doc.get("last_read")
                doc["staged"] = None
                self.store.write_cas(cursor_path(self.team, self.agent),
                                     json.dumps(doc), gen)
        return result


class LastWriterWinsQueue(ReferenceQueue):
    """Wrong in the way the legacy cursor is wrong: no conditional write."""

    def read(self, now: float) -> ReadResult:
        cfg = self._config_ok()
        if cfg is not ReadState.DATA:
            return ReadResult(state=cfg, detail="config")
        doc, gen, state = self._load()
        if state is not ReadState.DATA or doc is None:
            return ReadResult(state=state)
        staged = doc.get("staged")
        if isinstance(staged, dict) and now - float(staged.get("staged_at") or 0) <= self.STAGE_TIMEOUT_S:
            return ReadResult(state=ReadState.DATA,
                              events=list(staged.get("events") or []),
                              token=staged.get("token"), generation=gen)
        last = doc.get("last_read")
        fresh = [r for r in self.records
                 if last is None or str(r.get("recorded_at", "")) > str(last)]
        if not fresh:
            return ReadResult(state=ReadState.CLEAR, generation=gen)
        token = f"{self.agent}-blind-{len(fresh)}"
        doc["staged"] = {"token": token, "events": fresh, "staged_at": now,
                         "window_end": max(str(r.get("recorded_at", "")) for r in fresh)}
        self.store.write_blind(cursor_path(self.team, self.agent), json.dumps(doc))
        return ReadResult(state=ReadState.DATA, events=fresh, token=token,
                          generation=gen)


class NonIdempotentCommitQueue(ReferenceQueue):
    """Wrong in the way a naive ack is wrong: a replayed commit advances again."""

    def commit(self, token: str, now: float) -> CommitOutcome:
        doc, gen, state = self._load()
        if state is not ReadState.DATA or doc is None:
            raise TransportUnknown("cursor unreadable at commit")
        staged = doc.get("staged")
        if isinstance(staged, dict) and staged.get("token") == token:
            doc["last_read"] = staged.get("window_end")
            doc["staged"] = None
            self.store.write_cas(cursor_path(self.team, self.agent),
                                 json.dumps(doc), gen)
            return CommitOutcome.OK
        # No committed-token memory: a stale token silently "succeeds" and the
        # caller believes coverage moved.
        return CommitOutcome.OK


class AbsentOnInvalidConfigQueue(ReferenceQueue):
    """Wrong in the way the 2026-07-28 incident was wrong: INVALID reads CLEAR."""

    def _config_ok(self) -> ReadState:
        state = super()._config_ok()
        return ReadState.CLEAR if state is ReadState.INVALID else state


class EmptyOnTransportFailureQueue(ReferenceQueue):
    """Wrong in the way a false-clear is wrong: UNKNOWN collapses to CLEAR."""

    def read(self, now: float) -> ReadResult:
        try:
            result = super().read(now)
        except TransportUnknown:
            return ReadResult(state=ReadState.CLEAR)
        if result.state is ReadState.UNKNOWN:
            return ReadResult(state=ReadState.CLEAR)
        return result


class QueueLike(Protocol):
    """The only surface the gates bind to.

    Kept this small on purpose: when slice 2's engine lands, an adapter with
    these three methods binds every gate to the real implementation, and nothing
    in the gate bodies has to change. Engine internals stay codex-coder's call.
    """

    def read(self, now: float) -> ReadResult: ...
    def commit(self, token: str, now: float) -> CommitOutcome: ...
    def coverage(self) -> Optional[str]: ...
