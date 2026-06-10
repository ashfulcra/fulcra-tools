"""Pure lifecycle tests for the coordination-loop kind registry.

No I/O anywhere in this file: loops.py imports only schema + stdlib (pinned by
the fitness test in test_fulcra_coord.py).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fulcra_coord import loops, schema


def _loop(kind, state=None, *, expects=True, sla=None, audience="b:h:r", **over):
    d = schema.make_directive(
        directive_type="tell", from_agent="a:h:r", audience=audience,
        title=f"{kind} loop", workstream="general",
        kind=kind, state=state or loops.initial_state(kind),
        expects_response=expects, sla_hours=sla,
    )
    d.update(over)
    return d


class TestRegistry:
    def test_every_kind_has_initial_and_terminal_states(self):
        for kind in loops.KINDS:
            assert loops.initial_state(kind) in loops.states_of(kind)
            assert loops.terminal_states(kind) & loops.states_of(kind)

    def test_closure_reachable_from_every_state(self):
        # Spec invariant: lifecycles never strand a loop — from EVERY state some
        # terminal state is reachable through legal transitions.
        for kind in loops.KINDS:
            for state in loops.states_of(kind):
                assert loops.closure_reachable(kind, state), (kind, state)

    def test_review_lifecycle_shape(self):
        assert loops.initial_state("review") == "requested"
        assert loops.can_transition("review", "requested", "acked")
        assert loops.can_transition("review", "in_review", "responded")
        assert loops.can_transition("review", "responded", "closed")
        assert not loops.can_transition("review", "closed", "requested")

    def test_dispatch_lifecycle_shape(self):
        assert loops.initial_state("dispatch") == "assigned"
        assert loops.can_transition("dispatch", "assigned", "accepted")
        assert loops.can_transition("dispatch", "assigned", "declined")
        assert loops.can_transition("dispatch", "in_progress", "delivered")
        assert loops.can_transition("dispatch", "delivered", "closed")

    def test_idea_lifecycle_shape(self):
        assert loops.initial_state("idea") == "captured"
        for a, b in [("captured", "maturing"), ("maturing", "viable"),
                     ("viable", "routed"), ("routed", "active"),
                     ("active", "done")]:
            assert loops.can_transition("idea", a, b), (a, b)
        assert loops.can_transition("idea", "captured", "dropped")

    def test_illegal_transition_rejected(self):
        assert loops.can_transition("review", "requested", "closed") is False


class TestLegacyMapping:
    def test_old_record_without_kind_reads_as_tell(self):
        d = schema.make_directive(
            directive_type="broadcast", from_agent="a:h:r", audience="*",
            title="legacy fyi", workstream="general",
        )
        for k in ("kind", "state", "outcome", "expects_response", "sla_hours"):
            d.pop(k, None)
        assert loops.loop_kind_of(d) == "tell"
        # Legacy state derives from the directive status, not the state field.
        assert loops.loop_state_of(d) in loops.states_of("tell")

    def test_kindful_record_uses_its_own_fields(self):
        d = _loop("review")
        assert loops.loop_kind_of(d) == "review"
        assert loops.loop_state_of(d) == "requested"


class TestOpenClosed:
    def test_expects_response_loop_is_open_until_terminal(self):
        d = _loop("review")
        assert loops.is_open_loop(d)
        d["state"] = "responded"
        assert loops.is_open_loop(d)        # responded but not yet closed
        d["state"] = "closed"
        assert not loops.is_open_loop(d)

    def test_fyi_tell_without_expected_response_is_not_open(self):
        d = _loop("tell", expects=False)
        assert not loops.is_open_loop(d)

    def test_legacy_record_is_never_an_open_loop(self):
        d = schema.make_directive(
            directive_type="tell", from_agent="a:h:r", audience="b:h:r",
            title="legacy", workstream="general",
        )
        for k in ("kind", "state", "outcome", "expects_response", "sla_hours"):
            d.pop(k, None)
        assert not loops.is_open_loop(d)
