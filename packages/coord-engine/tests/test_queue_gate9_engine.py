"""Gate 9 against production: the consume audit, checked by the harness.

Gate 9 shipped in ``test_queue_contract_gates.py`` against the reference model,
because slice 4 did not exist. Slice 4 merged (PR #498), so it binds now — the
commitment made when the gap was raised.

Scope, stated because it is a deliberate narrowing
--------------------------------------------------
The reference gate also asserts a ``token`` field. That is **my
over-specification**, not the contract: the r2 spec's list is actor, target,
reason, timestamp, and the prior/new coverage claim. Requiring production to carry
my extra field would be enforcing an invention as if it were the spec — the same
error I flagged when gate 5's classification lived in an adapter. So the token
assertion stays on the reference model, and this gate requires exactly what the
spec asks for, in the corrected form the spec itself needs (see below).

The spec's own field list is wrong, and this gate encodes the fix
----------------------------------------------------------------
"prior generation, and new generation recorded durably" asks for two things no
process can honestly know at audit time: the observed prior can be overtaken by a
concurrent writer before the takeover lands, and a successor revision may never
exist (a replayed pending delivery creates none, a staged delivery may never
commit, a CAS loser adopts the winner's state). The engine records
``observed_prior`` + ``intended_authority`` instead, which is what a caller can
actually attest. This gate asserts THAT, and additionally asserts the prediction
has not crept back in — because the failure mode here is a well-meaning future
change adding a "new_revision" field to satisfy the spec sentence.
"""

from __future__ import annotations

import pytest

from coord_engine import cli, okf, records
from test_records_write import QueueTransport, _event_rec, _pin_clock

TEAM = "r"
AGENT = "amy"
CALLER = "coord-boss"


class AuditTransport(QueueTransport):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.write_log: list[str] = []

    def read_classified(self, path):
        content = self.read(path)
        return (content, "ok") if content is not None else (None, "absent")

    def write(self, path, content):
        self.write_log.append(path)
        return super().write(path, content)


@pytest.fixture
def transport() -> AuditTransport:
    t = AuditTransport(window=[_event_rec("r1", "job", to=AGENT)])
    t.put(f"team/{TEAM}/_coord/bus-v3/records.json",
          '{"data_type": "MomentAnnotation/x", "api_version": "v1alpha1"}')
    return t


def _takeover(monkeypatch, transport) -> int:
    monkeypatch.setenv("FULCRA_COORD_AGENT", CALLER)
    _pin_clock(monkeypatch)
    return cli.main(["queue", TEAM, "--agent", AGENT, "--consume"],
                    transport=transport)


def _audit_doc(transport) -> dict:
    prefix = f"team/{TEAM}/{records.CONSUME_AUDIT_PREFIX}/"
    paths = [p for p in transport.write_log if p.startswith(prefix)]
    assert len(paths) == 1, f"expected exactly one audit doc, got {paths}"
    return okf.parse_frontmatter(transport.store[paths[0]])


def test_gate_9_production_audit_carries_the_spec_field_set(monkeypatch, transport):
    """Who, whom, why, when, what was observed, and under which authority."""
    assert _takeover(monkeypatch, transport) == 0
    fm = _audit_doc(transport)

    for required in ("ts", "caller", "target", "reason",
                     "observed_prior", "intended_authority"):
        assert required in fm, f"audit record is missing {required}"
    assert fm["caller"] == CALLER
    assert fm["target"] == AGENT
    assert "--consume" in fm["reason"], (
        "the reason must say what was done, not merely that something was"
    )
    assert fm["observed_prior"], "observed_prior must name the claim seen"
    assert fm["intended_authority"], "intended_authority must name the authority"


def test_gate_9_production_audit_records_no_prediction(monkeypatch, transport):
    """The correction must not be undone by a later well-meaning field.

    A future change that adds ``new_generation`` to satisfy the spec sentence
    would reintroduce a value that may never come to exist. This is the test that
    makes that regression loud instead of plausible.
    """
    assert _takeover(monkeypatch, transport) == 0
    fm = _audit_doc(transport)

    assert "new_generation" not in fm
    assert "new_revision" not in fm
    intended = fm["intended_authority"]
    if isinstance(intended, dict):
        assert "revision" not in intended, (
            "intended_authority names the authority, not a successor revision — "
            "a predicted revision may never exist"
        )
        assert "ts" not in intended and "last_read" not in intended, (
            "a predicted coverage timestamp is the other unknowable the "
            "correction removed: save stamps its own clock"
        )


def test_gate_9_audit_precedes_the_cursor_write(monkeypatch, transport):
    """Ordering is the property that makes the audit evidence at all.

    An audit written after the fact is a note about something that already
    happened irrecoverably. Written first, a crash leaves the takeover
    reconstructable.
    """
    assert _takeover(monkeypatch, transport) == 0
    prefix = f"team/{TEAM}/{records.CONSUME_AUDIT_PREFIX}/"
    audit_index = next(i for i, p in enumerate(transport.write_log)
                       if p.startswith(prefix))
    cursor_index = next(i for i, p in enumerate(transport.write_log)
                        if p == records.cursor_path(TEAM, AGENT))
    assert audit_index < cursor_index, (
        "the cursor was mutated before the audit landed; a crash in between "
        "leaves an unexplained takeover"
    )


def test_binding_is_not_vacuous(monkeypatch, transport):
    """Prove a real takeover happened, not a no-op that trivially satisfies all
    the assertions above."""
    assert _takeover(monkeypatch, transport) == 0
    assert records.cursor_path(TEAM, AGENT) in transport.write_log
    assert transport.record_queries, (
        "no record window was queried, so no takeover read occurred and this "
        "suite is asserting against an audit for work that never happened"
    )
