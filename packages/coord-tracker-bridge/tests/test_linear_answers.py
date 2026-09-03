"""The return leg: Linear carries the message, the bus keeps the record.

Ash, 2026-09-03: "Coordination stays on the bus. Linear is a messaging surface."
An ask goes out as a card and the operator's reply comes back in, but the reply
is not an answer because a comment exists — it is an answer when
`coord-engine answer` says so.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coord_tracker_bridge.answers import (
    POSSIBLE_REPEAT,
    Reply,
    AnswerState,
    EngineAnswerDispatcher,
    collect_replies,
    run_answers,
)
from coord_tracker_bridge.model import ManagedRecord, SourceIdentity

BOT = "bot-actor-id"
ASH = "ash-user-id"


def record(
    item_id: str = "needs-a-spend-decision-0000dead",
    *,
    capability="asks",
    closed=False,
    consumer: str | None = "ash",
    lane: str | None = None,
):
    """An ask card. `consumer` is the human it is blocked on — None means the
    card never named one, which must NOT resolve to a default person."""

    fields = {"identifier": "BUS-1"}
    if consumer is not None:
        fields["blocked_on_user"] = consumer
    if lane is not None:
        fields["source_lane"] = lane
    return ManagedRecord(
        provider_id=f"prov-{item_id}",
        source=SourceIdentity("coord-engine", "fulcra/asks", item_id),
        capability=capability,
        fields=fields,
        closed=closed,
    )


def comments(*nodes):
    return lambda issue_id: list(nodes)


def comment(cid, author, body="approved, go ahead", created="2026-09-03T10:00:00Z"):
    return {"id": cid, "body": body, "createdAt": created, "user": {"id": author}}


class Runner:
    """Records argv instead of shelling out."""

    def __init__(self, code: int = 0):
        self.calls: list[tuple[str, ...]] = []
        self.code = code

    def __call__(self, argv, timeout):
        self.calls.append(tuple(argv))
        return self.code, "", "" if self.code == 0 else "boom"


def test_the_bots_own_comment_is_never_an_answer() -> None:
    """It posts confirmations on the cards it reads; trusting them would loop."""

    plan = collect_replies(
        [record()], comments(comment("c1", BOT)), bot_user_id=BOT, state=AnswerState()
    )
    assert plan.replies == ()
    assert plan.considered == 1


def test_an_operator_comment_is_collected() -> None:
    plan = collect_replies(
        [record()], comments(comment("c1", ASH)), bot_user_id=BOT, state=AnswerState()
    )
    assert [r.slug for r in plan.replies] == ["needs-a-spend-decision-0000dead"]


def test_only_ask_cards_are_read() -> None:
    """A task card's comments are conversation, not an answer to an ask."""

    plan = collect_replies(
        [record(capability="tasks")], comments(comment("c1", ASH)),
        bot_user_id=BOT, state=AnswerState(),
    )
    assert plan.replies == ()
    assert plan.cards == 0


def test_a_bare_workspace_ask_is_read_by_its_LANE() -> None:
    """On a plain fulcra-workspaces space an ask is a task document whose lane
    was derived to asks — its capability is honestly still "tasks". Keying on
    capability would work on coord-engine and silently do nothing here."""

    plan = collect_replies(
        [record(capability="tasks", lane="asks")], comments(comment("c1", ASH)),
        bot_user_id=BOT, state=AnswerState(),
    )
    assert plan.cards == 1
    assert [r.slug for r in plan.replies] == ["needs-a-spend-decision-0000dead"]


def test_a_card_naming_no_consumer_is_never_attributed() -> None:
    """Settling it would file the answer under whoever the runner defaults to."""

    plan = collect_replies(
        [record(consumer=None)], comments(comment("c1", ASH)),
        bot_user_id=BOT, state=AnswerState(),
    )
    assert plan.replies == ()
    assert plan.unattributed == ("prov-needs-a-spend-decision-0000dead",)


def test_each_answer_is_attributed_to_ITS_OWN_consumer(tmp_path: Path) -> None:
    """The bug this replaces: one global --human filed every reply under one
    name, so a second consumer's decision was recorded as the first's."""

    runner = Runner()
    path = tmp_path / "s.json"
    AnswerState(seeded=True).save(path)
    records = [
        record("ash-ask-0000dead", consumer="ash"),
        record("liz-ask-0000beef", consumer="liz"),
    ]
    seen: dict[str, list[dict]] = {
        "prov-ash-ask-0000dead": [comment("c1", ASH, created="2026-09-03T10:00:00Z")],
        "prov-liz-ask-0000beef": [comment("c2", "liz-user-id", created="2026-09-03T10:01:00Z")],
    }
    code, _ = run_answers(
        records=records,
        read_comments=lambda issue_id: seen[issue_id],
        bot_user_id=BOT,
        state_path=path,
        dispatcher=EngineAnswerDispatcher(team="fulcra", runner=runner),
        deliver=True,
    )
    assert code == 0
    humans = {argv[3]: argv[argv.index("--human") + 1] for argv in runner.calls}
    assert humans == {"ash-ask-0000dead": "ash", "liz-ask-0000beef": "liz"}


