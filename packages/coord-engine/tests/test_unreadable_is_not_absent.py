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
