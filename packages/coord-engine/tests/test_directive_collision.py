"""CLI `tell`/`broadcast`/`remind` — canonical hash-path directive delivery.

Regression cover for a production message-loss incident and its follow-ons:
`tell` derived a slug from the title, and on a slug collision printed "already
exists" and returned 1 — silently DROPPING the message. The fix history moved
through a suffix-on-collision scheme (still racy: a shared base slot could be
clobbered after a verified write). The CANONICAL design here makes EVERY
directive path carry the payload hash — ``<title-slug>-<sha256(payload)[:8]>``:

- identical payloads (any senders, any order) -> same path, same bytes: existence
  means already delivered, and a write race is idempotent (can't destroy);
- distinct payloads -> distinct paths that can never race each other;
- the post-write read-back is write-verification only (None/mismatch -> rc 1 loud,
  never a claimed-but-unverifiable delivery); an occupied-but-unreadable slot at
  the dedup check fails loud too — we never overwrite.
"""

import json

from coord_engine import cli, tasks
from coord_engine_test_helpers import FakeTransport


def _dslug(title, *, summary=None, next=None, assignee):
    """The canonical directive slug the CLI computes for a given message."""
    payload = cli._directive_payload(title, summary, next, assignee)
    return f"{tasks.slugify(title)}-{cli._payload_hash(payload)}"


def _task_docs(t):
    return sorted(
        k for k in t.store
        if k.startswith("team/r/task/") and k.endswith(".md")
        and not k.endswith("/index.md") and not k.endswith("/log.md")
    )


def test_late_racer_cannot_destroy_verified_delivery(capsys):
    # P1-A (codex r2): the post-write read-back proves the slot held OUR payload
    # at read-back time, but nothing stops a later write to the SAME base slot
    # from destroying it. Interleaving: A writes+verifies (rc 0), then B — which
    # snapshotted the slot as ABSENT before A wrote — writes the base slot,
    # clobbering A's delivered message. At HEAD both A and B target the bare
    # title slug, so only ONE survives. The canonical hash-suffixed path must
    # keep BOTH distinct messages durable (they can never share a path).
    t = FakeTransport()
    assert cli.main(["tell", "r", "amy", "Ship it", "-s", "AAA"], transport=t) == 0

    class StaleAbsentForB(FakeTransport):
        # Models B's pre-A snapshot: B's absence checks see the base slot empty
        # (its read/list are stale), yet B's write lands on the real store —
        # last-writer-wins clobbers A's verified doc.
        def __init__(self, seed):
            super().__init__()
            self.store = dict(seed)
            self.wrote = False

        def read(self, path):
            if not self.wrote and path.endswith("/ship-it.md"):
                return None  # stale snapshot: base slot looked absent to B
            return self.store.get(path)

        def list_dir(self, prefix):
            if not self.wrote:
                return []  # stale snapshot: base slot absent
            return super().list_dir(prefix)

        def write(self, path, content):
            self.wrote = True
            self.store[path] = content
            return True

    b = StaleAbsentForB(t.store)
    capsys.readouterr()
    assert cli.main(["tell", "r", "amy", "Ship it", "-s", "BBB"], transport=b) == 0
    docs = _task_docs(b)
    assert len(docs) == 2, "both A's and B's distinct messages must stay durable"
    bytes_all = "".join(b.store[d] for d in docs)
    assert "AAA" in bytes_all, "A's verified delivery must not be destroyed by B"
    assert "BBB" in bytes_all, "B's message must be delivered too"


def test_same_payload_race_is_idempotent_one_doc(capsys):
    # Two senders of the SAME payload converge on the SAME hash-bearing path and
    # write the SAME message: a race is idempotent (last-writer-wins is a no-op),
    # so it can never destroy a delivery — it collapses to ONE doc, both rc 0.
    path = f"team/r/task/{_dslug('Ship it', summary='AAA', assignee='amy')}.md"
    t = FakeTransport()
    assert cli.main(["tell", "r", "amy", "Ship it", "-s", "AAA"], transport=t) == 0

    class StaleAbsentForB(FakeTransport):
        def __init__(self, seed):
            super().__init__()
            self.store = dict(seed)
            self.wrote = False

        def read(self, p):
            if not self.wrote and p == path:
                return None  # B's stale pre-A snapshot: slot looked absent
            return self.store.get(p)

        def list_dir(self, prefix):
            if not self.wrote:
                return []
            return super().list_dir(prefix)

        def write(self, p, content):
            self.wrote = True
            self.store[p] = content
            return True

    b = StaleAbsentForB(t.store)
    capsys.readouterr()
    assert cli.main(["tell", "r", "amy", "Ship it", "-s", "AAA"], transport=b) == 0
    assert _task_docs(b) == [path], "same-payload race collapses to ONE doc"
    assert "AAA" in b.store[path]


def test_write_readback_none_fails_loud(capsys):
    # If the post-write read-back returns None (transport degraded), we cannot
    # confirm our write landed/survived -> fail loud (C1), never a silent rc-0
    # success on an unverifiable delivery.
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


