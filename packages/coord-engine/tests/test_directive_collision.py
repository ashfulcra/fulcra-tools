"""CLI `tell`/`broadcast`/`remind` — slug-collision handling (Task 0).

Regression cover for a production message-loss incident: `tell` derived a slug
from the title, and on a slug collision printed "already exists" and returned 1
— silently DROPPING the message. A relay re-sending an identical reminder must
succeed (dedup); a genuinely different message that happens to share a slug
(e.g. two long titles sharing an 80-char truncation prefix) must still be
delivered, at a stable suffixed slug.
"""

import json

from coord_engine import cli, tasks
from coord_engine_test_helpers import FakeTransport


def test_lost_race_readback_reslugs_to_suffixed(capsys):
    # F3: two colliding tells race. Our absence check passes and our write
    # "succeeds", but a concurrent racer's DIFFERENT message won the slot
    # (last-writer-wins). The I1 fix only checks absence BEFORE writing, so both
    # racers reported rc 0 and one message was silently clobbered. Post-write
    # read-back must reveal the slot holds the OTHER payload -> we lost the race
    # -> re-slug to our deterministic hash-suffixed slot. Both messages durable.
    base_slug = tasks.slugify("Ship it")
    other_doc = ("---\ntype: Task\ntitle: Ship it\ndescription: RACER won\n"
                 "assignee: amy\nstatus: proposed\npriority: P2\n---\nbody\n")

    class LostRaceAtBase(FakeTransport):
        def write(self, path, content):
            # base slot: the racer's write lands instead of ours (last wins).
            if path == f"team/r/task/{base_slug}.md":
                self.store[path] = other_doc
                return True
            return super().write(path, content)

    t = LostRaceAtBase()
    rc = cli.main(["tell", "r", "amy", "Ship it", "-s", "OURS"], transport=t)
    assert rc == 0, "we still deliver — at the suffixed slot, not a false clobber"
    docs = _task_docs(t)
    assert len(docs) == 2, "both the racer's and our message must be durable"
    # base holds the racer's message; ours lands at the deterministic suffix.
    assert t.store[f"team/r/task/{base_slug}.md"] == other_doc
    suffixed = next(d for d in docs if d != f"team/r/task/{base_slug}.md")
    suffixed_slug = suffixed[len("team/r/task/"):-len(".md")]
    assert suffixed_slug.startswith(f"{base_slug}-"), "stable hash suffix"
    assert "OURS" in t.store[suffixed], "our message must survive at the suffix"
    # both surface in the recipient's inbox fold
    cli.main(["reconcile", "r"], transport=t)
    capsys.readouterr()
    assert cli.main(["inbox", "r", "--agent", "amy", "--json"], transport=t) == 0
    names = {r["name"] for r in json.loads(capsys.readouterr().out)}
    assert names == {base_slug, suffixed_slug}, "both messages visible in inbox"


def test_write_readback_none_fails_loud(capsys):
    # F3 corollary: if the post-write read-back returns None (transport degraded),
    # we cannot confirm our write landed/survived -> fail loud (C1), never a
    # silent rc-0 success on an unverifiable delivery.
    class ReadBackNone(FakeTransport):
        def read(self, path):
            return None  # absence check AND read-back both time out

        def list_dir(self, prefix):
            return []  # slot genuinely absent -> we proceed to write

    t = ReadBackNone()
    rc = cli.main(["tell", "r", "amy", "Ship it", "-s", "OURS"], transport=t)
    cap = capsys.readouterr()
    assert rc == 1
    assert "read-back failed" in cap.err or "unverifiable" in cap.err
    assert "-> amy" not in cap.out, "must not claim delivery it cannot verify"


def _task_docs(t):
    return sorted(
        k for k in t.store
        if k.startswith("team/r/task/") and k.endswith(".md")
        and not k.endswith("/index.md") and not k.endswith("/log.md")
    )


def test_identical_resend_dedupes_rc0_one_doc(capsys):
    t = FakeTransport()
    assert cli.main(["tell", "r", "amy", "Ship it", "-s", "now"], transport=t) == 0
    capsys.readouterr()
    # re-send the SAME message: sanctioned dedup, not a failure.
    assert cli.main(["tell", "r", "amy", "Ship it", "-s", "now"], transport=t) == 0
    out = capsys.readouterr().out
    assert "already delivered" in out
    assert _task_docs(t) == ["team/r/task/ship-it.md"]


def test_empty_vs_missing_summary_compare_equal(capsys):
    # None summary and "" summary are the same message -> dedup, not a re-slug.
    t = FakeTransport()
    assert cli.main(["tell", "r", "amy", "Ship it"], transport=t) == 0
    capsys.readouterr()
    assert cli.main(["tell", "r", "amy", "Ship it", "-s", ""], transport=t) == 0
    assert "already delivered" in capsys.readouterr().out
    assert _task_docs(t) == ["team/r/task/ship-it.md"]


