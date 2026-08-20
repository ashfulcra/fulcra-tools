"""Unit 1: one typed outcome seals coverage, renderers, and exit status."""

from __future__ import annotations

import json

from coord_engine import cli
from coord_engine.outcome import (
    CommandOutcome,
    CoverageState,
    OutcomeState,
    SurfaceCoverage,
)


def test_required_unknown_makes_state_and_rc_unknown():
    """Mutation caught: treating required UNKNOWN as an empty/clean result."""
    result = CommandOutcome.from_surfaces(
        rows=[],
        coverage=[SurfaceCoverage(
            "tasks", CoverageState.UNKNOWN, required=True,
            reason="budget-cut")],
        source="raw-scan",
    )

    assert result.state is OutcomeState.UNKNOWN
    assert result.rc == 3
    assert result.as_dict() == {
        "contract": 2,
        "state": "UNKNOWN",
        "source": "raw-scan",
        "coverage": [{
            "surface": "tasks",
            "state": "UNKNOWN",
            "required": True,
            "reason": "budget-cut",
        }],
        "rows": [],
    }


def test_optional_not_run_does_not_poison_required_clear_surface():
    """Mutation caught: collapsing NOT_RUN into UNKNOWN or checked-clear."""
    result = CommandOutcome.from_surfaces(
        rows=[],
        coverage=[
            SurfaceCoverage("tasks", CoverageState.CLEAR, required=True),
            SurfaceCoverage("forge", CoverageState.NOT_RUN, required=False),
        ],
        source="projection",
    )

    assert result.state is OutcomeState.CLEAR
    assert result.rc == 0
    assert [row["state"] for row in result.as_dict()["coverage"]] == [
        "NOT_RUN", "CLEAR"]


def test_complete_rows_make_data_and_text_json_share_the_sealed_state():
    """Mutation caught: a renderer deriving a different state from the outcome."""
    result = CommandOutcome.from_surfaces(
        rows=[{"id": "task-1", "title": "Act"}],
        coverage=[SurfaceCoverage("tasks", CoverageState.DATA, required=True)],
        source="projection",
    )

    machine = json.loads(result.render_json())
    text = result.render_text()
    assert result.state is OutcomeState.DATA
    assert result.rc == 0
    assert machine["state"] == "DATA"
    assert machine["rows"] == [{"id": "task-1", "title": "Act"}]
    assert text.splitlines()[0] == "DATA"
    assert "tasks: DATA" in text


def test_coverage_order_is_deterministic_by_surface():
    """Mutation caught: renderer bytes depending on builder insertion order."""
    result = CommandOutcome.from_surfaces(
        rows=[],
        coverage=[
            SurfaceCoverage("reviews", CoverageState.CLEAR),
            SurfaceCoverage("tasks", CoverageState.CLEAR),
            SurfaceCoverage("forge", CoverageState.CLEAR),
        ],
    )

    assert [row["surface"] for row in result.as_dict()["coverage"]] == [
        "forge", "reviews", "tasks"]


def test_class_a_adapter_seals_partial_coverage_as_typed_unknown():
    """Mutation caught: CLI health/rc continuing to bypass the typed spine."""
    typed, legacy = cli.class_a_outcome(
        [{"type": "review-fold-degraded", "scanned": 2, "total": 7}],
        source_type="needs-me-source",
    )

    assert isinstance(typed, CommandOutcome)
    assert typed.state is OutcomeState.UNKNOWN
    assert typed.rc == 3
    assert typed.coverage == (
        SurfaceCoverage(
            "review-fold-degraded", CoverageState.UNKNOWN,
            required=True, reason="budget-cut"),
    )
    assert legacy == {
        "health": "DEGRADED",
        "source": "raw-scan",
        "as_of": None,
        "degraded": ["review-fold-degraded"],
        "basis": ["budget-cut"],
        "scanned": 2,
        "total": 7,
    }