def test_an_unattributable_comment_is_skipped_not_answered() -> None:
    plan = collect_replies(
        [record()], comments({"id": "c1", "body": "x", "createdAt": "", "user": None}),
        bot_user_id=BOT, state=AnswerState(),
    )
    assert plan.replies == ()


def test_cold_start_refuses_to_deliver(tmp_path: Path) -> None:
    """Otherwise the first run replays months of conversation into the bus."""

    runner = Runner()
    code, text = run_answers(
        records=[record()],
        read_comments=comments(comment("c1", ASH)),
        bot_user_id=BOT,
        state_path=tmp_path / "s.json",
        dispatcher=EngineAnswerDispatcher(team="fulcra", runner=runner),
        deliver=True,
    )
    assert code == 2
    assert "COLD START" in text and runner.calls == []


def test_seed_adopts_without_sending(tmp_path: Path) -> None:
    runner = Runner()
    path = tmp_path / "s.json"
    code, _ = run_answers(
        records=[record()],
        read_comments=comments(comment("c1", ASH)),
        bot_user_id=BOT,
        state_path=path,
        dispatcher=EngineAnswerDispatcher(team="fulcra", runner=runner),
        seed=True,
    )
    assert code == 0 and runner.calls == []
    assert AnswerState.load(path).seeded is True


def test_deliver_calls_coord_engine_answer_and_confirms(tmp_path: Path) -> None:
    runner = Runner()
    path = tmp_path / "s.json"
    AnswerState(seeded=True).save(path)
    posted: list[tuple[str, str]] = []
    code, _ = run_answers(
        records=[record()],
        read_comments=comments(comment("c1", ASH, "approve the spend")),
        bot_user_id=BOT,
        state_path=path,
        dispatcher=EngineAnswerDispatcher(team="fulcra", runner=runner),
        post_comment=lambda issue, body: posted.append((issue, body)),
        deliver=True,
    )
    assert code == 0
    argv = runner.calls[0]
    assert argv[:4] == ("coord-engine", "answer", "fulcra", "needs-a-spend-decision-0000dead")
    assert "--with" in argv and "approve the spend" in argv
    assert "--human" in argv
    assert len(posted) == 1
    assert AnswerState.load(path).confirmed == {"c1": "needs-a-spend-decision-0000dead"}


def test_a_settled_comment_is_never_answered_twice(tmp_path: Path) -> None:
    runner = Runner()
    path = tmp_path / "s.json"
    AnswerState(seeded=True, confirmed={"c1": "slug"}).save(path)
    code, text = run_answers(
        records=[record()],
        read_comments=comments(comment("c1", ASH)),
        bot_user_id=BOT,
        state_path=path,
        dispatcher=EngineAnswerDispatcher(team="fulcra", runner=runner),
        deliver=True,
    )
    assert code == 0 and runner.calls == []


def test_an_ambiguous_attempt_announces_possible_re_delivery() -> None:
    """`answer` can settle the ask and then fail to report it."""

    state = AnswerState(seeded=True, attempted={"c1": "slug"})
    plan = collect_replies(
        [record()], comments(comment("c1", ASH)), bot_user_id=BOT, state=state
    )
    assert plan.replies[0].repeat is POSSIBLE_REPEAT


def test_a_failed_dispatch_stops_and_reports_unknown(tmp_path: Path) -> None:
    runner = Runner(code=1)
    path = tmp_path / "s.json"
    AnswerState(seeded=True).save(path)
    code, text = run_answers(
        records=[record()],
        read_comments=comments(comment("c1", ASH)),
        bot_user_id=BOT,
        state_path=path,
        dispatcher=EngineAnswerDispatcher(team="fulcra", runner=runner),
        deliver=True,
    )
    assert code == 3
    assert "UNKNOWN" in text
    # The attempt stays marked: a retry must not present itself as a first send.
    assert AnswerState.load(path).attempted == {"c1": "needs-a-spend-decision-0000dead"}