def test_prefix_colliding_distinct_message_delivers_at_suffixed_slug(capsys):
    # Two titles sharing the same 80-char truncation prefix -> same base slug,
    # but different (message-bearing) payloads -> the second must NOT be dropped.
    t = FakeTransport()
    title1 = "Alert " + "x" * 80 + " ALPHA"
    title2 = "Alert " + "x" * 80 + " BETA"
    base_slug = tasks.slugify(title1)
    assert base_slug == tasks.slugify(title2)  # precondition: they collide

    assert cli.main(["tell", "r", "amy", title1], transport=t) == 0
    assert cli.main(["tell", "r", "amy", title2], transport=t) == 0

    docs = _task_docs(t)
    assert len(docs) == 2, "distinct message must be delivered, not dropped"
    assert f"team/r/task/{base_slug}.md" in docs
    suffixed = next(d for d in docs if d != f"team/r/task/{base_slug}.md")
    assert suffixed.startswith(f"team/r/task/{base_slug}-"), "stable hash suffix"

    # both surface in the recipient's inbox fold
    cli.main(["reconcile", "r"], transport=t)
    capsys.readouterr()
    assert cli.main(["inbox", "r", "--agent", "amy", "--json"], transport=t) == 0
    names = {r["name"] for r in json.loads(capsys.readouterr().out)}
    suffixed_slug = suffixed[len("team/r/task/"):-len(".md")]
    assert names == {base_slug, suffixed_slug}


def test_suffixed_message_hash_is_deterministic_across_calls(capsys):
    # Same distinct message -> same suffixed slug, twice, from independent stores.
    def deliver():
        t = FakeTransport()
        cli.main(["tell", "r", "amy", "Ship it", "-s", "A"], transport=t)
        cli.main(["tell", "r", "amy", "Ship it", "-s", "C"], transport=t)
        return [d for d in _task_docs(t) if d != "team/r/task/ship-it.md"][0]
    assert deliver() == deliver()


def test_suffixed_retry_dedupes_rc0(capsys):
    t = FakeTransport()
    cli.main(["tell", "r", "amy", "Ship it", "-s", "A"], transport=t)   # base = A
    cli.main(["tell", "r", "amy", "Ship it", "-s", "C"], transport=t)   # suffixed = C
    assert len(_task_docs(t)) == 2
    capsys.readouterr()
    # retry the SAME distinct message: dedupes at the suffixed slug.
    assert cli.main(["tell", "r", "amy", "Ship it", "-s", "C"], transport=t) == 0
    assert "already delivered" in capsys.readouterr().out
    assert len(_task_docs(t)) == 2, "retry must not create a third doc"


def test_pathological_double_collision_fails_rc1_naming_both(capsys):
    t = FakeTransport()
    cli.main(["tell", "r", "amy", "Ship it", "-s", "A"], transport=t)   # ship-it.md = A
    cli.main(["tell", "r", "amy", "Ship it", "-s", "C"], transport=t)   # suffixed = C
    suffixed = next(d for d in _task_docs(t) if d != "team/r/task/ship-it.md")
    suffixed_slug = suffixed[len("team/r/task/"):-len(".md")]
    # corrupt the suffixed slot with a DIFFERENT payload (reuse A's doc).
    t.store[suffixed] = t.store["team/r/task/ship-it.md"]
    capsys.readouterr()

    rc = cli.main(["tell", "r", "amy", "Ship it", "-s", "C"], transport=t)
    assert rc == 1
    err = capsys.readouterr().err
    assert "collision unresolved" in err
    assert "ship-it" in err and suffixed_slug in err  # names both slugs


def test_same_text_different_assignees_delivers_both(capsys):
    # Assignee IS message identity: the same text told to a different agent is a
    # DIFFERENT directive — bob must get his copy (under a suffixed slug).
    t = FakeTransport()
    assert cli.main(["tell", "r", "amy", "Ship it", "-s", "now"], transport=t) == 0
    assert cli.main(["tell", "r", "bob", "Ship it", "-s", "now"], transport=t) == 0
    docs = _task_docs(t)
    assert len(docs) == 2, "bob's copy must be delivered, not deduped away"
    assert "team/r/task/ship-it.md" in docs
    suffixed = next(d for d in docs if d != "team/r/task/ship-it.md")
    suffixed_slug = suffixed[len("team/r/task/"):-len(".md")]
    assert suffixed_slug.startswith("ship-it-")

    # each copy surfaces in its OWN recipient's inbox
    cli.main(["reconcile", "r"], transport=t)
    capsys.readouterr()
    assert cli.main(["inbox", "r", "--agent", "amy", "--json"], transport=t) == 0
    amy = {r["name"] for r in json.loads(capsys.readouterr().out)}
    assert cli.main(["inbox", "r", "--agent", "bob", "--json"], transport=t) == 0
    bob = {r["name"] for r in json.loads(capsys.readouterr().out)}
    assert amy == {"ship-it"}
    assert bob == {suffixed_slug}

    # deterministic per-recipient identity: a retry of bob's copy dedupes at
    # bob's suffixed slug rather than colliding with amy's or forking a third.
    capsys.readouterr()
    assert cli.main(["tell", "r", "bob", "Ship it", "-s", "now"], transport=t) == 0
    assert "already delivered" in capsys.readouterr().out
    assert len(_task_docs(t)) == 2


