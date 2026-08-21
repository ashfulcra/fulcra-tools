"""Fail-closed migration and release evidence for coord-engine v2.

This module deliberately does not activate v2 or bump the package version.
It supplies the bootstrap reader, the two-host lag instrument, and the pure
release fence that Unit 6 needs before those irreversible rollout edits are
licensed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import re
import time
import uuid
from typing import Any, Callable, Mapping, Optional

from . import change_detection, classifier, generation, jsonutil


TARGET_VERSION = "2.0.0"
AGE_TOLERANCE_SECONDS = 5.0
_CREDENTIAL_PROVENANCE = re.compile(r"^sha256:[0-9a-f]{64}$")
_ATTESTED_HOST_IDENTITY = re.compile(r"^coord-reconcile:[A-Za-z0-9:_.-]+$")

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
REQUIRED_EXCLUSION_PROVENANCE = {
    name: ("Ash operator ruling" if name == "MacBookPro.localdomain"
           else "measured fleet census")
    for name in REQUIRED_EXCLUSIONS
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
class MeasurementIdentity:
    state: str
    host_identity: Optional[str] = None
    principal_identity: Optional[str] = None
    credential_provenance: Optional[str] = None
    reason: Optional[str] = None

    @property
    def credentialed(self) -> bool:
        return (
            self.state == "DATA"
            and isinstance(self.credential_provenance, str)
            and _CREDENTIAL_PROVENANCE.fullmatch(self.credential_provenance) is not None
        )


@dataclass(frozen=True)
class LagMeasurement:
    state: str
    host_identity: Optional[str]
    display_label: Optional[str]
    principal_identity: Optional[str]
    credential_provenance: Optional[str]
    credentialed: bool
    observed_seconds: Optional[float]
    measured_at: str
    probe_id: str
    event_at: Optional[str] = None
    observed_at: Optional[str] = None
    update_id: Optional[str] = None
    reason: Optional[str] = None

    @property
    def rc(self) -> int:
        return 0 if self.state == "DATA" else 3

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": "coord.feed-visibility-lag.v1",
            "state": self.state,
            "host_identity": self.host_identity,
            "display_label": self.display_label,
            "principal_identity": self.principal_identity,
            "credential_provenance": self.credential_provenance,
            "credentialed": self.credentialed,
            "observed_seconds": self.observed_seconds,
            "measured_at": self.measured_at,
            "probe_id": self.probe_id,
            "event_at": self.event_at,
            "observed_at": self.observed_at,
            "update_id": self.update_id,
            "reason": self.reason,
        }

    @classmethod
    def unknown(
        cls, *, identity: MeasurementIdentity, display_label: Optional[str],
        reason: str, measured_at: str, probe_id: str,
    ) -> "LagMeasurement":
        return cls(
            "UNKNOWN", identity.host_identity, display_label,
            identity.principal_identity, identity.credential_provenance,
            identity.credentialed, None, measured_at, probe_id, reason=reason,
        )


def trusted_measurement_identity(
    transport: Any, *, persisted: classifier.PersistedIdentity,
    hostname: Callable[[], str],
) -> MeasurementIdentity:
    """Bind one machine, persisted principal, and real transport credential.

    Display labels are intentionally absent from this seam. The credential is
    fingerprinted and discarded; evidence receives provenance, never the token.
    """
    if (not isinstance(persisted, classifier.PersistedIdentity)
            or persisted.state is not classifier.PersistedIdentityState.PRESENT
            or not isinstance(persisted.identity, str)
            or not persisted.identity.strip()):
        return MeasurementIdentity(
            "UNKNOWN", reason="persisted principal identity unavailable"
        )
    try:
        raw_host = hostname()
    except Exception:
        return MeasurementIdentity("UNKNOWN", reason="machine identity unavailable")
    if not isinstance(raw_host, str):
        return MeasurementIdentity("UNKNOWN", reason="machine identity malformed")
    safe_host, _rewritten = classifier.sanitize_hostname(raw_host)
    if not safe_host:
        return MeasurementIdentity("UNKNOWN", reason="machine identity unusable")
    token_reader = getattr(transport, "_access_token", None)
    if not callable(token_reader):
        return MeasurementIdentity("UNKNOWN", reason="credential provenance unavailable")
    try:
        token = token_reader()
    except Exception:
        token = None
    if not isinstance(token, str) or not token.strip():
        return MeasurementIdentity("UNKNOWN", reason="credential provenance unavailable")
    fingerprint = hashlib.sha256(token.strip().encode("utf-8")).hexdigest()
    return MeasurementIdentity(
        "DATA", f"coord-reconcile:{safe_host}", persisted.identity.strip(),
        f"sha256:{fingerprint}",
    )


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
    evidence_measured_at: Any,
    exclusions: Mapping[str, Mapping[str, Any]],
    cas_supported: bool,
) -> ActivationDecision:
    """Pure schema-2/release fence. No caller may infer readiness from merge."""
    reasons: list[str] = []
    evidence_instant = _parse_aware_utc(evidence_measured_at)
    if evidence_instant is None:
        reasons.append("fleet evidence measured_at unavailable or malformed")
    measured: dict[str, Mapping[str, Any]] = {}
    if not isinstance(hosts, list):
        reasons.append("host measurement evidence unavailable")
        hosts = []
    for row in hosts:
        if not isinstance(row, Mapping):
            reasons.append("host measurement evidence malformed")
            continue
        host_identity = row.get("host_identity")
        principal = row.get("principal_identity")
        provenance = row.get("credential_provenance")
        observed = row.get("observed_max_seconds")
        measured_at = _parse_aware_utc(row.get("measured_at"))
        if (row.get("credentialed") is not True
                or not isinstance(host_identity, str) or not host_identity.strip()
                or _ATTESTED_HOST_IDENTITY.fullmatch(host_identity.strip()) is None
                or not isinstance(principal, str) or not principal.strip()
                or not isinstance(provenance, str)
                or _CREDENTIAL_PROVENANCE.fullmatch(provenance) is None
                or not isinstance(observed, (int, float))
                or isinstance(observed, bool) or not math.isfinite(float(observed))
                or float(observed) < 0 or measured_at is None):
            reasons.append("credentialed host measurement evidence malformed")
            continue
        host_identity = host_identity.strip()
        if host_identity in measured:
            reasons.append(f"duplicate host measurement identity: {host_identity}")
            continue
        measured[host_identity] = row
    if len(measured) < 2:
        reasons.append("two distinct credentialed host measurements required")
    sla_valid = (isinstance(fleet_sla_seconds, (int, float))
            and not isinstance(fleet_sla_seconds, bool)
            and math.isfinite(float(fleet_sla_seconds))
            and fleet_sla_seconds > 0)
    if not sla_valid:
        reasons.append("explicit positive fleet SLA required")
    if not isinstance(exclusions, Mapping):
        reasons.append("named fleet exclusions unavailable")
        exclusions = {}
    if set(exclusions) != set(REQUIRED_EXCLUSIONS):
        reasons.append("named fleet exclusions are incomplete or contain silent exclusions")
    else:
        for name, reason in REQUIRED_EXCLUSIONS.items():
            evidence = exclusions[name]
            if (not isinstance(evidence, Mapping)
                    or evidence.get("host_identity") != name
                    or evidence.get("reason") != reason
                    or evidence.get("provenance") != REQUIRED_EXCLUSION_PROVENANCE[name]):
                reasons.append(f"fleet exclusion evidence invalid for {name}")
                continue
            if name != "MacBookPro.localdomain":
                if evidence_instant is None or not _age_evidence_valid(
                        evidence, evidence_instant):
                    reasons.append(
                        f"stale reconciliation timestamp/age invalid for {name}"
                    )
                elif sla_valid and float(evidence["age_seconds"]) <= float(fleet_sla_seconds):
                    reasons.append(f"stale exclusion is within fleet SLA for {name}")
    live_fleet: dict[str, str] = {}
    seen_fleet_identities: set[str] = set()
    if not isinstance(fleet, Mapping):
        reasons.append("fleet census unavailable")
    elif sla_valid and evidence_instant is not None:
        for name, row in fleet.items():
            if (not isinstance(name, str) or not name
                    or not isinstance(row, Mapping)
                    or row.get("host_identity") != name
                    or not isinstance(row.get("engine_version"), str)
                    or not row.get("engine_version")
                    or not _age_evidence_valid(row, evidence_instant)):
                reasons.append(f"fleet census evidence invalid for {name}")
                continue
            identity = str(row["host_identity"])
            if identity in seen_fleet_identities:
                reasons.append(f"duplicate fleet host identity: {identity}")
                continue
            seen_fleet_identities.add(identity)
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


def _parse_aware_utc(value: Any) -> Optional[datetime]:
    normalized = change_detection._instant({"uploaded_at": value}, "uploaded")
    return normalized[0] if normalized is not None else None


def _age_evidence_valid(row: Mapping[str, Any], measured_at: datetime) -> bool:
    reconciled = _parse_aware_utc(row.get("last_reconciled_at"))
    age = row.get("age_seconds")
    if (reconciled is None or not isinstance(age, (int, float))
            or isinstance(age, bool) or not math.isfinite(float(age))
            or float(age) < 0):
        return False
    derived = (measured_at - reconciled).total_seconds()
    return derived >= 0 and abs(derived - float(age)) <= AGE_TOLERANCE_SECONDS


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def measure_feed_visibility_lag(
    transport: Any, team: str, display_label: str, *, identity: MeasurementIdentity,
    timeout_seconds: float = 30.0,
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
        "host_identity": identity.host_identity,
        "principal_identity": identity.principal_identity,
        "display_label": display_label,
        "written_at": measured_at,
    })
    if not identity.credentialed:
        return LagMeasurement.unknown(
            identity=identity, display_label=display_label,
            reason=identity.reason or "trusted measurement identity unavailable",
            measured_at=measured_at, probe_id=probe_id,
        )
    if (not isinstance(timeout_seconds, (int, float))
            or isinstance(timeout_seconds, bool)
            or not math.isfinite(float(timeout_seconds))
            or timeout_seconds <= 0
            or not isinstance(poll_seconds, (int, float))
            or isinstance(poll_seconds, bool)
            or not math.isfinite(float(poll_seconds))
            or poll_seconds <= 0):
        return LagMeasurement.unknown(
            identity=identity, display_label=display_label,
            reason="positive finite bounds required", measured_at=measured_at,
            probe_id=probe_id,
        )
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
        return LagMeasurement.unknown(
            identity=identity, display_label=display_label,
            reason="probe write did not persist", measured_at=measured_at,
            probe_id=probe_id,
        )
    while True:
        elapsed = monotonic() - start
        if elapsed >= timeout_seconds:
            return LagMeasurement.unknown(
                identity=identity, display_label=display_label,
                reason="feed visibility bound expired", measured_at=measured_at,
                probe_id=probe_id,
            )
        try:
            reader = getattr(transport, "data_updates", None)
            if not callable(reader):
                raise TypeError("authoritative data-updates seam unavailable")
            result = reader(measured_at, deadline=bound)
        except Exception:
            result = None
        if not isinstance(result, Mapping):
            return LagMeasurement.unknown(
                identity=identity, display_label=display_label,
                reason="data-updates envelope unavailable", measured_at=measured_at,
                probe_id=probe_id,
            )
        window, reason = change_detection._feed_window(result, measured_at)
        rows = result.get("file_changes")
        if window is None or not isinstance(rows, list):
            return LagMeasurement.unknown(
                identity=identity, display_label=display_label,
                reason=reason or "data-updates file_changes unavailable",
                measured_at=measured_at, probe_id=probe_id,
            )
        start_at = _parse_aware_utc(window[0])
        through_at = _parse_aware_utc(window[1])
        matched: list[tuple[str, datetime, str]] = []
        for row in rows:
            if not isinstance(row, Mapping):
                reason = "data-updates contains malformed lifecycle row"
                break
            raw_path = row.get("path", row.get("full_name"))
            state = row.get("state")
            update_id = change_detection._file_identity(row)
            instant = change_detection._instant(row, state) if isinstance(state, str) else None
            if (not isinstance(raw_path, str) or not raw_path.strip()
                    or state not in ("uploaded", "archived", "deleted")
                    or update_id is None or instant is None
                    or start_at is None or through_at is None
                    or not (start_at <= instant[0] <= through_at)):
                reason = "data-updates lifecycle row is unattested or malformed"
                break
            normalized_path = raw_path.strip().lstrip("/")
            if normalized_path == path:
                if state != "uploaded":
                    reason = "probe lifecycle is not uploaded"
                    break
                matched.append((update_id, instant[0], instant[1]))
        else:
            reason = ""
        if reason:
            return LagMeasurement.unknown(
                identity=identity, display_label=display_label, reason=reason,
                measured_at=measured_at, probe_id=probe_id,
            )
        if matched:
            if len(matched) != 1:
                return LagMeasurement.unknown(
                    identity=identity, display_label=display_label,
                    reason="probe update identity is not unique",
                    measured_at=measured_at, probe_id=probe_id,
                )
            update_id, event_instant, event_at = matched[0]
            observed_instant = now().astimezone(timezone.utc)
            lag = (observed_instant - event_instant).total_seconds()
            if not math.isfinite(lag) or lag < 0:
                return LagMeasurement.unknown(
                    identity=identity, display_label=display_label,
                    reason="authoritative event timestamp is after observation",
                    measured_at=measured_at, probe_id=probe_id,
                )
            return LagMeasurement(
                "DATA", identity.host_identity, display_label,
                identity.principal_identity, identity.credential_provenance,
                True, lag, measured_at, probe_id, event_at,
                _iso(observed_instant), update_id,
            )
        sleep(min(float(poll_seconds), timeout_seconds - elapsed))
