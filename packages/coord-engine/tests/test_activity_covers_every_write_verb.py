"""Every REGISTERED command must be classified read-or-write, deliberately.

History, because it explains the shape:

1. The chokepoint's comment promised "no verb can be missed and none has to opt
   in" while implementing an ALLOWLIST of thirteen functions. Twenty write verbs
   had drifted outside it, so an agent whose job IS reviewing rendered
   `stale — nudge` while working.
2. Inverting to a denylist fixed that direction and opened the other:
   codex-reviewer, 590 r1, found `headroom`, `route`, `atc report`,
   `annotate status` and `threads` — all READS living in extracted modules —
   refreshing presence through the default-true predicate. Merely looking at a
   dashboard manufactured liveness, which suppresses the nudge for an agent who
   really is gone.
3. The r1 coverage test could not see them: it regexed `Path(cli.__file__)`, so
   the extracted modules were invisible to the very check meant to be
   exhaustive.

Two rules follow, and they are why nothing here decides anything by scanning
source:

  - Enumerate the REAL registered surface by walking the argparse tree, not one
    file.
  - Require every command to appear in exactly one table below. A regex CANNOT
    classify these: `tell`, `reconcile` and `task restore` all persist through
    helpers and show no `transport.write` in their own bodies, so a source-scan
    classifier is confidently wrong about them. Classification is a judgement,
    so it is written down as one.
"""

from __future__ import annotations

import argparse

from coord_engine import cli

#: Pure views. Running one is NOT evidence anyone did work, so it must never
#: refresh presence.
EXPECTED_READS = {
    "cmd_status", "cmd_board", "cmd_search", "cmd_needs_me", "cmd_briefing",
    "cmd_presence_show", "cmd_health", "cmd_doctor",
    "cmd_obligations", "cmd_roles_status", "cmd_continuity_resume",
    "cmd_agents", "cmd_asks", "cmd_engagement_gate", "cmd_stash_list",
    "cmd_router_shadow_status",
    # extracted modules — the class codex-reviewer caught on 590 r1
    "cmd_headroom", "cmd_route", "cmd_atc_report", "cmd_dash",
    "cmd_annotate_status", "cmd_threads",
    # `presence beat` writes this very shard; W1 owns that write.
    "cmd_presence_beat",
    # A read that memoises: `review status` writes only the recomputable
    # `.settled` tally cache. Asking a review's state must not become evidence
    # that the asker did anything.
    "cmd_review_status",
}

#: Commands that persist something the actor is accountable for. Running one IS
#: evidence of work.
EXPECTED_WRITES = {
    "cmd_tell", "cmd_respond", "cmd_answer", "cmd_escalate", "cmd_broadcast",
    "cmd_intent", "cmd_later", "cmd_remind",
    "cmd_reconcile", "cmd_acceptance_pair",
    "cmd_task_start", "cmd_task_update", "cmd_task_block", "cmd_task_pause",
    "cmd_task_abandon", "cmd_task_assign", "cmd_task_restore", "cmd_task_done",
    "cmd_task_supersede",
    "cmd_review_request", "cmd_review_restore", "cmd_review_close",
    "cmd_review_conclude",
    "cmd_review_gc",
    # Added 2026-08-10. This test flagged it as unclassified the moment the verb
    # existed, which is the whole design: filing a verdict now counts as
    # activity by default rather than by anyone remembering.
    "cmd_review_verdict",
    "cmd_roles_claim", "cmd_roles_release",
    "cmd_continuity_snapshot", "cmd_continuity_park",
    "cmd_continuity_checkpoint",
    "cmd_bus_v3_send", "cmd_bus_v3_migrate", "cmd_bus_v3_tag_provision",
    "cmd_forge_watch", "cmd_forge_unwatch", "cmd_forge_mirror",
    "cmd_forge_feedback",
    "cmd_router_execute", "cmd_router_run", "cmd_router_shadow_arm",
    "cmd_router_shadow_report",
    "cmd_stash_push", "cmd_stash_pull",
    "cmd_engagement_sweep", "cmd_usage_log",
    "cmd_atc_init", "cmd_atc_harvest",
    "cmd_annotate_project", "cmd_annotate_resolution",
    "cmd_wake_consume", "cmd_wake_queue_file",
    # Writes one nonce probe before reading its feed visibility.
    "cmd_measure_feed_lag",
}


#: Handlers serving BOTH a read and a write operation, so the FUNCTION cannot
#: classify the invocation — only the parsed args can. Each entry lists an argv
#: for each branch, and both are asserted, because a mixed command can be (and
#: was) wrong in either direction at once.
#:
#: codex-reviewer, 590 r2: `queue commit` recorded classifications without
#: counting as activity, while `inbox` and `digest` refreshed presence merely by
#: being VIEWED. `queue --consume` is included although it was not reported —
#: it advances ANOTHER agent's cursor, which is a mutation of someone else's
#: state rather than bookkeeping of your own read.
EXPECTED_MIXED = {
    "cmd_queue": {
        "read": [["queue", "r"]],
        "write": [["queue", "commit", "r"], ["queue", "r", "--consume"]],
    },
    "cmd_inbox": {
        "read": [["inbox", "r"]],
        "write": [["inbox", "r", "--ack", "some-slug"]],
    },
    "cmd_digest": {
        "read": [["digest", "r"]],
        # BOTH flags enter the persistent branch. `--emit-timeline` was missing
        # from the predicate while the command branched on it, so a digest that
        # wrote its marker classified as a READ (codex-reviewer, 590 r3).
        "write": [["digest", "r", "--store"],
                  ["digest", "r", "--emit-timeline"],
                  ["digest", "r", "--store", "--emit-timeline"]],
    },
}


