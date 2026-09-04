"""Three more places where a 500 used to read as "the file isn't there".

`transport.read` returns None for a missing file and for a failed one alike;
`read_classified` separates them and says so in its own contract. The
2026-09-04 outage — HTTP 500 for an hour on every path three levels deep,
which is where role docs, task docs and vacancy rows all live — turned that
collapse from a latent defect into a live one. `roles claim` was fixed when it
was caught in the act (04b12fc1); these are the three siblings whose
consequence is a WRITE rather than a wrong sentence, found by auditing the
callers that fork on None:

* `acceptance peer_parks` creates an ephemeral role doc when it reads none —
  so an unreadable read overwrote a live role doc with a stub carrying its own
  maintainer and SLA;
* `task restore` refuses to move onto an occupied hot path — so an unreadable
  destination read as free, and the restore clobbered a live doc;
* `escalate` mints the day's vacancy row when it reads none — so an outage
  during the daily pass produced duplicate P1 rows AND duplicate bus events,
  fleet-wide.

Each of these fails toward doing MORE, which is why none of them would have
announced itself.

COVERAGE, stated exactly, because this docstring previously did not.

All three guards are now exercised, each with a converse test so that widening
a guard to refuse everything fails too. That was not true when this file was
written: it named three call sites and contained three tests, and all three
tests were `restore`. `peer_parks` and `escalate` appeared once each, in this
prose, and nowhere in the assertions.

collect-maintainer found that reviewing PR 694 on 2026-09-04 and confirmed it
at full-suite scale -- deleting the `peer_parks` guard entirely left all 2777
tests byte-identical. A file header that reads as coverage of three guards and
delivers one is the same shape as a green gate that cannot fail, and it is
worse than no claim, because it is what the next reader will trust when
deciding whether a change here is safe.

The two it added are the worse two to have left uncovered. Of the three,
`restore` -- the one that was tested -- clobbers a single document.
`peer_parks` overwrites a live role doc with a stub carrying its own maintainer
and SLA, and `escalate` mints duplicate P1 rows AND duplicate bus events
fleet-wide, during exactly the kind of outage that triggers the guard.
"""

from __future__ import annotations

import datetime

import pytest

from coord_engine import cli
from coord_engine_test_helpers import FakeTransport

TEAM = "fulcra"


class ReadsError(FakeTransport):
    """Reads fail the way the outage failed: error, never 'absent'."""

    def __init__(self, *, failing_prefix=""):
        super().__init__()
        self.failing_prefix = failing_prefix
        self.writes: list[str] = []

    def _fails(self, path):
        return path.startswith(self.failing_prefix)

    def read_classified(self, path, *, deadline=None):
        if self._fails(path):
            return None, "error"
        return super().read_classified(path)

    def read(self, path):
        return None if self._fails(path) else super().read(path)

    def write(self, path, content):
        self.writes.append(path)
        return super().write(path, content)


@pytest.fixture(autouse=True)
def _pin_clock(monkeypatch):
    monkeypatch.setattr(cli, "_now", lambda: datetime.datetime(
        2026, 9, 4, 4, 0, 0, tzinfo=datetime.timezone.utc))


def _task_doc(title="live work"):
    return "\n".join(["---", "type: Task", f"title: {title}", "id: keeper",
                      "status: active", "priority: P1", "owner: coord-boss",
                      "---", "", "# keeper", ""])


def test_restore_refuses_when_it_cannot_tell_whether_the_hot_path_is_occupied(capsys):
    """The destructive one. 'I could not read the destination' is not 'the
    destination is free', and a restore over a live doc is unrecoverable."""
    t = ReadsError(failing_prefix=f"team/{TEAM}/task/keeper.md")
    t.put(f"team/{TEAM}/task/keeper.md", _task_doc())
    t.put(f"team/{TEAM}/task/archive/2026-08/keeper.md", _task_doc("archived"))
    rc = cli.main(["task", "restore", TEAM, "keeper"], transport=t)
    err = capsys.readouterr().err
    assert rc == 3, err
    assert "UNKNOWN, not free" in err
    assert f"team/{TEAM}/task/keeper.md" not in t.writes


