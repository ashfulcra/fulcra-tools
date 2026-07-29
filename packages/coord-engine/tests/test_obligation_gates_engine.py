"""Bind harness gates 8 and 10 to the real obligation fold.

Gates 8 and 10 shipped in ``test_queue_contract_gates.py`` against the reference
``ObligationFold`` in ``acceptance/contract.py``, because slice 3 did not exist
yet. That was correct then and a liability now: a gate that only ever runs
against the model I also wrote proves that I am self-consistent, not that the
engine is right. I said so about gate 5 when the classification lived in
codex-coder's adapter; the same standard applies to my own gates.

So the original gate bodies run here unchanged, against
``coord_engine.obligations.fold`` — same pattern codex-coder used to bind gates
1-7. The adapter translates representation only; it decides nothing.
"""

from __future__ import annotations

import json

import pytest

from acceptance.contract import FakeStore, TransportUnknown, config_path
from coord_engine.obligations import (
    OBLIGATION_COMPONENTS,
    Component,
    ObligationState,
    ProbeResult,
    ProbeState,
    fold,
)
import test_queue_contract_gates as gates
from acceptance.contract import ReadState


class EngineObligationFold:
    """The gates' ``ObligationFold`` surface, backed by the production fold.

    The gates were written against a two-component store-backed fold with an
    ``owed() -> (ReadState, [names])`` shape. Production speaks
    ``ObligationState`` over probes, so this maps state names and builds one probe
    per store path. Mapping only: every decision — what makes CLEAR unreachable,
    how unreadable outranks malformed — stays in the production fold.
    """

    #: Gate fixtures name two components; the production registry names six. The
    #: gates are about the fail-closed RULE, not the registry's size, so the
    #: expected set for these runs is exactly what the fixture supplies.
    def __init__(self, store: FakeStore, components: dict[str, str]) -> None:
        self.store = store
        self.components = components

    def _probe(self, path: str):
        def probe() -> ProbeResult:
            try:
                raw = self.store.read(path)
            except TransportUnknown:
                return ProbeResult(state=ProbeState.UNREADABLE, detail=path)
            if raw is None:
                return ProbeResult(state=ProbeState.OK)
            try:
                doc = json.loads(raw)
            except ValueError:
                return ProbeResult(state=ProbeState.MALFORMED, detail=path)
            owed = doc.get("open") or []
            return ProbeResult(state=ProbeState.OK,
                               owed=[{"item": item} for item in owed])
        return probe

    def owed(self) -> tuple[ReadState, list[str]]:
        names = sorted(self.components)
        built = [Component(name=n, probe=self._probe(self.components[n]))
                 for n in names]
        result = fold(built, expected=tuple(names))
        mapped = {
            ObligationState.DATA: ReadState.DATA,
            ObligationState.CLEAR: ReadState.CLEAR,
            ObligationState.UNKNOWN: ReadState.UNKNOWN,
            ObligationState.INVALID: ReadState.INVALID,
        }[result.state]
        # The gates assert on component NAMES that owe work; production returns
        # the owed items. Recover the names by re-probing the OK components —
        # cheap against the in-memory fixture, and it keeps the gate bodies
        # untouched rather than rewriting their assertions.
        owing = [n for n in names
                 if mapped is ReadState.DATA and self._probe(
                     self.components[n])().owed]
        return mapped, owing


@pytest.fixture
def store() -> FakeStore:
    """Gate 8 also reads the queue (to show it is empty), so the store needs the
    same records config the queue gates seed — otherwise the queue reports
    INVALID config and the gate fails for a reason that has nothing to do with
    the fold under test."""
    value = FakeStore()
    value.seed(
        config_path(gates.TEAM),
        json.dumps({"data_type": "MomentAnnotation/test",
                    "api_version": "v1alpha1"}),
    )
    return value


def _bind(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gates, "ObligationFold", EngineObligationFold)


def test_engine_gate_8(monkeypatch: pytest.MonkeyPatch, store: FakeStore):
    """Gate 8 against production: a lost wake record does not lose the duty."""
    _bind(monkeypatch)
    gates.test_gate_8_obligation_fold_rediscovers_a_lost_wake(store)


def test_engine_gate_10(monkeypatch: pytest.MonkeyPatch, store: FakeStore):
    """Gate 10 against production: one dark component and CLEAR is unsayable."""
    _bind(monkeypatch)
    gates.test_gate_10_unreadable_component_makes_nothing_owed_unsayable(store)


def test_binding_is_not_vacuous(store: FakeStore):
    """Prove the adapter really reaches the production fold.

    If this file ever silently fell back to the reference model, gates 8 and 10
    would keep passing and mean nothing. The production fold has a property the
    reference one does not — a component omitted from ``expected`` is UNKNOWN —
    so exercising it here shows which implementation answered.
    """
    store.seed("a.json", json.dumps({"open": []}))
    f = EngineObligationFold(store, {"a": "a.json"})
    assert f.owed()[0] is ReadState.CLEAR

    built = [Component(name="a", probe=lambda: ProbeResult(state=ProbeState.OK))]
    result = fold(built, expected=("a", "never-offered"))
    assert result.state is ObligationState.UNKNOWN
    assert result.degraded == ["never-offered"]
