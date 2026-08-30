"""A fresh `intent` must reach the principal's QUEUE, not just the store.

The third instance of pr-630 root cause #2. `tell` was fixed (2026-08-06) and
`review request` was fixed (2026-08-14, after it was bitten live on
agent-skills pr-176), but `intent` still wrote its durable directive through
``_write_directive`` and returned without emitting the ``v:1`` companion — so
an intent captured against a principal was ABSENT from the annotation stream
entirely. Every stream consumer (queue, --stream obligations, any per-identity
cursor fold) reads events; a source that does not emit is not slow on the
channel, it is missing from it, and no cursor advance can ever surface it.

Contract pinned here, identical to the tell and review-request companions:
- a verified FRESH intent emits exactly one companion, addressed to the
  principal, pointing at the durable task doc it just wrote;
- an idempotent restatement (dedupe, "intent already captured") emits NOTHING
  — a second event for an already-delivered slug is indistinguishable from
  new work;
- an in-place ``--by`` window update emits NOTHING: the obligation was already
  opened and delivered, and a revised deadline is not a new obligation;
- a bus that is unconfigured degrades to file-plane-only and never fails the
  intent — the durable doc is the truth, the event is delivery.
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


def _intent(t, title="ship the widget", *, by=None, sender="boss"):
    argv = ["intent", "r", title, "--for", "alice", "--from", sender]
    if by:
        argv += ["--by", by]
    return cli.main(argv, transport=t)


def test_fresh_intent_emits_one_companion_to_the_principal(emitted):
    t = FakeTransport()
    assert _intent(t) == 0
    directives = [e for e in emitted if e.get("kind") == "directive"]
    assert len(directives) == 1, (
        "a fresh intent must emit exactly one companion — without it the "
        "intent exists only as a file and no stream consumer can see it"
    )
    (event,) = directives
    assert event["to"] == "alice"
    assert event["sender"] == "boss"


def test_the_companion_points_at_the_intent_doc_that_was_written(emitted):
    """The ptr is the whole point: a consumer following it lands on the durable
    doc, which is what lets a stream fold answer without enumerating files."""
    t = FakeTransport()
    assert _intent(t) == 0
    (event,) = [e for e in emitted if e.get("kind") == "directive"]
    assert event["ptr"].startswith("task/")
    assert t.read(f"team/r/{event['ptr']}") is not None


def test_an_identical_restatement_emits_nothing(emitted):
    """Dedupe path: the doc already exists and rc is 0, but re-emitting would
    put a second event for an already-delivered obligation on the channel."""
    t = FakeTransport()
    assert _intent(t) == 0
    first = len([e for e in emitted if e.get("kind") == "directive"])
    assert _intent(t) == 0          # "intent already captured"
    after = len([e for e in emitted if e.get("kind") == "directive"])
    assert after == first == 1


def test_a_revised_by_window_emits_nothing(emitted):
    """An in-place window update is not a new obligation — it was opened and
    delivered by the first intent, and the deadline is doc content."""
    t = FakeTransport()
    assert _intent(t, by="5d") == 0
    first = len([e for e in emitted if e.get("kind") == "directive"])
    assert _intent(t, by="10d") == 0
    after = len([e for e in emitted if e.get("kind") == "directive"])
    assert after == first == 1


def test_an_unconfigured_bus_never_fails_the_intent(monkeypatch):
    """Best-effort delivery over durable truth: with no bus configured the
    intent still succeeds and the durable doc is still written."""
    monkeypatch.setattr(
        records, "load_config_classified", lambda t, team: (None, "absent"))
    t = FakeTransport()
    assert _intent(t) == 0
    assert t.list_dir("team/r/task/")
