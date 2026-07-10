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
