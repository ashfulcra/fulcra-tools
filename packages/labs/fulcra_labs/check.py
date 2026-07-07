"""Double-extraction cross-check.

The SKILL asks the model to extract the SAME PDF twice, independently. This
module compares the two passes: observations that agree on (marker, value,
unit) become the trusted, ingestable set; disagreements are surfaced for a
targeted re-read. Two independent transcription passes agreeing is the
model-side guard that complements the code-side validation.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from . import markers as _markers
from .logging_setup import get_logger
from .validate import fmt_canonical, parse_collected_at, parse_value

log = get_logger(__name__)

_COLLECTED_SENTINEL = "__collected_at__"


def _marker_match_key(marker_raw: str) -> str:
    """Group observations across passes: the canonical key when the name
    resolves, else the normalized raw spelling (so identical raw spellings
    still line up even for an unregistered marker)."""
    res = _markers.resolve_marker(marker_raw or "")
    if res.marker is not None:
        return res.marker.key
    if res.candidates:
        return "|".join(sorted(res.candidates))
    return _markers.normalize_alias(marker_raw or "") or "?"


def _value_unit_key(obs: dict) -> tuple[str, str | None]:
    """A comparison key for a value+unit that ignores insignificant
    formatting (thousands commas, unit casing / micro-sign) but preserves the
    numeric value, its qualifier, and the printed unit's meaning."""
    num, qualifier = parse_value(str(obs.get("value_raw", "")))
    if num is None:
        vkey = f"raw:{str(obs.get('value_raw', '')).strip().lower()}"
    else:
        vkey = f"{fmt_canonical(num)}:{qualifier or ''}"
    return vkey, _markers.normalize_unit(obs.get("unit_raw"))


@dataclass
class CheckResult:
    lab: str | None
    report_date: str | None
    collected_at: str | None
    observations: list[dict]           # the agreed set (schema-valid)
    disagreements: list[dict] = field(default_factory=list)

    def agreed_extraction(self) -> dict:
        """The agreed observations as a valid extraction dict — the direct
        input to ``fulcra-labs ingest``."""
        return {
            "lab": self.lab,
            "report_date": self.report_date,
            "collected_at": self.collected_at,
            "observations": self.observations,
        }

    def to_dict(self) -> dict:
        d = self.agreed_extraction()
        d["disagreements"] = self.disagreements
        d["agreed_count"] = len(self.observations)
        d["disagreement_count"] = len(self.disagreements)
        return d


def cross_check(pass_a: dict, pass_b: dict) -> CheckResult:
    """Compare two independent extraction passes of the same report."""
    obs_a = pass_a.get("observations") or []
    obs_b = pass_b.get("observations") or []

    by_key_a: dict[str, list[dict]] = defaultdict(list)
    by_key_b: dict[str, list[dict]] = defaultdict(list)
    for o in obs_a:
        by_key_a[_marker_match_key(str(o.get("marker_raw", "")))].append(o)
    for o in obs_b:
        by_key_b[_marker_match_key(str(o.get("marker_raw", "")))].append(o)

    agreed: list[dict] = []
    disagreements: list[dict] = []

    # Collection date is report-level; a mismatch taints every row, so flag it.
    _, iso_a = parse_collected_at(pass_a.get("collected_at") or pass_a.get("report_date"))
    _, iso_b = parse_collected_at(pass_b.get("collected_at") or pass_b.get("report_date"))
    if iso_a != iso_b:
        disagreements.append({
            "marker": _COLLECTED_SENTINEL,
            "reason": "collection date differs between passes",
            "pass_a": iso_a,
            "pass_b": iso_b,
        })

    for key in sorted(set(by_key_a) | set(by_key_b)):
        a_list = by_key_a.get(key, [])
        b_list = by_key_b.get(key, [])
        a_multiset = sorted(_value_unit_key(o) for o in a_list)
        b_multiset = sorted(_value_unit_key(o) for o in b_list)
        if a_list and b_list and a_multiset == b_multiset:
            # Identical transcription in both passes → trust pass A's rows.
            agreed.extend(a_list)
            continue
        reason = (
            "present in only one pass"
            if not a_list or not b_list
            else "value/unit differ between passes"
        )
        disagreements.append({
            "marker": key,
            "reason": reason,
            "pass_a": [{"marker_raw": o.get("marker_raw"),
                        "value_raw": o.get("value_raw"),
                        "unit_raw": o.get("unit_raw")} for o in a_list],
            "pass_b": [{"marker_raw": o.get("marker_raw"),
                        "value_raw": o.get("value_raw"),
                        "unit_raw": o.get("unit_raw")} for o in b_list],
        })

    log.info("cross-check: %d agreed, %d disagreements", len(agreed), len(disagreements))
    return CheckResult(
        lab=pass_a.get("lab") or pass_b.get("lab"),
        report_date=pass_a.get("report_date") or pass_b.get("report_date"),
        collected_at=iso_a,
        observations=agreed,
        disagreements=disagreements,
    )