def _registered_commands() -> dict[str, object]:
    """Walk the REAL argparse tree — every subcommand the CLI actually exposes.

    This is the fix for 590 r1's blind spot: a source regex sees one file; this
    sees the surface a user can invoke, wherever its function was defined.
    """
    found: dict[str, object] = {}

    def walk(parser: argparse.ArgumentParser) -> None:
        fn = parser._defaults.get("func")
        if fn is not None:
            found.setdefault(getattr(fn, "__name__", repr(fn)), fn)
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                for sub in action.choices.values():
                    walk(sub)

    walk(cli.build_parser())
    return found


def test_every_registered_command_is_classified():
    """No command may be silently unclassified.

    This is what makes the other two exhaustive: a newly added command fails
    here until someone decides which it is, so neither direction can regress by
    omission — which is how both previous versions of this file broke.
    """
    registered = set(_registered_commands())
    unclassified = sorted(
        registered - EXPECTED_READS - EXPECTED_WRITES - set(EXPECTED_MIXED))
    assert not unclassified, (
        "these commands are registered but classified neither read nor write, "
        "so nobody has decided whether running them counts as activity: "
        f"{unclassified}. Add each to EXPECTED_READS, EXPECTED_WRITES or "
        "EXPECTED_MIXED — a read must ALSO be added to "
        "cli._ACTIVITY_READ_FUNCS, and a mixed one to cli._MIXED_MODE_ACTIVITY.")


def test_no_read_command_manufactures_liveness():
    """codex-reviewer, 590 r1. Looking at a view is not working.

    The worse direction: a false "this agent is alive" silently suppresses the
    nudge for someone who really is gone.
    """
    offenders = sorted(
        name for name, fn in _registered_commands().items()
        if name in EXPECTED_READS and cli._is_activity_refresh_func(fn))
    assert not offenders, (
        "these READ commands refresh the caller's presence, manufacturing "
        f"liveness out of looking at a view: {offenders}")


def test_every_write_command_counts_as_activity():
    """The original direction: an agent doing real work must not render dark."""
    missed = sorted(
        name for name, fn in _registered_commands().items()
        if name in EXPECTED_WRITES and not cli._is_activity_refresh_func(fn))
    assert not missed, (
        "these verbs persist to the store but do not refresh the actor's "
        "presence, so an agent doing this work renders stale and gets a "
        f"'nudge' it has not earned: {missed}")


def test_the_classification_tables_do_not_overlap():
    """A command in both tables would make one of the assertions vacuous."""
    both = sorted(EXPECTED_READS & EXPECTED_WRITES)
    assert not both, f"classified as both read and write: {both}"


def test_each_mixed_command_is_classified_per_OPERATION():
    """codex-reviewer, 590 r2. One function object, two operations.

    Both branches are asserted for every mixed command, because these were wrong
    in BOTH directions simultaneously: `queue commit` recorded durable
    classifications and did not count, while `inbox` and `digest` counted merely
    for being viewed.
    """
    parser = cli.build_parser()
    for name, branches in EXPECTED_MIXED.items():
        for argv in branches["read"]:
            args = parser.parse_args(argv)
            assert getattr(args.func, "__name__", "") == name, argv
            assert not cli._is_activity_invocation(args), (
                f"`{' '.join(argv)}` is the READ branch and must not refresh "
                "presence — viewing is not working")
        for argv in branches["write"]:
            args = parser.parse_args(argv)
            assert getattr(args.func, "__name__", "") == name, argv
            assert cli._is_activity_invocation(args), (
                f"`{' '.join(argv)}` persists something and must count as "
                "activity, or the actor renders dark while working")


def test_the_function_only_predicate_refuses_to_guess_for_mixed_handlers():
    """`_is_activity_refresh_func` cannot see which operation ran, so for a
    mixed handler it must decline rather than pick a direction — a caller with
    no parsed args has no basis to claim either."""
    for name in EXPECTED_MIXED:
        assert not cli._is_activity_refresh_func(getattr(cli, name))


def test_the_three_tables_are_mutually_exclusive():
    mixed = set(EXPECTED_MIXED)
    assert not (EXPECTED_READS & EXPECTED_WRITES), "read/write overlap"
    assert not (EXPECTED_READS & mixed), "a mixed command cannot also be a pure read"
    assert not (EXPECTED_WRITES & mixed), "a mixed command cannot also be a pure write"


def test_the_digest_predicate_and_the_command_share_ONE_condition():
    """The anti-drift guard, not just the fix.

    `digest --emit-timeline` was misclassified because the persistence condition
    existed TWICE: `cmd_digest` branched on `store or emit_timeline` while the
    classifier checked only `store`. Both now call `_digest_persists`, and this
    asserts the command body still defers to it — a re-inlined condition is the
    exact regression to catch, and it would otherwise be invisible until another
    flag is added.
    """
    import inspect
    body = inspect.getsource(cli.cmd_digest)
    assert "_digest_persists(args)" in body, (
        "cmd_digest must branch on the shared helper; an inlined condition here "
        "is free to drift from the activity classifier again")
    assert "if args.store or emit_timeline:" not in body, (
        "the duplicated condition is back — that duplication IS the bug")
