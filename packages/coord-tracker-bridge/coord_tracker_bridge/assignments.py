"""`linear-assignments` — route Linear assignment/state changes to the fleet.

PHASE 1, AND ONLY PHASE 1 (design: `_coord/agents/coord-boss/reports/
2026-08-19-linear-integration-design.md`, approved by Ash 2026-08-19). Zero
Linear writes: this module reaches the platform exclusively through
`linear-inbox`'s `ReadOnlyTransport`, which refuses any document that is not a
pure query. Phases 2 and 3 — the one-time board reconcile and the two-tier
projection — are separately gated on Ash GO'ing a printed plan plus a
bot-actor token, and nothing here anticipates them.

WHAT IT DOES. Reads the board, selects the issues that moved since a durable
watermark, decides for each whether the ASSIGNEE OR STATE actually changed, and
turns each real change into a durable directive: to the assigned fleet agent
when the name resolves, to the coordinator for triage when it does not. Never
to a guessed address.

WHY IT RE-READS THE WHOLE BOARD. It could filter server-side on `updatedAt`.
It doesn't: `fetch_inbox` is the read path that survived nine review rounds and
carries the completeness contract (null nodes, hollow sub-objects, pageInfo
internals, cursor cycles), and a second query shape would be a second place for
a partial board to be reported as a whole one. The board is ~124 issues; the
bandwidth is not worth a parallel contract. The watermark is applied to the
rows AFTER they have been read faithfully.

WHY A WATERMARK ALONE IS NOT ENOUGH. Linear bumps `updatedAt` for any edit — a
retitle, a description tweak — so a watermark selects CANDIDATES, not changes.
Routing on candidates would dispatch a directive every time Ash fixes a typo,
and the design names noise as a defect in its own right. So the durable state
also remembers the (assignee, state) pair last observed per issue, and only a
pair that actually differs is routed.

DELIVERY IS AT-LEAST-ONCE AND SAYS SO. The state file is written after the
directives go out, so a crash in between re-delivers. That is the coord-mesh
cursor doctrine — a cursor may repeat, it may never skip — and a repeat is
announced in the directive itself rather than hidden, because a silent
duplicate is indistinguishable from a real second assignment.

PREVIEW IS THE DEFAULT. `--deliver` is what dispatches and what advances the
watermark; without it the verb prints the plan and consumes nothing. The
reason is on the record: the near-miss that shaped this whole lane was a plan
that would have pushed ~503 creates into a curated board, and a first run of
THIS verb against a cold watermark has exactly the same shape with the fleet
bus as the target. A cold start therefore refuses to deliver at all until
`--seed` establishes the baseline.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

from .inbox import InboxItem, ReadOnlyTransport, fetch_inbox
from .linear import LinearClient
from .source import CommandRunner, subprocess_runner

STATE_SCHEMA_VERSION = 1

#: Circuit breaker, mirroring the design's card cap. A single run that wants to
#: dispatch more than this refuses whole rather than flooding the bus; the
#: operator raises it deliberately or seeds past the backlog.
DEFAULT_DELIVERY_CAP = 25

#: Dispositions. Exactly one is a delivery to a fleet agent.
RESOLVED = "resolved"
UNASSIGNED = "unassigned"
UNRESOLVED = "unresolved"
NOT_ADDRESSABLE = "mesh-peer"
AMBIGUOUS = "ambiguous"


class AssignmentsUnknown(RuntimeError):
    """This run proves nothing. Never rendered as "no assignments changed"."""


class RosterUnreadable(AssignmentsUnknown):
    """The nickname roster could not be read or parsed into usable mappings.

    Deliberately NOT "no mappings". An empty roster would route every issue to
    the coordinator as unresolved, which reads on the bus as a confident finding
    about Ash's assignments when it is really a failure to read a file.
    """


class StateUnreadable(AssignmentsUnknown):
    """The durable state file exists but cannot be trusted.

    An ABSENT state file is a genuine cold start — that default asserts only
    "we have never run", which is true. A CORRUPT one asserts nothing safe:
    read as cold start it re-seeds over real history, read as empty history it
    re-delivers the whole board. So it is UNKNOWN and the run stops.
    """


class DispatchFailed(RuntimeError):
    """A directive did not go out. The watermark must not advance past it."""


# --------------------------------------------------------------------------
# time
# --------------------------------------------------------------------------

def parse_timestamp(value: Any) -> datetime | None:
    """An aware UTC datetime, or None when the value cannot be placed in time.

    A TIMEZONE-NAIVE timestamp returns None rather than being assumed UTC. The
    coord-mesh cursor work paid for this one: assuming an offset on a naive
    stamp silently reorders rows against a watermark that is aware, and rows
    that sort wrong against a watermark are rows that get skipped.
    """
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw:
        return None
    if raw.endswith(("Z", "z")):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.tzinfo.utcoffset(parsed) is None:
        return None
    return parsed.astimezone(timezone.utc)


def _isoformat(when: datetime) -> str:
    return when.astimezone(timezone.utc).isoformat()


# --------------------------------------------------------------------------
# durable state
# --------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class Cursor:
    """Watermark plus the ledger of ids AT the watermark.

    A bare timestamp cannot express "I have seen three of the four rows stamped
    12:00:00" — `>` skips the fourth and `>=` replays all four forever. The
    ledger is what makes the boundary exact, and it is the shape coord-mesh
    arrived at after five rounds of getting the same boundary wrong.
    """

    t: datetime | None = None
    ids: frozenset[str] = frozenset()

    def is_after(self, when: datetime, identifier: str) -> bool:
        if self.t is None:
            return True
        if when > self.t:
            return True
        if when == self.t:
            return identifier not in self.ids
        return False


def fingerprint(identifier: str, assignee: str | None, state: str) -> str:
    return "\x1f".join((identifier, assignee or "", state))


@dataclass
class AssignmentState:
    """Everything this verb remembers between runs."""

    seeded: bool = False
    cursor: Cursor = Cursor()
    #: identifier -> (assignee, state) as last OBSERVED (not necessarily delivered)
    observed: dict[str, tuple[str | None, str]] = field(default_factory=dict)
    #: fingerprints that have already been dispatched at least once
    delivered: set[str] = field(default_factory=set)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": STATE_SCHEMA_VERSION,
            "seeded": self.seeded,
            "cursor": {
                "t": _isoformat(self.cursor.t) if self.cursor.t else None,
                "ids": sorted(self.cursor.ids),
            },
            "observed": {
                key: {"assignee": value[0], "state": value[1]}
                for key, value in sorted(self.observed.items())
            },
            "delivered": sorted(self.delivered),
        }

    @classmethod
    def from_dict(cls, raw: Any) -> AssignmentState:
        if not isinstance(raw, Mapping):
            raise StateUnreadable("assignment state root is not an object")
        if raw.get("schema_version") != STATE_SCHEMA_VERSION:
            raise StateUnreadable(
                f"unsupported assignment state schema_version {raw.get('schema_version')!r}")
        seeded = raw.get("seeded")
        if not isinstance(seeded, bool):
            raise StateUnreadable("assignment state `seeded` is missing or not a boolean")
        cursor_raw = raw.get("cursor")
        if not isinstance(cursor_raw, Mapping):
            raise StateUnreadable("assignment state `cursor` is missing or not an object")
        raw_t = cursor_raw.get("t")
        if raw_t is None:
            when = None
        else:
            when = parse_timestamp(raw_t)
            if when is None:
                raise StateUnreadable(
                    "assignment state cursor timestamp is present but unusable — a "
                    "watermark we cannot place in time is not a watermark")
        raw_ids = cursor_raw.get("ids")
        if not isinstance(raw_ids, list) or not all(isinstance(i, str) for i in raw_ids):
            raise StateUnreadable("assignment state cursor ids are missing or not strings")
        observed_raw = raw.get("observed")
        if not isinstance(observed_raw, Mapping):
            raise StateUnreadable("assignment state `observed` is missing or not an object")
        observed: dict[str, tuple[str | None, str]] = {}
        for key, value in observed_raw.items():
            if not isinstance(key, str) or not isinstance(value, Mapping):
                raise StateUnreadable("assignment state `observed` entry is malformed")
            assignee = value.get("assignee")
            state = value.get("state")
            if assignee is not None and not isinstance(assignee, str):
                raise StateUnreadable(f"observed[{key}].assignee is present but not a string")
            if not isinstance(state, str) or not state:
                raise StateUnreadable(f"observed[{key}].state is missing or not a string")
            observed[key] = (assignee, state)
        delivered_raw = raw.get("delivered")
        if not isinstance(delivered_raw, list) or not all(
            isinstance(i, str) for i in delivered_raw
        ):
            raise StateUnreadable("assignment state `delivered` is missing or not strings")
        return cls(
            seeded=seeded,
            cursor=Cursor(t=when, ids=frozenset(raw_ids)),
            observed=observed,
            delivered=set(delivered_raw),
        )

    @classmethod
    def load(cls, path: str | Path) -> AssignmentState:
        target = Path(path)
        if not target.exists():
            return cls()                      # genuine cold start
        try:
            raw = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise StateUnreadable(f"assignment state at {target} could not be read: {exc}")
        return cls.from_dict(raw)

    def save(self, path: str | Path) -> None:
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self.to_dict(), sort_keys=True, indent=2) + "\n"
        fd, staged = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(staged, destination)
        finally:
            try:
                os.unlink(staged)
            except FileNotFoundError:
                pass


# --------------------------------------------------------------------------
# the nickname roster
# --------------------------------------------------------------------------

#: The heading that separates the two tables in `_coord/roster-nicknames.md`.
#: The distinction is load-bearing, not cosmetic: the doc says in as many words
#: that mesh peers are "NOT fleet agents — reachable only via mesh outbox
#: channels, never via coord-engine tell". A `tell` addressed to one is a
#: directive that can never arrive, which is worse than no delivery because it
#: LOOKS delivered on our side.
_MESH_SECTION_MARKER = "external mesh peers"


@dataclass(frozen=True, slots=True)
class Roster:
    """Aliases (lowercased) to their resolution."""

    fleet: Mapping[str, str]
    mesh: Mapping[str, str]
    ambiguous: frozenset[str]

    def resolve(self, display_name: str | None) -> tuple[str, str | None]:
        """(disposition, target). Target is an agent name only when RESOLVED."""
        if display_name is None:
            return UNASSIGNED, None
        key = display_name.strip().casefold()
        if not key:
            return UNASSIGNED, None
        if key in self.ambiguous:
            return AMBIGUOUS, None
        if key in self.mesh:
            return NOT_ADDRESSABLE, self.mesh[key]
        if key in self.fleet:
            return RESOLVED, self.fleet[key]
        return UNRESOLVED, None


def _table_cells(line: str) -> list[str] | None:
    stripped = line.strip()
    if not stripped.startswith("|"):
        return None
    cells = [cell.strip() for cell in stripped.strip("|").split("|")]
    if len(cells) < 2:
        return None
    if all(set(cell) <= set("-: ") for cell in cells if cell):
        return None                            # separator row
    return cells


def _aliases(cell: str) -> list[str]:
    """One nickname cell may list several names, separated by `/`."""
    return [part.strip() for part in cell.split("/") if part.strip()]


def parse_roster(text: str) -> Roster:
    """Parse the two markdown tables. Fail closed on anything unusable.

    Written against the document as it stands, not against markdown in general:
    the fleet table is `| nickname | agent name | notes |` and the mesh table is
    `| name | identity | address |`, split by a heading naming external peers.
    """
    if not isinstance(text, str) or not text.strip():
        raise RosterUnreadable("the nickname roster is empty")
    fleet: dict[str, str] = {}
    mesh: dict[str, str] = {}
    ambiguous: set[str] = set()
    in_mesh = False

    def register(store: dict[str, str], alias: str, value: str) -> None:
        """An alias that already points somewhere ELSE becomes ambiguous.

        Checked across BOTH tables, because the worst collision is a name that
        is a fleet agent in one and a mesh peer in the other: resolving it
        would deliver a directive to whichever table happened to be parsed
        last. An ambiguous alias resolves to nobody and goes to triage.
        """
        key = alias.casefold()
        for prior in (fleet.get(key), mesh.get(key)):
            if prior is not None and prior != value:
                ambiguous.add(key)
        store[key] = value

    for line in text.splitlines():
        if _MESH_SECTION_MARKER in line.casefold():
            in_mesh = True
            continue
        cells = _table_cells(line)
        if cells is None:
            continue
        first, second = cells[0], cells[1]
        if not first or not second:
            continue
        header = first.casefold()
        if header.startswith("nickname") or header == "name":
            continue                            # header row
        if in_mesh:
            for alias in _aliases(first):
                register(mesh, alias, second)
        else:
            agent = second.strip()
            if " " in agent:
                # The agent-name column must hold a bus address, and a bus
                # address has no spaces. A prose cell here means the table
                # shape changed under us; resolving from it would invent an
                # address. Skipping the row leaves the name UNRESOLVED, which
                # routes to triage — visible, and never a guessed delivery.
                continue
            for alias in _aliases(first):
                register(fleet, alias, agent)
            register(fleet, agent, agent)

    if not fleet:
        raise RosterUnreadable(
            "the nickname roster parsed to zero fleet agents — treating that as "
            "'nobody resolves' would report a confident triage verdict on every "
            "issue from a file we failed to read")
    for key in ambiguous:
        fleet.pop(key, None)
        mesh.pop(key, None)
    return Roster(fleet=fleet, mesh=mesh, ambiguous=frozenset(ambiguous))


class RosterReader(Protocol):
    def __call__(self) -> str: ...


@dataclass(frozen=True, slots=True)
class FulcraRosterReader:
    """Read the roster document out of the coord store.

    `FulcraTeamsTransport.read` returns None for both "absent" and "the command
    failed", which is precisely the collapse this lane keeps finding; this
    reader raises instead, so an unreadable roster reaches the caller as UNKNOWN.
    """

    path: str = "team/fulcra/_coord/roster-nicknames.md"
    runner: CommandRunner = subprocess_runner
    timeout: float = 30.0
    command: tuple[str, ...] = ("fulcra-api",)

    def __call__(self) -> str:
        code, stdout, stderr = self.runner(
            (*self.command, "file", "download", self.path, "-"), self.timeout
        )
        if code != 0:
            raise RosterUnreadable(
                f"reading the nickname roster at {self.path} failed (rc {code}): "
                f"{stderr.strip()[:200]}")
        return stdout


# --------------------------------------------------------------------------
# planning
# --------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class Route:
    """One directive this run would send. Built only from validated fields."""

    identifier: str
    title: str
    url: str | None
    assignee: str | None
    state: str
    updated_at: datetime
    disposition: str
    target: str
    redelivery: bool
    previous: tuple[str | None, str] | None

    @property
    def to_agent(self) -> bool:
        return self.disposition == RESOLVED


@dataclass(frozen=True, slots=True)
class Candidate:
    """A row past the watermark, and what (if anything) it routes to."""

    item: InboxItem
    updated_at: datetime
    route: Route | None


@dataclass(frozen=True, slots=True)
class Plan:
    cold_start: bool
    considered: int
    candidates: tuple[Candidate, ...]

    @property
    def routes(self) -> tuple[Route, ...]:
        return tuple(c.route for c in self.candidates if c.route is not None)

    @property
    def unchanged(self) -> tuple[str, ...]:
        return tuple(c.item.identifier for c in self.candidates if c.route is None)


def _placeable(item: InboxItem) -> datetime:
    """The row's position in time, or UNKNOWN for the whole run.

    `updated_at` is OPTIONAL in `linear-inbox` and REQUIRED here, and the
    difference is not inconsistency — it is what load-bearing means. A verb
    that prints a board can print a row with no timestamp; a verb that decides
    "newer than the watermark" cannot. Neither default asserts nothing: called
    old, the row is silently never delivered; called new, it is delivered on
    every single run forever. So it is UNKNOWN.
    """
    when = parse_timestamp(item.updated_at)
    if when is None:
        raise AssignmentsUnknown(
            f"issue {item.identifier} has no usable updatedAt — it cannot be "
            "placed against the watermark, and a row we cannot place is one we "
            "would either never deliver or deliver forever")
    return when


def _routable_state(item: InboxItem) -> str:
    if not item.state_present:
        raise AssignmentsUnknown(
            f"issue {item.identifier} came back without a workflow state — "
            "routing on a state change requires having read the state, and the "
            "placeholder is not a reading")
    return item.state


def plan_assignments(
    items: Sequence[InboxItem],
    state: AssignmentState,
    roster: Roster,
    *,
    coordinator: str,
) -> Plan:
    """Pure. Decide what this run WOULD do; dispatch nothing, mutate nothing."""
    placed: list[tuple[datetime, InboxItem]] = []
    for item in items:
        when = _placeable(item)
        _routable_state(item)                  # raises before anything is planned
        placed.append((when, item))
    placed.sort(key=lambda pair: (pair[0], pair[1].identifier))

    candidates: list[Candidate] = []
    for when, item in placed:
        if not state.cursor.is_after(when, item.identifier):
            continue
        current = (item.assignee, _routable_state(item))
        previous = state.observed.get(item.identifier)
        if previous == current:
            candidates.append(Candidate(item=item, updated_at=when, route=None))
            continue
        disposition, target = roster.resolve(item.assignee)
        candidates.append(Candidate(
            item=item,
            updated_at=when,
            route=Route(
                identifier=item.identifier,
                title=item.title,
                url=item.url,
                assignee=item.assignee,
                state=current[1],
                updated_at=when,
                disposition=disposition,
                target=target if disposition == RESOLVED else coordinator,
                redelivery=fingerprint(item.identifier, *current) in state.delivered,
                previous=previous,
            ),
        ))
    return Plan(
        cold_start=not state.seeded,
        considered=len(placed),
        candidates=tuple(candidates),
    )


def advance(
    state: AssignmentState,
    handled: Sequence[Candidate],
    *,
    dispatched: Sequence[Route] = (),
) -> AssignmentState:
    """Fold a PREFIX of successfully handled candidates into new durable state.

    A prefix, never a set. The candidates are in total order, so stopping at
    the first failure and folding everything before it leaves a watermark that
    is exactly true: everything at or below it is done, everything above it is
    still owed. That is the rule the coord-mesh cursor took five rounds to
    reach — a watermark that skips is a lost message, and a watermark that
    moves backwards is a replay.
    """
    observed = dict(state.observed)
    delivered = set(state.delivered)
    for candidate in handled:
        observed[candidate.item.identifier] = (
            candidate.item.assignee,
            candidate.item.state,
        )
    for route in dispatched:
        delivered.add(fingerprint(route.identifier, route.assignee, route.state))

    cursor = state.cursor
    if handled:
        top = max(candidate.updated_at for candidate in handled)
        if cursor.t is None or top > cursor.t:
            ids = frozenset(c.item.identifier for c in handled if c.updated_at == top)
            cursor = Cursor(t=top, ids=ids)
        elif top == cursor.t:
            ids = frozenset(c.item.identifier for c in handled if c.updated_at == top)
            cursor = Cursor(t=top, ids=cursor.ids | ids)
        # top < cursor.t cannot happen for candidates (they are all past the
        # watermark by construction) and is ignored if it ever does: the
        # watermark never moves backwards.
    return AssignmentState(
        seeded=state.seeded,
        cursor=cursor,
        observed=observed,
        delivered=delivered,
    )


def seed(state: AssignmentState, items: Sequence[InboxItem]) -> AssignmentState:
    """Adopt the whole board as the baseline WITHOUT delivering anything."""
    observed = dict(state.observed)
    cursor = state.cursor
    top: datetime | None = None
    for item in items:
        when = _placeable(item)
        observed[item.identifier] = (item.assignee, _routable_state(item))
        top = when if top is None or when > top else top
    if top is not None and (cursor.t is None or top > cursor.t):
        cursor = Cursor(
            t=top,
            ids=frozenset(
                item.identifier for item in items if parse_timestamp(item.updated_at) == top
            ),
        )
    return AssignmentState(
        seeded=True,
        cursor=cursor,
        observed=observed,
        delivered=set(state.delivered),
    )


# --------------------------------------------------------------------------
# delivery
# --------------------------------------------------------------------------

class Dispatcher(Protocol):
    def deliver(self, route: Route) -> None: ...


def _directive_title(route: Route) -> str:
    if route.to_agent:
        return f"Linear {route.identifier} assigned to you — {route.state}"
    return f"Linear {route.identifier} needs triage — {route.disposition}"


_DISPOSITION_DETAIL = {
    UNASSIGNED: "the card has no assignee",
    UNRESOLVED: "the assignee name is not in the nickname roster",
    NOT_ADDRESSABLE: (
        "the assignee is an external mesh peer, which the roster states is not "
        "reachable via coord-engine tell"
    ),
    AMBIGUOUS: "the assignee name resolves to more than one identity in the roster",
}


def directive_body(route: Route) -> tuple[str, str]:
    """(summary, next_action) for the durable directive."""
    parts = [f"Linear {route.identifier}: {route.title}"]
    if route.url:
        parts.append(f"URL: {route.url}")
    parts.append(f"State: {route.state}")
    parts.append(f"Assignee (Linear display name): {route.assignee or 'unassigned'}")
    if route.previous is None:
        parts.append("First time this card has been observed by the bridge.")
    else:
        prior_assignee, prior_state = route.previous
        parts.append(
            f"Previously observed as assignee={prior_assignee or 'unassigned'} "
            f"state={prior_state}.")
    if not route.to_agent:
        parts.append(f"Routed here for triage because {_DISPOSITION_DETAIL[route.disposition]}.")
    if route.redelivery:
        # Announced, never suppressed. Suppressing it would turn at-least-once
        # into "at most once, silently" — the reader could no longer tell a
        # duplicate from a second real assignment, so they are told which it is.
        parts.append(
            "RE-DELIVERY: this exact (card, assignee, state) has already been "
            "dispatched at least once. Linear assignment delivery is "
            "at-least-once by design; treat this as a repeat, not a new change.")
    parts.append("Sent by coord-tracker-bridge linear-assignments (read-only; zero Linear writes).")
    next_action = (
        "Pick up the card, or reply on the bus if it is not yours."
        if route.to_agent
        else "Triage: assign it to a fleet agent, or add the name to the nickname roster."
    )
    return "\n".join(parts), next_action


@dataclass(frozen=True, slots=True)
class EngineTellDispatcher:
    """Durable directive via `coord-engine tell`. Never a bare bus send.

    `tell` opens an obligation the recipient must discharge, which is the whole
    point: an assignment that vanishes when a session ends is not an assignment.
    """

    team: str
    sender: str
    priority: str = "P2"
    workstream: str = "linear-revival"
    runner: CommandRunner = subprocess_runner
    timeout: float = 60.0
    command: tuple[str, ...] = ("coord-engine",)

    def deliver(self, route: Route) -> None:
        summary, next_action = directive_body(route)
        argv = (
            *self.command, "tell", self.team, route.target, _directive_title(route),
            "-p", self.priority,
            "-w", self.workstream,
            "-s", summary,
            "-n", next_action,
            "--from", self.sender,
        )
        code, stdout, stderr = self.runner(argv, self.timeout)
        if code != 0:
            raise DispatchFailed(
                f"coord-engine tell to {route.target} for {route.identifier} failed "
                f"(rc {code}): {(stderr or stdout).strip()[:300]}")


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------

def render_plan(plan: Plan, *, delivered: int | None = None, note: str = "") -> str:
    lines: list[str] = []
    mode = "delivered" if delivered is not None else "preview"
    lines.append(
        f"linear assignments: {plan.considered} issue(s) read, "
        f"{len(plan.candidates)} past the watermark, {len(plan.routes)} to route "
        f"({mode})")
    if plan.cold_start:
        lines.append(
            "  COLD START: no baseline has ever been recorded, so every card "
            "below reads as a change. Nothing is delivered from a cold start — "
            "run with --seed to adopt the board as the baseline.")
    for route in plan.routes:
        flag = " [RE-DELIVERY]" if route.redelivery else ""
        where = route.target if route.to_agent else f"{route.target} (triage: {route.disposition})"
        lines.append(
            f"  {route.identifier}  {route.state:<12} -> {where}{flag}  {route.title}")
    for identifier in plan.unchanged:
        lines.append(f"  {identifier}  moved, but assignee and state are unchanged — not routed")
    if delivered is not None:
        lines.append(f"  {delivered} directive(s) dispatched.")
    if note:
        lines.append(f"  {note}")
    return "\n".join(lines)


def render_unknown(detail: str) -> str:
    return (
        f"linear assignments: UNKNOWN — {detail}. This is not 'no assignments "
        "changed'; nothing was delivered and the watermark did not move.")


# --------------------------------------------------------------------------
# orchestration
# --------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class Outcome:
    code: int
    text: str


def run_assignments(
    client: LinearClient,
    *,
    team_id: str,
    state_path: str | Path,
    roster_reader: RosterReader,
    coordinator: str,
    dispatcher: Dispatcher | None = None,
    deliver: bool = False,
    do_seed: bool = False,
    cap: int = DEFAULT_DELIVERY_CAP,
) -> Outcome:
    """One pass. Reads always; delivers only when asked; never writes to Linear.

    Exit codes are the `linear-inbox` contract extended by one: 0 succeeded,
    3 UNKNOWN (proves nothing — never "no changes"), 2 a deliberate refusal.
    """
    if deliver and do_seed:
        return Outcome(2, "linear assignments: --deliver and --seed are mutually "
                          "exclusive; seeding adopts a baseline without delivering it")
    if deliver and dispatcher is None:
        return Outcome(2, "linear assignments: --deliver requires a dispatcher")

    result = fetch_inbox(client, team_id)
    if result.unknown:
        return Outcome(3, render_unknown(f"the board read failed ({result.detail})"))

    try:
        state = AssignmentState.load(state_path)
        roster = parse_roster(roster_reader())
        plan = plan_assignments(result.items, state, roster, coordinator=coordinator)
    except AssignmentsUnknown as exc:
        return Outcome(3, render_unknown(str(exc)))

    if do_seed:
        seeded = seed(state, result.items)
        seeded.save(state_path)
        return Outcome(0, (
            f"linear assignments: baseline seeded from {len(result.items)} issue(s); "
            f"watermark {_isoformat(seeded.cursor.t) if seeded.cursor.t else 'unset'}. "
            "Nothing was delivered."))

    if not deliver:
        return Outcome(0, render_plan(plan, note=(
            "Preview only: nothing was delivered and the watermark did not move. "
            "Re-run with --deliver to dispatch.")))

    if plan.cold_start:
        return Outcome(2, (
            f"linear assignments: REFUSING to deliver {len(plan.routes)} directive(s) "
            "from a cold start — with no baseline every card on the board reads as a "
            "change. Run with --seed to adopt the board, then deliver real changes."))

    if len(plan.routes) > cap:
        return Outcome(2, (
            f"linear assignments: REFUSING to deliver {len(plan.routes)} directive(s); "
            f"the cap is {cap}. Nothing was delivered and the watermark did not move. "
            "Raise --delivery-cap deliberately, or --seed past a backlog."))

    handled: list[Candidate] = []
    dispatched: list[Route] = []
    for candidate in plan.candidates:
        if candidate.route is not None:
            try:
                dispatcher.deliver(candidate.route)      # type: ignore[union-attr]
            except DispatchFailed as exc:
                # The prefix before this row IS done, so it is folded and saved;
                # this row and everything after it stays owed. Recording a
                # delivery that did not happen is the transport split this fleet
                # has already been bitten by twice, in the other direction.
                advance(state, handled, dispatched=dispatched).save(state_path)
                return Outcome(3, render_unknown(
                    f"{len(dispatched)} directive(s) went out, then delivery failed "
                    f"at {candidate.route.identifier} ({exc}); the watermark stopped "
                    "there and the rest are still owed"))
            dispatched.append(candidate.route)
        handled.append(candidate)

    advance(state, handled, dispatched=dispatched).save(state_path)
    return Outcome(0, render_plan(plan, delivered=len(dispatched)))


def build_client(api_key: str, transport_factory) -> LinearClient:
    """A client that CANNOT write: same read-only wrapper as `linear-inbox`."""
    return LinearClient(ReadOnlyTransport(transport_factory(api_key)))
