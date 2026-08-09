"""Activity-implies-liveness must cover EVERY write verb, not a curated list.

The chokepoint's own comment promises "no verb can be missed and none has to
opt in". It was implemented as a hand-maintained ALLOWLIST of 13 functions, so
verbs could be missed — and twenty were, including `review close`, `escalate`,
`continuity snapshot`/`park`, `roles claim`/`release`, `answer`, `bus-v3 send`
and `stash push`.

Measured consequence (2026-08-09, live store): codex-reviewer rendered
`stale 6d — nudge` while having filed a verdict 3.5h earlier, and
coord-opus-worker rendered `stale 42h — nudge` having filed a report 4.8h
earlier. An agent whose work IS reviewing and maintaining — filing verdicts,
closing reviews, claiming its role, saving continuity — was invisible to the
signal that decides whether to go poke it.

So the rule is inverted here: a verb refreshes presence UNLESS it is a declared
read. This test is the thing that keeps the promise honest — a newly added
write verb is covered by default, and a newly added READ verb must be declared
in `_ACTIVITY_READ_FUNCS`, which is a decision someone makes deliberately
rather than an omission nobody notices.
"""

from __future__ import annotations

import re
import pathlib

from coord_engine import cli

SRC = pathlib.Path(cli.__file__).read_text()

#: Verbs that persist something yet must NOT count as activity. Each needs a
#: reason, because every name here is a hole this test agrees not to look at.
EXPECTED_READ_ONLY = {
    # W1 owns this write — it IS the presence shard. Routing it through the
    # activity path would double-write and let the throttle suppress a
    # deliberate beat.
    "cmd_presence_beat",
    # Semantically a read that happens to memoise: it writes only the `.settled`
    # tally CACHE, which is recomputable from the verdict shards and is not the
    # actor's work product. Asking "what is the state of this review" must never
    # become evidence that the asker did anything — that is precisely the
    # confusion this whole change exists to remove. (It is also the write that
    # once clobbered a MERGED marker, which is tracked separately as a
    # cache-vs-evidence problem, not a liveness one.)
    "cmd_review_status",
}


def _writes(name: str) -> bool:
    """Does this command's body persist anything? Source-derived, not guessed."""
    m = re.search(rf"^def {re.escape(name)}\(.*?(?=^def )", SRC, re.M | re.S)
    body = m.group(0) if m else ""
    return bool(re.search(r"transport\.write|transport\.delete|emit_event"
                          r"|_write_settled|_clear_settled", body))


def _command_funcs() -> set[str]:
    return set(re.findall(r"^def (cmd_\w+)", SRC, re.M))


def test_every_write_verb_refreshes_presence():
    """The promise, enforced. Any write verb outside the refresh path is a
    liveness hole: the agent does real work and still renders as dark."""
    missed = sorted(
        name for name in _command_funcs()
        if _writes(name)
        and name not in EXPECTED_READ_ONLY
        and getattr(cli, name, None) not in cli.ACTIVITY_REFRESH_FUNCS
    )
    assert not missed, (
        "these verbs persist to the store but do not refresh the actor's "
        "presence, so an agent doing this work renders stale and gets a "
        f"'nudge' it has not earned: {missed}")


def test_declared_read_verbs_do_not_refresh():
    """The other direction: a read must never manufacture liveness. Reading the
    board is not evidence anyone is working."""
    for name in ("cmd_briefing", "cmd_needs_me", "cmd_presence_show",
                 "cmd_review_status"):
        fn = getattr(cli, name, None)
        if fn is None:
            continue
        assert fn not in cli.ACTIVITY_REFRESH_FUNCS, (
            f"{name} is a read verb; refreshing presence from it would make "
            "'someone looked at the board' indistinguishable from 'someone did "
            "the work'")


def test_presence_beat_is_not_an_activity_refresh():
    """`presence beat` writes the shard itself (W1). Routing it through the
    activity path would double-write and let the throttle memo suppress a
    deliberate beat."""
    assert cli.cmd_presence_beat not in cli.ACTIVITY_REFRESH_FUNCS
