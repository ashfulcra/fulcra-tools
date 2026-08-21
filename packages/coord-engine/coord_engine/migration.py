"""Fail-closed migration and release evidence for coord-engine v2.

This module deliberately does not activate v2 or bump the package version.
It supplies the bootstrap reader, the two-host lag instrument, and the pure
release fence that Unit 6 needs before those irreversible rollout edits are
licensed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
import time
import uuid
from typing import Any, Callable, Mapping, Optional

from . import generation, jsonutil


TARGET_VERSION = "2.0.0"

# Exclusion is evidence, not omission. Values are the operator-approved reason
# class; the evidence document must additionally carry measured timestamps/ages
# for every stale host before activation can become READY.
REQUIRED_EXCLUSIONS = {
    "MacBookPro.localdomain": "fresh v1.6.9 excluded by Ash operator ruling",
    "coord-reconcile:vm": "stale reconciliation",
    "coord-boss": "stale reconciliation",
    "coord-maintainer": "stale reconciliation",
    "Mac.localdomain": "stale reconciliation",
    "DeskbookPro.local": "stale reconciliation",
    "cloud-claudecode-website": "stale reconciliation",
    "home-network-maintainer": "stale reconciliation",
}


@dataclass(frozen=True)
class BootstrapResult:
    state: str
    sections: Mapping[str, Mapping[str, Any]]
    authoritative: bool = False
    reason: Optional[str] = None


@dataclass(frozen=True)
class ActivationDecision:
    state: str
    ready: bool
    release_complete: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class LagMeasurement:
    state: str
    host: str
    credentialed: bool
    observed_seconds: Optional[float]
    measured_at: str
    probe_id: str
    reason: Optional[str] = None

    @property
    def rc(self) -> int:
        return 0 if self.state == "DATA" else 3

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": "coord.feed-visibility-lag.v1",
            "state": self.state,
            "host": self.host,
            "credentialed": self.credentialed,
            "observed_seconds": self.observed_seconds,
            "measured_at": self.measured_at,
            "probe_id": self.probe_id,
            "reason": self.reason,
        }


def read_v1_bootstrap(raw: Any) -> BootstrapResult:
    """Read the legacy aggregate as non-authoritative bootstrap input only."""
    try:
        doc = json.loads(raw)
    except (TypeError, ValueError):
        return BootstrapResult("UNKNOWN", {}, reason="v1 aggregate malformed")
    if not isinstance(doc, dict):
        return BootstrapResult("UNKNOWN", {}, reason="v1 aggregate schema unsupported")
    # Early v1 aggregates predate the explicit top-level schema stamp.  They
    # remain licensed downgrade/bootstrap input; an explicit non-v1 stamp does
    # not.  This distinction preserves incremental rebuilds without allowing a
    # newer aggregate to masquerade as legacy authority.
    schema = doc.get("schema")
    if schema is not None and schema != "coord.teams.summaries.v1":
        return BootstrapResult("UNKNOWN", {}, reason="v1 aggregate schema unsupported")
    rows, reviews, forge = doc.get("rows"), doc.get("reviews"), doc.get("forge")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        return BootstrapResult("UNKNOWN", {}, reason="v1 task rows unavailable")
    sections: dict[str, Mapping[str, Any]] = {
        "tasks": {"rows": [dict(row) for row in rows]},
    }
    if reviews is not None:
        if (not isinstance(reviews, Mapping) or reviews.get("complete") is not True
                or generation.validated_review_projection(reviews) is None):
            return BootstrapResult("UNKNOWN", {}, reason="v1 review projection unavailable")
        sections["reviews"] = dict(reviews)
    if forge is not None:
        if (not isinstance(forge, Mapping) or forge.get("complete") is not True
                or generation.validated_forge_projection(forge) is None):
            return BootstrapResult("UNKNOWN", {}, reason="v1 forge projection unavailable")
        sections["forge"] = dict(forge)
    return BootstrapResult("DATA", sections)


def evaluate_activation(
    *, hosts: list[Mapping[str, Any]], configured_epsilon_seconds: Any,
    fleet: Mapping[str, Mapping[str, Any]], fleet_sla_seconds: Any,
    exclusions: Mapping[str, Mapping[str, Any]],
    cas_supported: bool,
) -> ActivationDecision:
    """Pure schema-2/release fence. No caller may infer readiness from merge."""
    reasons: list[str] = []
    measured = {
        str(row.get("host")): row for row in hosts
        if isinstance(row, Mapping) and row.get("credentialed") is True
        and isinstance(row.get("host"), str) and row.get("host")
        and isinstance(row.get("observed_max_seconds"), (int, float))
        and not isinstance(row.get("observed_max_seconds"), bool)
        and math.isfinite(float(row["observed_max_seconds"]))
        and float(row["observed_max_seconds"]) >= 0
        and isinstance(row.get("measured_at"), str) and row.get("measured_at")
    }
    if len(measured) < 2:
        reasons.append("two distinct credentialed host measurements required")
    sla_valid = (isinstance(fleet_sla_seconds, (int, float))
            and not isinstance(fleet_sla_seconds, bool)
            and math.isfinite(float(fleet_sla_seconds))
            and fleet_sla_seconds > 0)
    if not sla_valid:
        reasons.append("explicit positive fleet SLA required")
    if set(exclusions) != set(REQUIRED_EXCLUSIONS):
        reasons.append("named fleet exclusions are incomplete or contain silent exclusions")
    else:
        for name, reason in REQUIRED_EXCLUSIONS.items():
            evidence = exclusions[name]
            if not isinstance(evidence, Mapping) or evidence.get("reason") != reason:
                reasons.append(f"fleet exclusion evidence invalid for {name}")
                continue
            if name != "MacBookPro.localdomain" and (
                    not isinstance(evidence.get("last_reconciled_at"), str)
                    or not evidence.get("last_reconciled_at")
                    or not isinstance(evidence.get("age_seconds"), (int, float))
                    or isinstance(evidence.get("age_seconds"), bool)
                    or evidence.get("age_seconds") < 0):
                reasons.append(
                    f"stale reconciliation timestamp/age missing for {name}"
                )
            elif (sla_valid and name != "MacBookPro.localdomain"
                  and float(evidence["age_seconds"]) <= float(fleet_sla_seconds)):
                reasons.append(f"stale exclusion is within fleet SLA for {name}")
    live_fleet: dict[str, str] = {}
    if not isinstance(fleet, Mapping):
        reasons.append("fleet census unavailable")
    elif sla_valid:
        for name, row in fleet.items():
            if (not isinstance(name, str) or not name
                    or not isinstance(row, Mapping)
                    or not isinstance(row.get("engine_version"), str)
                    or not row.get("engine_version")
                    or not isinstance(row.get("last_reconciled_at"), str)
                    or not row.get("last_reconciled_at")
                    or not isinstance(row.get("age_seconds"), (int, float))
                    or isinstance(row.get("age_seconds"), bool)
                    or row.get("age_seconds") < 0):
                reasons.append(f"fleet census evidence invalid for {name}")
                continue
            age = float(row["age_seconds"])
            if age <= float(fleet_sla_seconds):
                if name not in exclusions:
                    live_fleet[name] = str(row["engine_version"])
            elif name not in exclusions:
                reasons.append(f"stale host silently excluded: {name}")
        if "MacBookPro.localdomain" not in fleet:
            reasons.append("operator-excluded fresh MacBookPro.localdomain absent from census")
    if reasons:
        return ActivationDecision("UNKNOWN", False, False, tuple(reasons))
    if (not isinstance(configured_epsilon_seconds, (int, float))
            or isinstance(configured_epsilon_seconds, bool)
            or not math.isfinite(float(configured_epsilon_seconds))
            or configured_epsilon_seconds <= 0):
        return ActivationDecision("UNKNOWN", False, False,
                                  ("configured epsilon unavailable",))
    observed_max = max(float(row["observed_max_seconds"]) for row in measured.values())
    if float(configured_epsilon_seconds) < observed_max:
        return ActivationDecision("REFUSED", False, False,
                                  ("epsilon is below observed visibility lag",))
    mixed = sorted(
        name for name, version in live_fleet.items()
        if name not in exclusions and version != TARGET_VERSION
    )
    if mixed:
        return ActivationDecision("REFUSED", False, False,
                                  ("mixed live fleet: " + ", ".join(mixed),))
    if not live_fleet:
        return ActivationDecision("UNKNOWN", False, False,
                                  ("live fleet evidence absent",))
    if cas_supported is not True:
        return ActivationDecision("REFUSED", False, False,
                                  ("schema-2 activation requires proven CAS",))
    # This licenses the activation edit only. Release completion additionally
    # requires publish, adoption and two-host live command verification.
    return ActivationDecision("READY", True, False, ())


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _rows_from_update_result(value: Any) -> Optional[list[Mapping[str, Any]]]:
    if isinstance(value, list):
        return value if all(isinstance(row, Mapping) for row in value) else None
    if isinstance(value, Mapping):
        rows = value.get("file_changes", value.get("files"))
        return rows if isinstance(rows, list) and all(isinstance(row, Mapping) for row in rows) else None
    return None


def measure_feed_visibility_lag(
    transport: Any, team: str, host: str, *, timeout_seconds: float = 30.0,
    poll_seconds: float = 0.25,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> LagMeasurement:
    """Write one nonce and bound the time until that exact path is feed-visible."""
    measured_at = _iso(now())
    probe_id = uuid.uuid4().hex
    path = f"team/{team}/_coord/projections/lag-probes/{probe_id}.json"
    content = jsonutil.dumps({
        "schema": "coord.feed-visibility-lag-probe.v1",
        "id": probe_id,
        "host": host,
        "written_at": measured_at,
    })
    if (not isinstance(timeout_seconds, (int, float))
            or isinstance(timeout_seconds, bool)
            or not math.isfinite(float(timeout_seconds))
            or timeout_seconds <= 0
            or not isinstance(poll_seconds, (int, float))
            or isinstance(poll_seconds, bool)
            or not math.isfinite(float(poll_seconds))
            or poll_seconds <= 0):
        return LagMeasurement("UNKNOWN", host, False, None, measured_at,
                              probe_id, "positive finite bounds required")
    start = monotonic()

    class _Bound:
        def remaining(self) -> float:
            return max(0.0, float(timeout_seconds) - (monotonic() - start))

        def expired(self) -> bool:
            return self.remaining() <= 0.0

    bound = _Bound()
    try:
        persisted = transport.write(path, content)
    except Exception:
        persisted = False
    if persisted is not True:
        return LagMeasurement("UNKNOWN", host, False, None, measured_at,
                              probe_id, "probe write did not persist")
    while True:
        elapsed = monotonic() - start
        if elapsed >= timeout_seconds:
            return LagMeasurement("UNKNOWN", host, True, None, measured_at,
                                  probe_id, "feed visibility bound expired")
        try:
            reader = getattr(transport, "data_updates", None)
            if callable(reader):
                try:
                    result = reader(measured_at, deadline=bound)
                except TypeError:
                    result = reader(measured_at)
            else:
                result = transport.updates(measured_at, team=team)
        except Exception:
            result = None
        rows = _rows_from_update_result(result)
        if rows is None:
            return LagMeasurement("UNKNOWN", host, True, None, measured_at,
                                  probe_id, "data-updates envelope unavailable")
        if any(str(row.get("path", row.get("full_name", ""))).lstrip("/") == path
               for row in rows):
            return LagMeasurement("DATA", host, True, monotonic() - start,
                                  measured_at, probe_id)
        sleep(min(float(poll_seconds), timeout_seconds - elapsed))