def test_restore_still_moves_when_the_hot_path_is_genuinely_free():
    """Positive control: the guard must not block the ordinary case."""
    t = FakeTransport()
    t.put(f"team/{TEAM}/task/archive/2026-08/keeper.md", _task_doc("archived"))
    rc = cli.main(["task", "restore", TEAM, "keeper"], transport=t)
    assert rc == 0
    assert t.read(f"team/{TEAM}/task/keeper.md") is not None


def test_restore_still_refuses_a_genuinely_occupied_hot_path(capsys):
    t = FakeTransport()
    t.put(f"team/{TEAM}/task/keeper.md", _task_doc())
    t.put(f"team/{TEAM}/task/archive/2026-08/keeper.md", _task_doc("archived"))
    rc = cli.main(["task", "restore", TEAM, "keeper"], transport=t)
    assert rc == 1
    assert "already exists in the hot path" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# peer_parks — the guard this file's own docstring named FIRST and then never
# exercised. Found by collect-maintainer reviewing PR 694 on 2026-09-04: it
# mutated `if status == "error":` to `if False:` and the whole module's suite
# stayed green, so the guard was load-bearing in production and untested here.
# Reproduced before fixing (8 passed mutated, 8 passed clean).
#
# The PR body claimed every new test file was verified to discriminate by
# mutation. That was true of the restore guard above and false of this one, and
# a coverage claim that is false in part is worth less than no claim at all.
# ---------------------------------------------------------------------------


def _pair_adapter(transport, *, nonce="deadbeef"):
    """The adapter under test, built directly — `acceptance pair` drives a live
    multi-hop protocol, and the hop we need to isolate is the first one."""
    import argparse

    from coord_engine import commands_acceptance

    args = argparse.Namespace(
        team=TEAM, agent="coord-boss", peer="codex-coder",
        timeout=5, nonce=nonce,
    )
    return commands_acceptance._AcceptancePairAdapter(args, transport)


def test_peer_parks_refuses_to_stub_a_role_doc_it_cannot_read():
    """A store 500 on the role path must NOT read as 'no role doc here'.

    This is the write that made the read/error collapse dangerous rather than
    merely wrong: the branch creates an ephemeral role doc carrying its OWN
    maintainer and SLA, so a transient failure overwrites live config with a
    stub — and the acceptance run then reports the hop it just corrupted.
    """
    transport = ReadsError(failing_prefix=f"team/{TEAM}/roles/")
    adapter = _pair_adapter(transport)

    result = adapter.peer_parks()

    assert result.ok is False
    assert "UNREADABLE" in result.detail
    # The whole point: nothing was written over the doc it could not read.
    assert transport.writes == [], (
        f"peer_parks wrote {transport.writes} after an UNREADABLE role doc — "
        "this is the clobber the guard exists to prevent"
    )


def test_peer_parks_still_creates_the_role_when_it_is_genuinely_absent():
    """The converse, so the guard cannot be 'fixed' by refusing everything.

    Without this, `if status == "error"` could be widened to `if True` and the
    test above would still pass while the verb stopped working entirely.
    """
    transport = ReadsError(failing_prefix="\x00never-matches")
    adapter = _pair_adapter(transport)

    adapter.peer_parks()

    assert any(p.startswith(f"team/{TEAM}/roles/") for p in transport.writes), (
        f"peer_parks wrote {transport.writes} — it must still create the "
        "ephemeral role doc when the read is a genuine 'absent'"
    )


def test_public_read_will_not_claim_absent_for_a_transport_that_cannot_classify():
    """The fallback must not manufacture the one status that licenses a write.

    collect-maintainer's PR 694 Finding 2. Not live — every production
    transport implements `read_classified` — but ~30 hand-written test doubles
    reach this branch, and "absent" is precisely what a write-caller treats as
    safe to create over.
    """
    from coord_engine.public_read import SealedGenerationTransport

    class CannotClassify:
        """A transport double that forgot `read_classified`, like the next one will."""

        def read(self, path):
            return None

    # Built without __init__: the constructor wants a live public-read
    # authority, and the branch under test is the one BELOW the sealed-prefix
    # check — reached only when the path is not sealed.
    wrapped = SealedGenerationTransport.__new__(SealedGenerationTransport)
    wrapped._transport = CannotClassify()
    wrapped._team = TEAM
    wrapped._prefix_sections = {}
    wrapped._records = {}

    value, status = wrapped.read_classified("team/fulcra/roles/anything/role.md")

    assert value is None
    assert status == "error", (
        f"fallback claimed {status!r} for an unclassifiable read — a write-caller "
        "treats 'absent' as permission to create over a live doc"
    )


