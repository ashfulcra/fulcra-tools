"""`linear-answers` — carry the operator's replies from Linear back to the bus.

LINEAR IS A MESSAGING SURFACE, NEVER THE COORDINATION AUTHORITY (Ash, 2026-09-03:
"Coordination stays on the bus. Linear is a messaging surface."). An ask goes OUT
as a card; the operator's reply comes back IN and is settled on the bus by
`coord-engine answer`. The card is the envelope, not the record — an ask is not
answered because a comment exists, it is answered when the bus says so.

WHY THIS IS NOT THE INBOUND DIRECTION BUS-24 RULES OUT. BUS-24 (operator-seeded,
ash 2026-07-15) forbids putting WORK into Linear for agents to pick up. This
carries no work: it is the return leg of a loop the fleet itself opened, closing
an obligation the fleet raised. Nothing here can create a task, assign an agent,
or originate a directive; the only bus verb it can reach is `answer`.

THE BOT MUST NOT ANSWER ITSELF. Coord Bridge comments on these cards — that is
how a confirmation gets back to the operator — so a reader that took every
comment as an answer would feed its own confirmations back into the bus forever.
Authorship is checked against the bridge's own actor id, on the comment about to
be processed, not on what the caller meant.

A COLD START REFUSES TO DELIVER. With no state every historical comment reads as
new, and on a board of long-lived asks that replays months of conversation into
`coord-engine answer` in one pass. `--seed` adopts the current comments as the
baseline without sending anything — the same shape as linear-assignments, for the
same reason.

A DISPATCH HAS THREE OUTCOMES. `coord-engine answer` can settle the ask and then
fail to report it, so the attempt is recorded BEFORE the transport runs and a
retry whose comment is still marked says POSSIBLE RE-DELIVERY: not "new", which
under-claims, and not "repeat", which over-claims.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from .model import ManagedRecord
from .source import CommandRunner, sanitize_text, subprocess_runner

OK = "ok"
EMPTY = "empty"
UNKNOWN = "unknown"

#: Outcome of one attempt to settle an answer on the bus.
NEW = "new"
POSSIBLE_REPEAT = "possible-repeat"
CONFIRMED = "confirmed"

#: Bound on one reply carried to the bus. A Linear comment has no length limit
#: worth trusting, and the answer rides an argv.
ANSWER_LIMIT = 4000


class AnswersError(RuntimeError):
    """The run proves nothing. Never rendered as 'no answers'."""


class DispatchFailed(AnswersError):
    """The transport reported failure. Distinct from an UNKNOWN outcome."""


class DispatchRefused(AnswersError):
    """The transport PROVABLY did nothing, and would do nothing on a retry.

    Not the same as DispatchFailed. A failure might have settled the answer and
    then failed to report it, which is why an attempt is marked before the
    transport runs and a retry announces POSSIBLE RE-DELIVERY. A refusal is
    decided BEFORE anything is written — the reply names a row this transport
    has no verb for — so marking it would over-claim forever, and halting the
    run on it would leave every later reply owed for a reason that will never
    change. It is recorded, unmarked, and the run continues.
    """


@dataclass(frozen=True, slots=True)
class Reply:
    """One operator comment on one ask card, not yet settled on the bus."""

    comment_id: str
    issue_id: str
    identifier: str
    slug: str
    author_id: str
    body: str
    created_at: str
    #: The human this card was blocked on — whose answer this therefore is.
    #: Carried per-reply, never taken from a global default, because with more
    #: than one consumer a global handle files one person's decision under
    #: another's name.
    consumer: str
    #: The agent waiting on this answer. Distinct from `consumer`: the
    #: consumer is the human who decides, the owner is who gets unblocked.
    #: A bare workspace has no `answer` verb, so the reply is delivered to
    #: this member's inbox — the base convention's only messaging primitive.
    owner: str | None = None
    repeat: str = NEW

    @property
    def fingerprint(self) -> str:
        """Stable identity of this reply, independent of when it was read."""

        return self.comment_id


@dataclass(slots=True)
class AnswerState:
    """Durable record of which comments have been carried to the bus.

    `attempted` is written before a dispatch and cleared only by a CONFIRMED
    outcome, so an ambiguous attempt is never silently retried as a first send.
    """

    seeded: bool = False
    confirmed: dict[str, str] = field(default_factory=dict)
    attempted: dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> AnswerState:
        if not path.exists():
            return cls()
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise AnswersError("answer state root must be an object")
        return cls(
            seeded=bool(raw.get("seeded", False)),
            confirmed=dict(raw.get("confirmed") or {}),
            attempted=dict(raw.get("attempted") or {}),
        )

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {"seeded": self.seeded, "confirmed": self.confirmed, "attempted": self.attempted},
            sort_keys=True, indent=2,
        ) + "\n"
        fd, staged = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(staged, path)
        finally:
            try:
                os.unlink(staged)
            except FileNotFoundError:
                pass

    def classify(self, comment_id: str) -> str | None:
        """None once settled; otherwise how a send should announce itself."""

        if comment_id in self.confirmed:
            return None
        return POSSIBLE_REPEAT if comment_id in self.attempted else NEW


#: The lane an operator-blocking row projects into, on either substrate.
ASKS_LANE = "asks"


def is_ask(record: ManagedRecord) -> bool:
    """Is this card an operator ask, on either substrate?

    coord-engine gives it capability "asks"; a bare workspace gives it a task
    document with lane derived to asks. Either is an ask.
    """

    lane = str((record.fields or {}).get("source_lane") or "").strip()
    return lane == ASKS_LANE or record.capability == ASKS_LANE


def consumer_of(record: ManagedRecord) -> str | None:
    """WHICH human this card is blocked on, or None when unresolved.

    None is never "the default human": an answer attributed to the wrong person
    is worse than an answer that waits for triage.
    """

    value = str((record.fields or {}).get("blocked_on_user") or "").strip()
    return value or None


@dataclass(frozen=True, slots=True)
class Plan:
    replies: tuple[Reply, ...]
    considered: int
    cards: int
    cold_start: bool
    #: Cards carrying a reply we refused to attribute — no consumer named.
    unattributed: tuple[str, ...] = ()


def collect_replies(
    records: Iterable[ManagedRecord],
    read_comments,
    *,
    bot_user_id: str,
    state: AnswerState,
) -> Plan:
    """Gather operator replies on ask cards. A read failure raises, never empties."""

    replies: list[Reply] = []
    unattributed: list[str] = []
    considered = 0
    cards = 0
    for record in records:
        # THE READER MUST MATCH THE VIEW. A card is answerable because it
        # NAMES A HUMAN — the same rule that put it in that human's "blocked on
        # me" view. Keying this on the asks fold alone made the view hold 27
        # cards while the reader saw 13, so two replies the operator left sat
        # unread on cards he was looking at, and every "it's fixed" report
        # measured the fold he wasn't reading.
        #
        # An ask that names NOBODY is still read, and read for a different
        # reason: it is in the triage view, a person can reply there, and that
        # reply must surface as unattributed rather than vanish. Read is not
        # answerable — nothing without a consumer is ever attributed below.
        if record.closed or (consumer_of(record) is None and not is_ask(record)):
            continue
        cards += 1
        for node in read_comments(record.provider_id):
            considered += 1
            comment_id = str(node.get("id") or "").strip()
            user = node.get("user")
            author = str((user or {}).get("id") or "").strip()
            if not comment_id or not author:
                # A comment we cannot attribute is not evidence that the operator
                # said anything. Skipping it is safe; answering it is not.
                continue
            if author == bot_user_id:
                continue
            repeat = state.classify(comment_id)
            if repeat is None:
                continue
            body = sanitize_text(node.get("body"), limit=ANSWER_LIMIT).strip()
            if not body:
                continue
            consumer = consumer_of(record)
            if consumer is None:
                # The card never named whose decision this is. Settling it would
                # attribute the answer to whoever the runner happens to default
                # to, so it waits for triage instead.
                unattributed.append(record.provider_id)
                continue
            replies.append(Reply(
                comment_id=comment_id,
                issue_id=record.provider_id,
                identifier=str(record.fields.get("identifier") or record.provider_id),
                slug=record.source.item_id,
                author_id=author,
                body=body,
                created_at=str(node.get("createdAt") or ""),
                consumer=consumer,
                owner=str((record.fields or {}).get("owner") or "").strip() or None,
                repeat=repeat,
            ))
    replies.sort(key=lambda item: (item.created_at, item.comment_id))
    cold = not state.seeded and not state.confirmed and not state.attempted
    return Plan(tuple(replies), considered, cards, cold, tuple(dict.fromkeys(unattributed)))


def parse_answerable_fold(raw: str, *, consumer: str) -> frozenset[str]:
    """Turn the engine's `asks --json` output into the set of answerable slugs.

    PARSE HERE, ONCE, RATHER THAN NARROWING AT EACH USE. The first version of
    this walked the decoded JSON with `isinstance` checks inline and skipped
    anything that did not match — which is the drop this package already ruled
    out for the Linear transport ("A TRANSPORT DOES NOT GET TO DECIDE THAT DATA
    LOSS IS ACCEPTABLE", linear.py). It is worse here than there: a dropped row
    means its slug is missing from this set, so a reply on it is REFUSED and
    reported as "no verb reaches this row" — a false statement about the bus,
    printed to the operator whose reply is now sitting undelivered.

    So every shape this cannot read raises. A malformed fold is a failed read,
    and a failed read is UNKNOWN; it is never a smaller fold.
    """

    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise DispatchFailed(
            f"the answerable fold for {consumer!r} was not JSON: {exc}"
        ) from None
    if not isinstance(payload, Mapping):
        raise DispatchFailed(
            f"the answerable fold for {consumer!r} is not an object"
        )
    if "rows" not in payload:
        # NO DEFAULT. An absent `rows` is not an empty fold — it would refuse
        # every reply and read as "nothing here is answerable", which is the
        # same lie as rendering a failed read as "no answers".
        raise DispatchFailed(
            f"the answerable fold for {consumer!r} has no rows field"
        )
    rows = payload["rows"]
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        raise DispatchFailed(
            f"the answerable fold for {consumer!r} has a non-list rows field"
        )
    slugs: set[str] = set()
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise DispatchFailed(
                f"row {index} of the answerable fold for {consumer!r} is not an object"
            )
        slug = row.get("id")
        if not isinstance(slug, str) or not slug.strip():
            raise DispatchFailed(
                f"row {index} of the answerable fold for {consumer!r} has no usable id"
            )
        slugs.add(slug.strip())
    return frozenset(slugs)


@dataclass(slots=True)
class EngineAnswerDispatcher:
    """Settle one ask on the bus via `coord-engine answer`.

    The ONLY bus verb this module can reach. It unblocks the ask and hands it
    back to its owner; it cannot create work.

    THE VIEW IS WIDER THAN THIS VERB. A card reaches a person's "blocked on me"
    view by naming them, in any lane; `answer` settles only the rows the engine
    holds in its waiting-for-operator ask fold. Measured live 2026-09-04: 30
    cards in the view, 11 answerable. So a reply on a directive that names the
    same human is a row this dispatcher HAS NO VERB FOR, and it says so by
    refusing up front rather than by failing after the fact — a pre-flight read
    of the fold, never a parse of an error message.
    """

    team: str
    runner: CommandRunner = subprocess_runner
    timeout: float = 60.0
    command: tuple[str, ...] = ("coord-engine",)
    #: consumer -> the slugs the engine will accept an answer for. Read once
    #: per consumer per run; a run is short and the fold does not move under it.
    _answerable: dict[str, frozenset[str]] = field(default_factory=dict)

    def answerable(self, consumer: str) -> frozenset[str]:
        """Which of this consumer's rows `coord-engine answer` will accept.

        A read failure raises rather than returning an empty set: an empty set
        would refuse every reply and read as "nothing was answerable", which is
        the same lie as rendering a failed read as "no answers".
        """

        if consumer in self._answerable:
            return self._answerable[consumer]
        argv = (*self.command, "asks", self.team, "--human", consumer, "--json")
        code, stdout, stderr = self.runner(argv, self.timeout)
        if code != 0:
            raise DispatchFailed(
                f"reading the answerable fold for {consumer!r} failed "
                f"(rc {code}): {(stderr or stdout).strip()[:300]}"
            )
        found = parse_answerable_fold(stdout, consumer=consumer)
        self._answerable[consumer] = found
        return found

    def deliver(self, reply: Reply) -> None:
        if reply.slug not in self.answerable(reply.consumer):
            raise DispatchRefused(
                f"{reply.slug} is not a waiting-for-operator ask for "
                f"{reply.consumer!r}; `coord-engine answer` has no verb for it"
            )
        argv = (
            *self.command, "answer", self.team, reply.slug,
            "--with", reply.body,
            "--human", reply.consumer,
        )
        code, stdout, stderr = self.runner(argv, self.timeout)
        if code != 0:
            raise DispatchFailed(
                f"coord-engine answer for {reply.slug} failed (rc {code}): "
                f"{(stderr or stdout).strip()[:300]}"
            )


def confirmation_body(reply: Reply) -> str:
    """What Coord Bridge says back on the card once the bus has the answer."""

    return (
        "Carried to the bus — this ask is settled and handed back to its owner.\n\n"
        f"`coord-engine answer` accepted your reply for `{reply.slug}`.\n\n"
        "The bus is the record; this card is the envelope. It closes on the next "
        "sync once the row leaves the asks lane."
    )


def render(plan: Plan, *, delivered: int | None = None, note: str = "") -> str:
    mode = "delivered" if delivered is not None else "preview"
    lines = [
        f"linear answers: {plan.cards} open ask card(s), {plan.considered} comment(s) read, "
        f"{len(plan.replies)} to carry ({mode})"
    ]
    if plan.cold_start:
        lines.append(
            "  COLD START: no baseline has ever been recorded, so every comment "
            "below reads as new. Nothing is delivered from a cold start — run "
            "with --seed to adopt the current comments as the baseline."
        )
    for reply in plan.replies:
        flag = " [POSSIBLE RE-DELIVERY]" if reply.repeat is POSSIBLE_REPEAT else ""
        first = reply.body.splitlines()[0] if reply.body else ""
        lines.append(f"  {reply.identifier}  {reply.slug[:44]}{flag}")
        lines.append(f"      {first[:96]}")
    if note:
        lines.append(f"  {note}")
    return "\n".join(lines)


def unknown(detail: str) -> str:
    return (
        f"linear answers: UNKNOWN — {detail}. This is not 'no answers'; nothing "
        "was carried to the bus and no comment was marked."
    )


def run_answers(
    *,
    records: Sequence[ManagedRecord],
    read_comments,
    bot_user_id: str,
    state_path: Path,
    dispatcher: EngineAnswerDispatcher,
    post_comment=None,
    deliver: bool = False,
    seed: bool = False,
    cap: int = 25,
) -> tuple[int, str]:
    """Carry operator replies to the bus. Returns (exit code, rendered report).

    Exit codes extend the linear-inbox contract: 0 succeeded, 3 UNKNOWN (proves
    nothing — never "no answers"), 2 a deliberate refusal.
    """

    state = AnswerState.load(state_path)
    try:
        plan = collect_replies(
            records, read_comments, bot_user_id=bot_user_id, state=state
        )
    except Exception as exc:  # a partial read cannot authorize an answer
        return 3, unknown(f"reading ask comments failed ({type(exc).__name__}: {exc})")

    if seed:
        for reply in plan.replies:
            state.confirmed[reply.comment_id] = "seeded"
        state.seeded = True
        state.save(state_path)
        return 0, render(plan, note=(
            f"SEEDED: {len(plan.replies)} existing comment(s) adopted as the "
            "baseline. Nothing was carried to the bus."))

    if not deliver:
        return 0, render(plan, note="preview only — pass --deliver to carry these to the bus")

    if plan.cold_start:
        return 2, render(plan, note=(
            "REFUSED: cold start. Delivering now would replay every historical "
            "comment as an answer. Run with --seed first."))

    if len(plan.replies) > cap:
        return 2, render(plan, note=(
            f"REFUSED WHOLE: {len(plan.replies)} replies exceeds --delivery-cap "
            f"{cap}. Nothing was carried; raise the cap deliberately."))

    carried = 0
    refused: list[str] = []
    for reply in plan.replies:
        # Written BEFORE the transport: `coord-engine answer` can settle the ask
        # and then fail to report it, so a raise is not evidence nothing landed.
        state.attempted[reply.comment_id] = reply.slug
        state.save(state_path)
        try:
            dispatcher.deliver(reply)
        except DispatchRefused as exc:
            # PROVABLY nothing was written, and a retry changes nothing. Unmark
            # it — a mark means "this might have landed", and leaving one here
            # makes every future run announce POSSIBLE RE-DELIVERY for an answer
            # that never went anywhere. Then keep going: the refusal is about
            # THIS row, and halting would owe every later reply for a reason
            # that has nothing to do with them.
            state.attempted.pop(reply.comment_id, None)
            state.save(state_path)
            refused.append(f"{reply.identifier}: {exc}")
            continue
        except Exception as exc:
            return 3, render(plan, delivered=carried, note=(
                f"STOPPED at {reply.identifier} ({exc}) — that one's outcome is "
                "UNKNOWN and stays marked; everything after it is still owed."))
        state.confirmed[reply.comment_id] = reply.slug
        state.attempted.pop(reply.comment_id, None)
        state.save(state_path)
        carried += 1
        if post_comment is not None:
            try:
                post_comment(reply.issue_id, confirmation_body(reply))
            except Exception:
                # The answer IS on the bus; a missing courtesy comment must not
                # unwind it or stop the run. The bus is the record.
                pass
    if refused:
        # Exit 2, not 0: replies the operator wrote are still undelivered, and a
        # run that returned success would report that as "done".
        return 2, render(plan, delivered=carried, note=(
            f"REFUSED {len(refused)} repl(y/ies) — no verb reaches these rows, "
            "and nothing was marked, so a later run with a delivery path for "
            "them starts clean:\n      " + "\n      ".join(refused)))
    return 0, render(plan, delivered=carried)


@dataclass(frozen=True, slots=True)
class WorkspaceInboxDispatcher:
    """Deliver an answer into the waiting member's inbox on a bare workspace.

    `fulcra-workspaces` has no `answer` verb and no engine — its whole
    coordination primitive is `member/<name>/inbox/`, where others drop tasks and
    messages for that member to pick up. So on the base convention the return leg
    IS an inbox drop, and this is the sibling of EngineAnswerDispatcher for a
    space with no coord-engine installed.

    It still cannot create work: it writes one message into the inbox of the
    member who was already waiting on this ask. It cannot address anybody else,
    and it names no other member.
    """

    team: str
    sender: str = "linear-bridge"
    runner: CommandRunner = subprocess_runner
    timeout: float = 60.0
    command: tuple[str, ...] = ("fulcra-api",)
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)

    def _path(self, reply: Reply) -> str:
        stamp = self.clock().strftime("%Y%m%d-%H%M%S")
        slug = re.sub(r"[^a-z0-9-]+", "-", reply.slug.lower()).strip("-")[:60]
        return (
            f"team/{self.team}/member/{reply.owner}/inbox/"
            f"{stamp}_{self.sender}_answer-{slug}.md"
        )

    def body(self, reply: Reply) -> str:
        return (
            f"# Answer from {reply.consumer}\n\n"
            f"You were blocked on `{reply.consumer}`. They have replied in Linear "
            f"on {reply.identifier}.\n\n"
            f"---\n\n{reply.body}\n\n---\n\n"
            f"- ask: `{reply.slug}`\n"
            f"- answered by: `{reply.consumer}`\n"
            f"- carried by: `{self.sender}`\n"
        )

    def deliver(self, reply: Reply) -> None:
        if not reply.owner:
            # No member to deliver to. Refusing is the point: an answer written
            # to a guessed inbox is worse than one that waits for triage.
            raise DispatchFailed(
                f"ask {reply.slug} names no owner, so there is no inbox to answer into"
            )
        with tempfile.NamedTemporaryFile(
            "w", suffix=".md", encoding="utf-8", delete=False
        ) as handle:
            handle.write(self.body(reply))
            staged = handle.name
        try:
            argv = (*self.command, "file", "upload", staged, self._path(reply))
            code, stdout, stderr = self.runner(argv, self.timeout)
            if code != 0:
                raise DispatchFailed(
                    f"inbox delivery for {reply.slug} to {reply.owner} failed "
                    f"(rc {code}): {(stderr or stdout).strip()[:300]}"
                )
        finally:
            try:
                os.unlink(staged)
            except FileNotFoundError:
                pass