def test_a_read_failure_is_unknown_never_no_answers(tmp_path: Path) -> None:
    def boom(issue_id):
        raise RuntimeError("board read failed")

    code, text = run_answers(
        records=[record()],
        read_comments=boom,
        bot_user_id=BOT,
        state_path=tmp_path / "s.json",
        dispatcher=EngineAnswerDispatcher(team="fulcra", runner=Runner()),
        deliver=True,
    )
    assert code == 3
    assert "UNKNOWN" in text and "not 'no answers'" in text


def test_a_run_over_the_cap_refuses_whole(tmp_path: Path) -> None:
    runner = Runner()
    path = tmp_path / "s.json"
    AnswerState(seeded=True).save(path)
    many = comments(*(comment(f"c{i}", ASH, created=f"2026-09-03T10:0{i}:00Z") for i in range(4)))
    code, text = run_answers(
        records=[record()],
        read_comments=many,
        bot_user_id=BOT,
        state_path=path,
        dispatcher=EngineAnswerDispatcher(team="fulcra", runner=runner),
        deliver=True,
        cap=2,
    )
    assert code == 2
    assert "REFUSED WHOLE" in text and runner.calls == []


def test_a_failed_confirmation_comment_does_not_unwind_the_answer(tmp_path: Path) -> None:
    """The bus is the record. A missing courtesy note is not a failed answer."""

    runner = Runner()
    path = tmp_path / "s.json"
    AnswerState(seeded=True).save(path)

    def refuse(issue, body):
        raise RuntimeError("comment failed")

    code, _ = run_answers(
        records=[record()],
        read_comments=comments(comment("c1", ASH)),
        bot_user_id=BOT,
        state_path=path,
        dispatcher=EngineAnswerDispatcher(team="fulcra", runner=runner),
        post_comment=refuse,
        deliver=True,
    )
    assert code == 0
    assert AnswerState.load(path).confirmed


# --------------------------------------------------------------------------
# the bare-workspace return leg
# --------------------------------------------------------------------------

from datetime import UTC, datetime  # noqa: E402

from coord_tracker_bridge.answers import (  # noqa: E402
    DispatchFailed,
    WorkspaceInboxDispatcher,
)

FIXED = lambda: datetime(2026, 9, 3, 14, 5, 9, tzinfo=UTC)  # noqa: E731


def reply(**kw):
    base = dict(
        comment_id="c1", issue_id="prov-1", identifier="BUS-127",
        slug="agent-reasoning-tick-needs-a-spend-decision-0000dead",
        author_id=ASH, body="approved, go ahead", created_at="2026-09-03T10:00:00Z",
        consumer="ash", owner="collect-maintainer",
    )
    base.update(kw)
    return Reply(**base)


def test_the_answer_lands_in_the_WAITING_members_inbox() -> None:
    """fulcra-workspaces has no `answer` verb — member/<name>/inbox/ is the whole
    coordination primitive, so on the base convention that IS the return leg."""

    runner = Runner()
    WorkspaceInboxDispatcher(team="fulcra", runner=runner, clock=FIXED).deliver(reply())

    argv = runner.calls[0]
    assert argv[:4] == ("fulcra-api", "file", "upload", argv[3])
    assert argv[4] == (
        "team/fulcra/member/collect-maintainer/inbox/"
        "20260903-140509_linear-bridge_answer-agent-reasoning-tick-needs-a-spend-decision-0000dead.md"
    )


def test_it_delivers_to_the_OWNER_not_the_consumer() -> None:
    """The consumer is the human who decides; the owner is who gets unblocked."""

    runner = Runner()
    WorkspaceInboxDispatcher(team="fulcra", runner=runner, clock=FIXED).deliver(
        reply(consumer="liz", owner="coord-boss")
    )
    assert "/member/coord-boss/inbox/" in runner.calls[0][4]
    assert "/member/liz/" not in runner.calls[0][4]


def test_an_ask_with_no_owner_is_refused_not_guessed() -> None:
    """An answer written to a guessed inbox is worse than one that waits."""

    runner = Runner()
    with pytest.raises(DispatchFailed, match="names no owner"):
        WorkspaceInboxDispatcher(team="fulcra", runner=runner).deliver(reply(owner=None))
    assert runner.calls == []


def test_a_failed_upload_is_a_dispatch_failure() -> None:
    runner = Runner(code=1)
    with pytest.raises(DispatchFailed, match="inbox delivery"):
        WorkspaceInboxDispatcher(team="fulcra", runner=runner, clock=FIXED).deliver(reply())


def test_the_message_names_who_answered_and_which_ask() -> None:
    body = WorkspaceInboxDispatcher(team="fulcra", clock=FIXED).body(reply())
    assert "approved, go ahead" in body
    assert "ash" in body and "BUS-127" in body
    assert "agent-reasoning-tick-needs-a-spend-decision-0000dead" in body