def test_identical_resend_dedupes_rc0_one_doc(capsys):
    slug = _dslug("Ship it", summary="now", assignee="amy")
    t = FakeTransport()
    assert cli.main(["tell", "r", "amy", "Ship it", "-s", "now"], transport=t) == 0
    capsys.readouterr()
    # re-send the SAME message: sanctioned dedup, not a failure.
    assert cli.main(["tell", "r", "amy", "Ship it", "-s", "now"], transport=t) == 0
    out = capsys.readouterr().out
    assert "already delivered" in out
    assert _task_docs(t) == [f"team/r/task/{slug}.md"]


def test_empty_vs_missing_summary_compare_equal(capsys):
    # None summary and "" summary are the same message -> same hash, dedup.
    slug = _dslug("Ship it", assignee="amy")
    t = FakeTransport()
    assert cli.main(["tell", "r", "amy", "Ship it"], transport=t) == 0
    capsys.readouterr()
    assert cli.main(["tell", "r", "amy", "Ship it", "-s", ""], transport=t) == 0
    assert "already delivered" in capsys.readouterr().out
    assert _task_docs(t) == [f"team/r/task/{slug}.md"]


def test_distinct_messages_land_at_distinct_hash_paths(capsys):
    # Two titles sharing the same 80-char truncation prefix -> same title slug,
    # but different payloads -> distinct hash paths; neither is dropped and they
    # never share a slot to race over.
    t = FakeTransport()
    title1 = "Alert " + "x" * 80 + " ALPHA"
    title2 = "Alert " + "x" * 80 + " BETA"
    assert tasks.slugify(title1) == tasks.slugify(title2)  # precondition: prefix collides

    assert cli.main(["tell", "r", "amy", title1], transport=t) == 0
    assert cli.main(["tell", "r", "amy", title2], transport=t) == 0

    docs = _task_docs(t)
    assert len(docs) == 2, "distinct messages must both deliver, never share a slot"
    prefix = f"team/r/task/{tasks.slugify(title1)}-"
    assert all(d.startswith(prefix) for d in docs), "both carry the payload hash"

    # both surface in the recipient's inbox fold
    cli.main(["reconcile", "r"], transport=t)
    capsys.readouterr()
    assert cli.main(["inbox", "r", "--agent", "amy", "--json"], transport=t) == 0
    names = {r["name"] for r in json.loads(capsys.readouterr().out)}
    assert names == {d[len("team/r/task/"):-len(".md")] for d in docs}


def test_directive_slug_is_deterministic_across_stores(capsys):
    # Same distinct message -> same slug, twice, from independent stores.
    def deliver():
        t = FakeTransport()
        cli.main(["tell", "r", "amy", "Ship it", "-s", "A"], transport=t)
        return _task_docs(t)[0]
    assert deliver() == deliver()


def test_retry_of_distinct_message_dedupes_rc0(capsys):
    t = FakeTransport()
    cli.main(["tell", "r", "amy", "Ship it", "-s", "A"], transport=t)   # message A
    cli.main(["tell", "r", "amy", "Ship it", "-s", "C"], transport=t)   # message C
    assert len(_task_docs(t)) == 2, "two distinct messages -> two docs"
    capsys.readouterr()
    # retry the SAME distinct message: dedupes at its hash slug.
    assert cli.main(["tell", "r", "amy", "Ship it", "-s", "C"], transport=t) == 0
    assert "already delivered" in capsys.readouterr().out
    assert len(_task_docs(t)) == 2, "retry must not create a third doc"


def test_same_text_different_assignees_delivers_both(capsys):
    # Assignee IS message identity: the same text told to a different agent is a
    # DIFFERENT directive with a DIFFERENT hash -> bob must get his own copy.
    amy_slug = _dslug("Ship it", summary="now", assignee="amy")
    bob_slug = _dslug("Ship it", summary="now", assignee="bob")
    assert amy_slug != bob_slug
    t = FakeTransport()
    assert cli.main(["tell", "r", "amy", "Ship it", "-s", "now"], transport=t) == 0
    assert cli.main(["tell", "r", "bob", "Ship it", "-s", "now"], transport=t) == 0
    assert _task_docs(t) == sorted(
        [f"team/r/task/{amy_slug}.md", f"team/r/task/{bob_slug}.md"])

    # each copy surfaces in its OWN recipient's inbox
    cli.main(["reconcile", "r"], transport=t)
    capsys.readouterr()
    assert cli.main(["inbox", "r", "--agent", "amy", "--json"], transport=t) == 0
    amy = {r["name"] for r in json.loads(capsys.readouterr().out)}
    assert cli.main(["inbox", "r", "--agent", "bob", "--json"], transport=t) == 0
    bob = {r["name"] for r in json.loads(capsys.readouterr().out)}
    assert amy == {amy_slug}
    assert bob == {bob_slug}

    # deterministic per-recipient identity: a retry of bob's copy dedupes at
    # bob's hash slug rather than colliding with amy's or forking a third.
    capsys.readouterr()
    assert cli.main(["tell", "r", "bob", "Ship it", "-s", "now"], transport=t) == 0
    assert "already delivered" in capsys.readouterr().out
    assert len(_task_docs(t)) == 2


