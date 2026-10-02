"""`health` must distinguish "a host wrote a shard" from "a host advanced the index".

Earned live on 2026-10-02: ``team/fulcra``'s task index had not advanced in ~16h
while ``health`` reported ``4/7 host(s) fresh`` with four ``[ok]`` rows. Every one
of those hosts was aborting on ``change detection UNKNOWN`` and preserving the
current generation -- and the abort path writes a health shard *before* returning
(``reconcile._write_health_shard``), so an aborting host produces a perfectly
fresh shard. The only trace was an unlabelled ``1 warn``, which nobody reads.

The shard could not carry the answer either: ``build_shard`` recorded
``warnings`` as a COUNT and dropped the ``degraded`` flag and the reason entirely,
so the distinction was not merely unrendered, it was unrecorded.

What is pinned here:

* the shard records ``advanced`` explicitly, plus the reason when it did not;
* a shard written by an OLDER engine (no ``advanced`` key) folds to ``None`` --
  UNKNOWN, never ``True``. A missing field is not evidence that a pass succeeded,
  and during a rollout every pre-upgrade host must read as unknown rather than
  either accused or excused;
* ``fresh``/``stale``/``healthy`` keep their current meaning exactly, so existing
  readers do not shift under them. The new information rides as new facts. Whether
  "every fresh host is aborting" should also flip ``healthy`` (and therefore the
  exit code) is a deliberate open question for review, not something this change
  decides;
* the text output says so out loud, and does NOT say so for a healthy host.
"""

import json

from coord_engine import cli, health
from coord_engine.tasks import agent_key

from coord_engine_test_helpers import FakeTransport


SUCCESS_RESULT = {"tasks": 7, "parsed": 2, "reused": 5, "warnings": []}
ABORT_RESULT = {
    "degraded": True,
    "reason": "change detection UNKNOWN; current generation preserved",
    "tasks": 7,
    "warnings": ["change detection UNKNOWN; current generation preserved"],
    "rows": [],
}


def _shard(**over):
    base = {"schema": "coord.teams.health.v1", "host": "h", "at": "2026-10-02T16:00:00Z"}
    base.update(over)
    return base


def test_build_shard_records_that_a_successful_pass_advanced():
    got = health.build_shard(host="h", now="2026-10-02T16:00:00Z",
                             engine_version="2.0.6", result=SUCCESS_RESULT)
    assert got["advanced"] is True
    assert got["degraded_reason"] is None


def test_build_shard_records_that_an_aborted_pass_did_not_advance():
    got = health.build_shard(host="h", now="2026-10-02T16:00:00Z",
                             engine_version="2.0.6", result=ABORT_RESULT)
    assert got["advanced"] is False
    assert got["degraded_reason"] == (
        "change detection UNKNOWN; current generation preserved")


def test_fold_carries_the_advance_fact_and_counts_fresh_hosts_that_did_not():
    now = "2026-10-02T16:05:00Z"
    view = health.fold(
        [
            _shard(host="advancing", advanced=True, degraded_reason=None),
            _shard(host="aborting", advanced=False,
                   degraded_reason="change detection UNKNOWN; current generation preserved"),
        ],
        now=now,
    )
    by_host = {h["host"]: h for h in view["hosts"]}
    assert by_host["advancing"]["advanced"] is True
    assert by_host["aborting"]["advanced"] is False
    assert by_host["aborting"]["degraded_reason"].startswith("change detection UNKNOWN")
    assert view["advancing"] == 1
    assert view["aborting"] == 1
    assert view["unknown_advance"] == 0


def test_a_shard_from_an_older_engine_folds_to_unknown_not_advanced():
    """A missing field is never evidence that a pass succeeded."""
    view = health.fold([_shard(host="old")], now="2026-10-02T16:05:00Z")
    assert view["hosts"][0]["advanced"] is None
    assert view["unknown_advance"] == 1
    assert view["advancing"] == 0
    assert view["aborting"] == 0, "UNKNOWN must not be counted as an abort either"


def test_a_stale_host_is_not_counted_in_any_advance_rollup():
    """The rollups answer "is the index moving NOW", so they only count fresh hosts."""
    view = health.fold(
        [_shard(host="dead", at="2020-01-01T00:00:00Z", advanced=False,
                degraded_reason="change detection UNKNOWN")],
        now="2026-10-02T16:05:00Z",
    )
    assert view["hosts"][0]["stale"] is True
    assert (view["advancing"], view["aborting"], view["unknown_advance"]) == (0, 0, 0)


def test_fresh_stale_and_healthy_keep_their_existing_meaning():
    """The new facts are additive. Redefining `fresh` would shift every reader."""
    view = health.fold(
        [_shard(host="aborting", advanced=False, degraded_reason="change detection UNKNOWN")],
        now="2026-10-02T16:05:00Z",
    )
    assert view["fresh"] == 1 and view["total"] == 1
    assert view["healthy"] is True, (
        "healthy still means 'some host reconciled recently'; whether an "
        "all-aborting fleet should flip it is an open review question")


def test_health_text_names_a_fresh_host_that_did_not_advance(capsys):
    """The live failure: four [ok] rows over a 16h freeze. It must be loud now."""
    transport = FakeTransport()
    transport.put(
        f"team/r/_coord/health/{agent_key('frozen-host')}.json",
        json.dumps(_shard(host="frozen-host", at="2026-10-02T16:00:00Z", advanced=False,
                          degraded_reason="change detection UNKNOWN; current generation preserved")),
    )
    cli.main(["health", "r"], transport=transport)
    out = capsys.readouterr().out
    assert "DID NOT ADVANCE" in out
    assert "change detection UNKNOWN" in out
    assert "did not advance the index" in out, "the headline must carry it too"


def test_health_text_stays_quiet_for_a_host_that_did_advance(capsys):
    """Discrimination: the marker must not fire on the healthy path."""
    transport = FakeTransport()
    transport.put(
        f"team/r/_coord/health/{agent_key('good-host')}.json",
        json.dumps(_shard(host="good-host", at="2026-10-02T16:00:00Z", advanced=True,
                          degraded_reason=None)),
    )
    cli.main(["health", "r"], transport=transport)
    out = capsys.readouterr().out
    assert "DID NOT ADVANCE" not in out
    assert "did not advance the index" not in out


def test_reconcile_abort_writes_a_shard_that_says_it_did_not_advance():
    """End to end through the real command, not just the decision function.

    A ship-gate in AGENTS.md: a green decision-function suite over a path the
    command never takes is the false clear this whole change is about. The
    transport shape here is the live one -- ``data_updates`` returning ``None``,
    which the detector turns into UNKNOWN with no named reason.
    """

    class NoFeedTransport(FakeTransport):
        def data_updates(self, _since, *, deadline=None):
            return None

    transport = NoFeedTransport()
    transport.put("team/r/task/a.md", "---\ntype: Task\ntitle: A\nstatus: active\n---\n")
    cli.main(["reconcile", "r"], transport=transport)

    shards = [p for p in transport.store if p.startswith("team/r/_coord/health/")]
    assert shards, "the abort path still writes a shard -- that is the whole problem"
    shard = json.loads(transport.store[shards[0]])
    assert shard["advanced"] is False
    assert shard["degraded_reason"], "the reason must reach the shard, not only the log"
