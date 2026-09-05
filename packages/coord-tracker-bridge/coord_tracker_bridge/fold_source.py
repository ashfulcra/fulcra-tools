"""Read the v4 signal plane: a per-consumer fold is the "blocked on me" set.

WHY THIS REPLACES THE v3 READER RATHER THAN JOINING IT. Nearly every defect this
lane spent a week on was the v3 read model showing through:

  * The teams reader was O(every task document ever) -- measured 2026-09-05, it
    opened 3,505 files and returned NOTHING inside 600 seconds. A fold is
    O(new events) from a durable cursor and reads only the `ptr` of rows that
    are actually open.
  * The reader and the operator's view were decided by two different rules, so
    the view held 27 cards and the reader saw 13. Here there is ONE set -- the
    fold's `open` -- and the reader and the view are the same object by
    construction, so they cannot drift.
  * v3 identity embedded the FOLD a row came from, so one row surfaced in two
    folds became two cards. A v4 slug is the row; there is no fold in it.
  * The v3 settle verb accepted only a subset of what the view showed. `close`
    operates on exactly what is in `open`.

WHAT A HUMAN'S FOLD IS. The event filter is `to in (agent, "all") or from ==
agent`, so folding as `ash` yields precisely the obligations addressed to Ash.
That IS the "blocked on me" set -- not a query over it, and not a lane
convention that some rows happen to follow.

THE BRIDGE FOLDS ON THE HUMAN'S BEHALF. A person does not run a CLI, so nobody
advances a human identity's cursor; measured 2026-09-05, `ash` had never folded
while coord-maintainer (194 open) and coord-boss (339 open) were current to the
minute. coord-fold's generation guard means a SECOND folder for the same
identity is refused by name rather than silently interleaved, so this is safe to
own -- and `read_only=True` keeps it a pure reader where something else already
folds for that identity.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Sequence

from .model import CapabilityState, Diagnostic, Snapshot, SourceIdentity, WorkRecord
from .source import sanitize_text

#: Every row in a consumer's fold is something waiting on that consumer, so the
#: lane is not derived from row shape -- it is what the fold MEANS.
ASKS_LANE = "asks"
CAPABILITY = "asks"

#: coord-fold priorities are already P-levels; the projection policy maps them.
_DEFAULT_PRIORITY = "P2"


class FoldSourceError(RuntimeError):
    """The fold did not answer. Never rendered as an empty fold."""


@dataclass(frozen=True, slots=True)
class FoldRow:
    """One open obligation, parsed at the boundary rather than scraped.

    The CLI renders `open` as text; this reads the checkpoint STRUCTURE instead.
    Parsing a rendered line would be the same mistake as every other silent-drop
    in this package -- a row whose rendering changed would read as absent.
    """

    slug: str
    pri: str
    sender: str
    ptr: str
    at: str
    claimed_by: str | None = None

    @classmethod
    def parse(cls, slug: str, raw: object) -> FoldRow:
        if not isinstance(raw, Mapping):
            raise FoldSourceError(f"fold row {slug!r} is not an object")
        missing = [k for k in ("pri", "from", "ptr", "at") if not raw.get(k)]
        if missing:
            # NOT a skip. A row we cannot read is not a row that is not owed,
            # and dropping it here would quietly shrink the operator's queue.
            raise FoldSourceError(f"fold row {slug!r} is missing {missing}")
        return cls(
            slug=slug,
            pri=str(raw["pri"]),
            sender=str(raw["from"]),
            ptr=str(raw["ptr"]),
            at=str(raw["at"]),
            claimed_by=str(raw["claimed_by"]) if raw.get("claimed_by") else None,
        )


def parse_open(state: Mapping[str, Any]) -> tuple[FoldRow, ...]:
    """Every open row in a checkpoint, or raise. Never a partial set."""

    rows = state.get("open")
    if rows is None:
        # NO DEFAULT. An absent `open` is not an empty fold: it would read as
        # "nothing is blocked on you", which is the most misleading thing this
        # bridge can say.
        raise FoldSourceError("checkpoint has no open set")
    if not isinstance(rows, Mapping):
        raise FoldSourceError("checkpoint open set is not an object")
    return tuple(FoldRow.parse(str(slug), raw) for slug, raw in sorted(rows.items()))


#: The store root every team-relative pointer hangs off.
_TEAM_ROOT = "team/{team}/"


def resolve_pointer(team: str, ptr: str) -> str:
    """Where a row's `ptr` actually lives in the store.

    POINTERS ARE TEAM-RELATIVE IN PRACTICE. Measured on the live v4 plane
    2026-09-05: emitters write `task/<slug>.md`, and the store holds it at
    `team/fulcra/task/<slug>.md` -- reading the pointer verbatim gets `absent`
    for every row. That failure is quiet in the worst way: `absent` is a
    legitimate answer meaning "no such document", so a snapshot built from
    unresolved pointers stays COMPLETE and simply carries 339 cards with no
    title and no body. Blank cards on a person's board, with nothing in the run
    that looks wrong.

    A pointer that already names the team root is passed through, so an emitter
    writing the fully-qualified path is not double-prefixed.
    """

    root = _TEAM_ROOT.format(team=team)
    if ptr.startswith(root) or ptr.startswith("/"):
        return ptr
    return f"{root}{ptr}"


def _title_from(body: str | None, slug: str) -> tuple[str, str]:
    """(title, description) for one row, from its pointer document.

    A pointer that will not read is NOT a blank card. The row is real -- the
    fold says so -- so it keeps its slug as a title and says plainly that the
    document could not be read, rather than appearing as an empty card the
    operator cannot act on or explain.
    """

    if body is None:
        return (slug, "The pointer for this row could not be read; the row itself is real.")
    text = sanitize_text(body, limit=8000)
    lines = [line.strip() for line in text.splitlines()]
    heading = next(
        (line.lstrip("# ").strip() for line in lines if line.startswith("#")), ""
    )
    if not heading:
        heading = next((line for line in lines if line and not line.startswith("---")), slug)
    return (heading[:240] or slug, text)


@dataclass(slots=True)
class FoldSourceAdapter:
    """A consumer's fold, as a projection source.

    `consumer` is the identity whose fold this is -- the human the rows are
    blocked on. One adapter serves one consumer; a deployment with several reads
    several, which is the honest shape because a fold IS per identity.
    """

    provider: str = field(default="coord-fold", init=False)

    team: str = "fulcra"
    consumer: str = "ash"
    #: Injected so tests drive real code rather than mocking a module. Given
    #: None, the caller must supply reader/writer explicitly -- this adapter
    #: never reaches for a global CLI, because on this very host `fulcra-api`
    #: resolves to a BROKEN build inside the repo venv that shadows the working
    #: one, and every read then fails as an unexplained "error" (measured
    #: 2026-09-05). Which binary talks to the store is a caller's decision.
    reader: Any = None
    writer: Any = None
    #: True: read the checkpoint someone else advances. False: advance it here.
    #: A human identity has no other folder, so the bridge is normally the one
    #: that folds; coord-fold's generation guard refuses a second folder by name
    #: rather than interleaving, so this is safe either way.
    read_only: bool = False
    max_events: int = 5000
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)

    @property
    def source_id(self) -> str:
        return f"{self.provider}:{self.team}:{self.consumer}"

    def _state(self) -> tuple[Mapping[str, Any], list[Diagnostic], bool]:
        """The consumer's checkpoint, its diagnostics, and whether it is whole."""

        from coord_fold import channel, checkpoint, fold  # local: optional dep
        from coord_fold.transport import TransportUnavailable

        diagnostics: list[Diagnostic] = []
        if self.read_only:
            state, source = checkpoint.load(self.reader, self.team, self.consumer)
            if source == "error":
                raise FoldSourceError(f"{self.consumer}'s checkpoint did not answer")
            if source == "corrupt":
                raise FoldSourceError(f"{self.consumer}'s checkpoint is corrupt")
            if source == "fresh":
                # NEVER an empty fold. Nobody has folded for this identity, so
                # "no open rows" is unknown, not measured -- and projecting it
                # as empty would close every card the operator has.
                raise FoldSourceError(
                    f"{self.consumer} has never folded; read_only cannot bootstrap it"
                )
            return state, diagnostics, True

        import uuid

        now = self.clock().strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            outcome = fold.run(
                self.reader, self.writer, self.team, self.consumer,
                now=now, writer_id=f"linear-bridge:{uuid.uuid4().hex[:8]}",
                max_events=self.max_events,
            )
        except fold.FoldContended as exc:
            # Something else folds for this identity. Two writers on one
            # checkpoint is exactly what the generation guard exists to catch.
            raise FoldSourceError(f"another folder owns {self.consumer}: {exc}") from None
        except (fold.FoldRefused, channel.ChannelUnresolved) as exc:
            raise FoldSourceError(f"fold refused for {self.consumer}: {exc}") from None
        except TransportUnavailable as exc:
            raise FoldSourceError(f"fold did not complete for {self.consumer}: {exc}") from None

        whole = True
        if outcome.unread:
            # A remainder is rc 0 for coord-fold and it is NOT completeness for
            # this bridge: rows may still be closed in the events we did not
            # reach, and an absence-close on that reads a backlog as a deletion.
            whole = False
            diagnostics.append(Diagnostic(
                CAPABILITY, "fold-remainder",
                f"{outcome.unread} event(s) not applied; the next pass gets them",
            ))
        for slug in outcome.state.get("unreadable_pointers", ()):
            whole = False
            diagnostics.append(Diagnostic(CAPABILITY, "fold-pointer-unreadable", str(slug)))
        return outcome.state, diagnostics, whole

    def snapshot(self) -> Snapshot:
        """Every row blocked on this consumer, as projectable work.

        Completeness is deliberately strict. `complete` is what licenses an
        absence-close, and this package has already queued 52 live cards for
        closing once by treating a partial read as authoritative. A remainder or
        an unreadable pointer means some row's state is unknown, so absence
        proves nothing this pass.
        """

        state, diagnostics, whole = self._state()
        rows = parse_open(state)

        items: list[WorkRecord] = []
        for row in rows:
            body, read_state = self.reader.read_classified(
                resolve_pointer(self.team, row.ptr)
            )
            if read_state == "error":
                # Distinguish "the document says nothing" from "the read did not
                # answer". Only the second costs us completeness.
                whole = False
                diagnostics.append(Diagnostic(CAPABILITY, "pointer-unreadable", row.slug))
                body = None
            title, description = _title_from(body, row.slug)
            items.append(WorkRecord(
                source=SourceIdentity(self.provider, f"{self.team}/{self.consumer}", row.slug),
                capability=CAPABILITY,
                title=title,
                lane=ASKS_LANE,
                priority=row.pri if row.pri else _DEFAULT_PRIORITY,
                description=description,
                # The agent that opened it, and therefore the one the answer is
                # owed back to. Distinct from the consumer, who decides.
                owner=row.sender,
                assignee=row.claimed_by,
                origin="fleet",
                # By CONSTRUCTION, not by convention: this is that consumer's
                # fold, so every row in it is blocked on them.
                blocked_on_user=self.consumer,
            ))

        capabilities = {
            CAPABILITY: CapabilityState.COMPLETE if whole else CapabilityState.DEGRADED,
        }
        return Snapshot(tuple(items), whole, tuple(diagnostics), capabilities, self.clock())
