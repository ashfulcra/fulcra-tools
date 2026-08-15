"""A fresh `review request` must reach the reviewer's QUEUE, not just the store.

pr-630 root cause #2, bitten live 2026-08-14 (agent-skills pr-176): the verb
wrote the review register and the reviewer's durable task, but its delivery
path never emitted the ``v:1`` companion event ``tell`` emits — so a raw
queue read had nothing to deliver, and the reviewer learned of the request
from a ``needs-me`` fold forty minutes later, via the operator. The durable
document is the obligation; the event is the delivery. Both or it isn't sent.

Contract pinned here, same as the tell/respond companions:
- a verified FRESH reviewer directive emits exactly one companion event,
  addressed to the reviewer, pointing at the directive task doc;
- an idempotent same-head re-request (dedupe) emits NOTHING — a second event
  for an already-delivered slug is noise the recipient cannot tell from work;
- a bus that is unconfigured degrades to file-plane-only and never fails the
  request (best-effort delivery over a durable truth).
"""
from __future__ import annotations

import pytest

from coord_engine import cli, records
from coord_engine_test_helpers import FakeTransport

CFG = {"data_type": "MomentAnnotation/d04f357e-b556-4298-ad1e-4ce307d54041",
       "api_version": "v1alpha1"}


@pytest.fixture
def emitted(monkeypatch):
    calls: list[dict] = []

    def _emit(transport, cfg, **kw):
        calls.append(kw)
        return True

    monkeypatch.setattr(records, "emit_event", _emit)
    monkeypatch.setattr(
        records, "load_config_classified", lambda t, team: (CFG, "ok"))
    return calls


def _request(t, head="a" * 40):
    return cli.main(["review", "request", "r", "pr-9", "--of", "url",
                     "--reviewer", "alice", "--from", "boss",
                     "--head", head], transport=t)


def test_fresh_review_request_emits_one_companion_to_the_reviewer(emitted):
    t = FakeTransport()
    assert _request(t) == 0
    directives = [e for e in emitted if e.get("kind") == "directive"]
    assert len(directives) == 1
    (event,) = directives
    assert event["to"] == "alice"
    assert event["sender"] == "boss"
    assert event["priority"] == "P1"
    # The pointer names the durable task doc — the obligation the event delivers.
    assert event["ptr"].startswith("task/")
    assert t.read(f"team/r/{event['ptr']}") is not None


def test_same_head_re_request_dedupes_and_emits_nothing_new(emitted):
    t = FakeTransport()
    assert _request(t) == 0
    before = len(emitted)
    assert _request(t) == 0          # idempotent re-request, same (slug, head)
    assert len(emitted) == before    # no second event for a delivered slug


def test_new_head_round_emits_a_fresh_companion(emitted):
    t = FakeTransport()
    assert _request(t, head="a" * 40) == 0
    assert _request(t, head="b" * 40) == 0   # head moved: new round, new work
    directives = [e for e in emitted if e.get("kind") == "directive"]
    assert len(directives) == 2


def test_unconfigured_bus_degrades_to_file_plane_and_still_succeeds(capsys):
    t = FakeTransport()   # no records config anywhere: emit path prints + returns
    assert _request(t) == 0
    # The durable task landed even though no event could be emitted.
    names = [p for p in t.store if p.startswith("team/r/task/")]
    assert any("review-request-pr-9" in n for n in names)