# ---------------------------------------------------------------------------
# escalate — the THIRD site this file's docstring names and never exercised.
#
# collect-maintainer's PR 694 addendum (2026-09-04) found the gap is 2 of 3,
# not 1, and confirmed it at full-suite scale: deleting the peer_parks guard
# left all 2777 tests byte-identical. Its argument for why escalate is the
# worst one to leave uncovered is the reason this test exists — all three
# guarded sites "fail toward doing MORE", but restore (the one that WAS tested)
# clobbers a single doc, while escalate mints duplicate P1 rows AND duplicate
# bus events fleet-wide, during exactly the kind of outage that triggers it.
# ---------------------------------------------------------------------------

_ESC_ROLE_DOC = "---\ntype: Role\nsla_hours: 12\nmaintainer: alice\n---\n"
_ESC_STALE_LEASE = ("---\ntype: Lease\nagent: bob\n"
                    "timestamp: 2020-01-01T00:00:00Z\n---\n")


class _EscalateReadsError(ReadsError):
    """ReadsError plus a record sink, so emitted events are inspectable.

    The assertion that matters most is about EVENTS, not just the document:
    a duplicate row is noise, a duplicate event is noise delivered into every
    fold on the bus.
    """

    def __init__(self, *, failing_prefix=""):
        super().__init__(failing_prefix=failing_prefix)
        self.records: list[dict] = []

    def record_write(self, data_type, api_version, note, sender, **kw):
        self.records.append({"note": note, "sender": sender})
        return True


def _escalate_team(failing_prefix):
    import json

    t = _EscalateReadsError(failing_prefix=failing_prefix)
    t.put("team/r/roles/reviewer.md", _ESC_ROLE_DOC)
    t.put("team/r/roles/reviewer/leases/bob.md", _ESC_STALE_LEASE)
    t.put("team/r/_coord/bus-v3/records.json",
          json.dumps({"data_type": "MomentAnnotation/test-bus",
                      "api_version": "v1alpha1"}))
    return t


def test_escalate_will_not_mint_a_vacancy_row_it_cannot_tell_is_already_minted(capsys):
    """A 500 on the day's row is not 'not minted yet'.

    This branch both WRITES and EMITS, and its guard is the per-day idempotency
    check. Read the failure as absence and the daily pass turns an outage into
    duplicate P1 rows plus duplicate bus events, fleet-wide — alarm bloat at
    machine speed, on the day the store is least able to absorb it.
    """
    t = _escalate_team(failing_prefix="team/r/task/")

    cli.main(["escalate", "r"], transport=t)
    err = capsys.readouterr().err

    assert "UNKNOWN this pass" in err, err
    assert [p for p in t.writes if p.startswith("team/r/task/")] == [], (
        f"escalate wrote {t.writes} after an UNREADABLE day-row — that is the "
        "duplicate vacancy row the guard exists to prevent"
    )
    assert t.records == [], (
        f"escalate emitted {len(t.records)} event(s) after an UNREADABLE "
        "day-row — a duplicate row is noise, a duplicate EVENT is noise "
        "delivered into every fold on the bus"
    )


def test_escalate_still_mints_when_the_day_row_is_genuinely_absent():
    """Positive control: the guard must not silence the alarm it protects.

    Without this, widening the guard would pass the test above while turning
    the daily vacancy pass into a no-op — a fix that looks exactly like the
    silent-alarm incident escalate publishing was built to end.
    """
    t = _escalate_team(failing_prefix="\x00never-matches")

    assert cli.main(["escalate", "r"], transport=t) == 0
    assert [p for p in t.writes if p.startswith("team/r/task/")], (
        f"escalate wrote {t.writes} — it must still mint the vacancy row when "
        "the day-row read is a genuine 'absent'"
    )
    assert t.records, "the minted vacancy must still be published to the bus"