def test_identical_retell_same_assignee_still_dedupes(capsys):
    # With assignee in the identity, the relay re-send case must still dedupe.
    t = FakeTransport()
    assert cli.main(["tell", "r", "amy", "Ship it", "-s", "now"], transport=t) == 0
    capsys.readouterr()
    assert cli.main(["tell", "r", "amy", "Ship it", "-s", "now"], transport=t) == 0
    assert "already delivered" in capsys.readouterr().out
    assert _task_docs(t) == ["team/r/task/ship-it.md"]


def test_identical_rebroadcast_dedupes(capsys):
    # broadcast pins assignee="*": identical re-broadcasts are the same message.
    t = FakeTransport()
    assert cli.main(["broadcast", "r", "All hands", "-s", "now"], transport=t) == 0
    capsys.readouterr()
    assert cli.main(["broadcast", "r", "All hands", "-s", "now"], transport=t) == 0
    assert "already delivered" in capsys.readouterr().out
    assert _task_docs(t) == ["team/r/task/all-hands.md"]
    # ...while a directed tell of the same text is a DIFFERENT audience -> delivered.
    assert cli.main(["tell", "r", "amy", "All hands", "-s", "now"], transport=t) == 0
    assert len(_task_docs(t)) == 2


def test_never_crashes_on_unparseable_existing_doc(capsys):
    # A base slug occupied by a doc with no parseable frontmatter must be treated
    # as DIFFERENT (suffix + deliver) — losing a message is worse than a dup.
    t = FakeTransport()
    t.store["team/r/task/ship-it.md"] = "garbage, not frontmatter"
    assert cli.main(["tell", "r", "amy", "Ship it", "-s", "real"], transport=t) == 0
    docs = _task_docs(t)
    assert len(docs) == 2
    assert any(d.startswith("team/r/task/ship-it-") for d in docs)


def test_write_timeout_fails_loud_reports_nothing_delivered(capsys):
    # C1: T1 made a timed-out write() return False (never raise). Discarding that
    # bool prints "directive <slug> -> <assignee>" rc 0 while the message is GONE.
    # A False write must fail loud (rc 1), write no doc, and NOT claim delivery.
    class WriteTimesOut(FakeTransport):
        def write(self, path, content):
            return False  # timeout/exec failure -> False, never raises

    t = WriteTimesOut()
    rc = cli.main(["tell", "r", "codex", "Serve r3 review NOW", "-s", "urgent"],
                  transport=t)
    cap = capsys.readouterr()
    assert rc == 1
    assert _task_docs(t) == [], "a failed write must leave the slot empty"
    assert "directive write failed" in cap.err
    assert "-> codex" not in cap.out, "must NOT report an undelivered message as delivered"


def test_read_timeout_over_occupied_slot_refuses_to_clobber(capsys):
    # I1: read() timeout returns None (T1), indistinguishable from a missing slot.
    # Treating None as "empty" would overwrite an occupied slot. A list_dir of the
    # parent confirms the slot IS present -> refuse to write (rc 1), original kept.
    class ReadTimesOut(FakeTransport):
        def read(self, path):
            return None  # timeout: content unknown, no exception

    t = ReadTimesOut()
    original = ("---\ntype: Task\ntitle: Ship it\ndescription: ORIGINAL urgent\n"
                "assignee: amy\n---\n")
    t.store["team/r/task/ship-it.md"] = original
    rc = cli.main(["tell", "r", "bob", "Ship it", "-s", "different message"],
                  transport=t)
    cap = capsys.readouterr()
    assert rc == 1
    assert t.store["team/r/task/ship-it.md"] == original, "original directive must survive"
    assert "unreadable" in cap.err


def test_reremind_new_when_dedupes_and_keeps_original_schedule(capsys):
    # Minor (a): re-reminding the same reminder with a DIFFERENT not_before is the
    # same message (not_before is delivery metadata, outside identity) -> rc 0
    # dedup, original schedule kept, one doc.
    t = FakeTransport()
    assert cli.main(["remind", "r", "amy", "2026-07-11T09:00:00+00:00", "standup"],
                    transport=t) == 0
    capsys.readouterr()
    assert cli.main(["remind", "r", "amy", "2026-07-12T15:00:00+00:00", "standup"],
                    transport=t) == 0
    assert "already delivered" in capsys.readouterr().out
    assert _task_docs(t) == ["team/r/task/standup.md"]
    doc = t.store["team/r/task/standup.md"]
    assert "2026-07-11" in doc and "2026-07-12" not in doc, "original schedule kept"
