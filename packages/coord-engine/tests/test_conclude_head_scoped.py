"""`review conclude` on an UNBOUND review accepts HEAD-SCOPED verdicts.

The gate asked `parse_verdict_filename(name, head=None)`, which returns None for
a name that CARRIES a head — so it failed closed on MORE information than it
asked for. `fulcra-meeting-crm-upstream…` has five verdicts, the newest an
`approve` superseding an earlier `changes`, and the verb called it
"abandonment, not conclusion" and refused. A verdict naming the exact head it
reviewed is strictly better evidence than one naming nothing.

Ruling: `three-rulings-your-withdrawal-is-half-right-and-i-measured-which-half-hold-the-b-804e53c7`
— codex-reviewer's old-head guard is SUPERSEDED FOR UNBOUND ROWS ONLY. It keeps
its teeth on bound rows, where `test_conclude_REFUSES_an_OLD_HEAD_shard_on_a_BOUND_row`
still asserts its original reproduction.

The load-bearing REFUSALS are unchanged, and are what this file mostly tests:
643 r2 says a filename is not review evidence, so widening which NAMES are
candidates must not let a candidate through on its name. An unrelated `notes.md`
is still rejected — by its contents. A bound review still belongs to
`review close`. An unreadable candidate is still UNKNOWN and refuses.
"""
from __future__ import annotations

import argparse

import pytest

from coord_engine import cli, okf, review_gc
from coord_engine_test_helpers import FakeTransport

TEAM = "t"
SLUG = "unbound-review"
HEAD = "3bed1be24dd7e599665373741eeaa47bff7a6c01"
OTHER = "2120c38007afb01ef2e51be6fceb93a0e8289331"


@pytest.fixture(autouse=True)
def _quiet_rows(monkeypatch):
    """Settle-time close is a separate concern; keep it out of these results."""
    monkeypatch.setattr(cli, "_load_rows_status", lambda t, team, **kw: ([], True, ""))


def _ns(**kw):
    ns = argparse.Namespace(team=TEAM, slug=SLUG, sender="tester", reason=None)
    for k, v in kw.items():
        setattr(ns, k, v)
    return ns


def _verdict(head, verdict="approve", reviewer="codex-reviewer"):
    return okf.render_frontmatter(
        {"type": "Verdict", "schema": "review-verdict/v2", "reviewer": reviewer,
         "head": head, "verdict": verdict}) + "\n"


def _unbound_review(t, *, doc_head=None, shards=None):
    fm = {"type": "Review", "title": SLUG}
    if doc_head:
        fm["head"] = doc_head
    t.put(cli._review_doc_path(TEAM, SLUG), okf.render_frontmatter(fm) + "\n")
    for name, body in (shards or {}).items():
        t.put(cli._verdicts_prefix(TEAM, SLUG) + name, body)
    return t


def _marker(t):
    return okf.parse_frontmatter(
        t.store[cli._verdicts_prefix(TEAM, SLUG) + review_gc.CONCLUDED_MARKER])


def test_a_head_scoped_verdict_now_concludes_an_unbound_review():
    t = _unbound_review(FakeTransport(),
                        shards={f"{HEAD}--codex-reviewer.md": _verdict(HEAD)})
    assert cli.cmd_review_conclude(_ns(), t) == 0
    assert _marker(t)["state"] == "CONCLUDED"


def test_the_conclusion_records_the_head_it_found():
    t = _unbound_review(FakeTransport(),
                        shards={f"{HEAD}--codex-reviewer.md": _verdict(HEAD)})
    assert cli.cmd_review_conclude(_ns(), t) == 0
    assert _marker(t)["heads"] == [HEAD], (
        "a conclusion that names no head cannot be audited against its evidence")


def test_the_document_head_wins_over_the_filename():
    """The DOCUMENT is the evidence. The filename is only the fallback."""
    t = _unbound_review(FakeTransport(),
                        shards={f"{OTHER}--codex-reviewer.md": _verdict(HEAD)})
    assert cli.cmd_review_conclude(_ns(), t) == 0
    assert _marker(t)["heads"] == [HEAD]


def test_an_unrelated_md_is_still_refused_by_its_CONTENTS():
    """643 r2. Widening which NAMES are candidates must not admit one on name."""
    t = _unbound_review(FakeTransport(),
                        shards={f"{HEAD}--notes.md": "just some prose\n"})
    assert cli.cmd_review_conclude(_ns(), t) != 0
    assert (cli._verdicts_prefix(TEAM, SLUG) + review_gc.CONCLUDED_MARKER
            not in t.store)


def test_a_verdict_with_no_recognized_verdict_field_is_still_refused():
    t = _unbound_review(FakeTransport(), shards={
        f"{HEAD}--codex-reviewer.md": okf.render_frontmatter(
            {"reviewer": "codex-reviewer", "head": HEAD}) + "\n"})
    assert cli.cmd_review_conclude(_ns(), t) != 0


def test_a_BOUND_review_is_still_refused_and_sent_to_review_close():
    t = _unbound_review(FakeTransport(), doc_head=HEAD,
                        shards={f"{HEAD}--codex-reviewer.md": _verdict(HEAD)})
    assert cli.cmd_review_conclude(_ns(), t) == 2


def test_an_unreadable_candidate_is_UNKNOWN_and_refuses():
    class Blind(FakeTransport):
        def read(self, path):
            if path.endswith("--codex-reviewer.md"):
                return None
            return super().read(path)
    t = _unbound_review(Blind(),
                        shards={f"{HEAD}--codex-reviewer.md": _verdict(HEAD)})
    assert cli.cmd_review_conclude(_ns(), t) == 3