def test_identical_retell_same_assignee_still_dedupes(capsys):
    # With assignee in the identity, the relay re-send case must still dedupe.
    slug = _dslug("Ship it", summary="now", assignee="amy")
    t = FakeTransport()
    assert cli.main(["tell", "r", "amy", "Ship it", "-s", "now"], transport=t) == 0
    capsys.readouterr()
    assert cli.main(["tell", "r", "amy", "Ship it", "-s", "now"], transport=t) == 0
    assert "already delivered" in capsys.readouterr().out
    assert _task_docs(t) == [f"team/r/task/{slug}.md"]


def test_identical_rebroadcast_dedupes(capsys):
    # broadcast pins assignee="*": identical re-broadcasts are the same message.
    bslug = _dslug("All hands", summary="now", assignee="*")
    t = FakeTransport()
    assert cli.main(["broadcast", "r", "All hands", "-s", "now"], transport=t) == 0
    capsys.readouterr()
    assert cli.main(["broadcast", "r", "All hands", "-s", "now"], transport=t) == 0
    assert "already delivered" in capsys.readouterr().out
    assert _task_docs(t) == [f"team/r/task/{bslug}.md"]
    # ...while a directed tell of the same text is a DIFFERENT audience -> delivered.
    assert cli.main(["tell", "r", "amy", "All hands", "-s", "now"], transport=t) == 0
    assert len(_task_docs(t)) == 2


def test_unparseable_doc_at_canonical_slot_fails_loud(capsys):
    # With the hash-bearing canonical path, an unparseable doc at OUR slot can no
    # longer be a colliding DIFFERENT message (distinct payloads never share a
    # path) — only corruption. Fail loud and NEVER overwrite it; a duplicate is
    # cheaper than a dropped message, but a clobbered slot is worse than both.
    slug = _dslug("Ship it", summary="real", assignee="amy")
    path = f"team/r/task/{slug}.md"
    t = FakeTransport()
    t.store[path] = "garbage, not frontmatter"
    rc = cli.main(["tell", "r", "amy", "Ship it", "-s", "real"], transport=t)
    assert rc == 1
    assert "cannot verify delivery" in capsys.readouterr().err
    assert t.store[path] == "garbage, not frontmatter", "must never overwrite"
    assert _task_docs(t) == [path], "no second doc; nothing clobbered"


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
    # parent confirms OUR canonical slot IS present -> refuse to write (rc 1),
    # original kept, delivery reported as unverifiable.
    slug = _dslug("Ship it", summary="different message", assignee="bob")
    path = f"team/r/task/{slug}.md"

    class ReadTimesOut(FakeTransport):
        def read(self, p):
            return None  # timeout: content unknown, no exception

    t = ReadTimesOut()
    original = ("---\ntype: Task\ntitle: Ship it\ndescription: ORIGINAL urgent\n"
                "assignee: bob\n---\n")
    t.store[path] = original
    rc = cli.main(["tell", "r", "bob", "Ship it", "-s", "different message"],
                  transport=t)
    cap = capsys.readouterr()
    assert rc == 1
    assert t.store[path] == original, "original directive must survive"
    assert "unreadable" in cap.err


def test_reremind_new_when_dedupes_and_keeps_original_schedule(monkeypatch, capsys):
    # Minor (a): re-reminding the same reminder with a DIFFERENT not_before is the
    # same message (not_before is delivery metadata, outside identity) -> rc 0
    # dedup, original schedule kept, one doc.
    #
    # The clock is PINNED (established cli._now monkeypatch pattern, cf.
    # test_cli_respond_response_paths_do_not_collide): the directive doc stamps a
    # `created` timestamp from cli._now(), so an unpinned wall clock landing on
    # 2026-07-12 (UTC) would inject "2026-07-12" into the doc and false-fail the
    # `"2026-07-12" not in doc` schedule-preservation assertion. Pinning to a date
    # distinct from BOTH not_before dates keeps the assertion purely about the
    # kept schedule, independent of the day the suite runs.
    from datetime import datetime, timezone
    monkeypatch.setattr(cli, "_now", lambda: datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc))
    slug = _dslug("standup", assignee="amy")
    path = f"team/r/task/{slug}.md"
    t = FakeTransport()
    assert cli.main(["remind", "r", "amy", "2026-07-11T09:00:00+00:00", "standup"],
                    transport=t) == 0
    capsys.readouterr()
    assert cli.main(["remind", "r", "amy", "2026-07-12T15:00:00+00:00", "standup"],
                    transport=t) == 0
    assert "already delivered" in capsys.readouterr().out
    assert _task_docs(t) == [path]
    doc = t.store[path]
    assert "2026-07-11" in doc and "2026-07-12" not in doc, "original schedule kept"
