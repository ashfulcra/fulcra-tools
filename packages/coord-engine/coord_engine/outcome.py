"""Typed command outcomes for coord-engine v2.

The outcome is sealed before either renderer runs.  Renderers project this one
fact; they never infer health or exit status independently.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable, Optional

from . import jsonutil


class CoverageState(str, Enum):
    NOT_RUN = "NOT_RUN"
    CLEAR = "CLEAR"
    DATA = "DATA"
    UNKNOWN = "UNKNOWN"


class OutcomeState(str, Enum):
    NOT_RUN = "NOT_RUN"
    CLEAR = "CLEAR"
    DATA = "DATA"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class SurfaceCoverage:
    surface: str
    state: CoverageState
    required: bool = True
    reason: Optional[str] = None

    def as_dict(self) -> dict[str, Any]:
        row: dict[str, Any] = {
            "surface": self.surface,
            "state": self.state.value,
            "required": self.required,
        }
        if self.reason:
            row["reason"] = self.reason
        return row


@dataclass(frozen=True)
class CommandOutcome:
    state: OutcomeState
    rows: tuple[Any, ...]
    coverage: tuple[SurfaceCoverage, ...]
    source: Optional[str] = None
    contract: int = 2

    @classmethod
    def from_surfaces(
        cls,
        *,
        rows: Iterable[Any],
        coverage: Iterable[SurfaceCoverage],
        source: Optional[str] = None,
        contract: int = 2,
    ) -> "CommandOutcome":
        ordered = tuple(sorted(coverage, key=lambda item: item.surface))
        required = tuple(item for item in ordered if item.required)
        row_tuple = tuple(rows)
        if any(item.state is CoverageState.UNKNOWN for item in required):
            state = OutcomeState.UNKNOWN
        elif any(item.state is CoverageState.NOT_RUN for item in required):
            state = OutcomeState.NOT_RUN
        elif row_tuple or any(item.state is CoverageState.DATA for item in required):
            state = OutcomeState.DATA
        else:
            state = OutcomeState.CLEAR
        return cls(
            state=state,
            rows=row_tuple,
            coverage=ordered,
            source=source,
            contract=contract,
        )

    @property
    def rc(self) -> int:
        return 0 if self.state in (OutcomeState.CLEAR, OutcomeState.DATA) else 3

    def as_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "contract": self.contract,
            "state": self.state.value,
        }
        if self.source is not None:
            value["source"] = self.source
        value["coverage"] = [item.as_dict() for item in self.coverage]
        value["rows"] = list(self.rows)
        return value

    def render_json(self) -> str:
        return jsonutil.dumps(self.as_dict())

    def render_text(self) -> str:
        lines = [self.state.value]
        for item in self.coverage:
            line = f"{item.surface}: {item.state.value}"
            if item.reason:
                line += f" ({item.reason})"
            lines.append(line)
        return "\n".join(lines)
