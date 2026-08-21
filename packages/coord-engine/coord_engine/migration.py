"""Fail-closed migration and release evidence for coord-engine v2.

This module deliberately does not activate v2 or bump the package version.
It supplies the bootstrap reader, the two-host lag instrument, and the pure
release fence that Unit 6 needs before those irreversible rollout edits are
licensed.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import hashlib
import inspect
import json
import math
import re
import time
import uuid
from typing import Any, Callable, Mapping, Optional

from . import change_detection, classifier, generation, jsonutil, pin_currency, records


TARGET_VERSION = "2.0.0"
MEASUREMENT_SCHEMA = "coord.feed-visibility-lag.v1"
PROBE_SCHEMA = "coord.feed-visibility-lag-probe.v1"
AGE_TOLERANCE_SECONDS = 5.0
_CANONICAL_TEAM = re.compile(r"^[a-z0-9][a-z0-9_.-]*$")
_ATTESTED_HOST_IDENTITY = re.compile(r"^coord-reconcile:[A-Za-z0-9:_.-]+$")
_PROBE_ID = re.compile(r"^[0-9a-f]{32}$")
_PROVENANCE = re.compile(r"^evidence-sha256:[0-9a-f]{64}$")
_BUILD_IDENTITY = re.compile(r"^[0-9a-f]{40}$")
_PRINCIPAL_SOURCES = frozenset({"explicit", "env", "persisted"})
_AUTHORITY_BASE_FIELDS = ("data_type", "api_version")
_AUTHORITY_VERSIONED_FIELDS = (
    "protocol_version", "cursor_schema_version",
    "minimum_reader_version", "minimum_writer_version",
    "cursor_generation", "cursor_activated_at",
)
LAG_TOLERANCE_SECONDS = 0.001
FEED_QUERY_SKEW_SECONDS = 5.0

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
class LagMeasurement:
    state: str
    team: Optional[str]
    host_identity: Optional[str]
    display_label: Optional[str]
    principal_identity: Optional[str]
    principal_source: Optional[str]
    transport_authority: Optional[Mapping[str, Any]]
    probe_schema: str
    producer_build: Optional[str]
    credential_provenance: Optional[str]
    credentialed: bool
    observed_seconds: Optional[float]
    measured_at: str
    probe_id: str
    probe_path: str
    event_at: Optional[str] = None
    observed_at: Optional[str] = None
    update_id: Optional[str] = None
    reason: Optional[str] = None

    @property
    def rc(self) -> int:
        return 0 if self.state == "DATA" else 3

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": MEASUREMENT_SCHEMA,
            "state": self.state,
            "team": self.team,
            "host_identity": self.host_identity,
            "display_label": self.display_label,
            "principal_identity": self.principal_identity,
            "principal_source": self.principal_source,
            "transport_authority": (
                dict(self.transport_authority)
                if isinstance(self.transport_authority, Mapping) else None
            ),
            "probe_schema": self.probe_schema,
            "producer_build": self.producer_build,
            "credential_provenance": self.credential_provenance,
            "credentialed": self.credentialed,
            "observed_seconds": self.observed_seconds,
            "measured_at": self.measured_at,
            "probe_id": self.probe_id,
            "probe_path": self.probe_path,
            "event_at": self.event_at,
            "observed_at": self.observed_at,
            "update_id": self.update_id,
            "reason": self.reason,
        }

    @classmethod
    def unknown(
        cls, *, display_label: Optional[str], reason: str, measured_at: str,
        probe_id: str, team: Optional[str] = None, probe_path: str = "",
        host_identity: Optional[str] = None,
        principal_identity: Optional[str] = None,
        principal_source: Optional[str] = None,
        transport_authority: Optional[Mapping[str, Any]] = None,
        producer_build: Optional[str] = None,
    ) -> "LagMeasurement":
        return cls(
            state="UNKNOWN", team=team, host_identity=host_identity,
            display_label=display_label, principal_identity=principal_identity,
            principal_source=principal_source,
            transport_authority=transport_authority, probe_schema=PROBE_SCHEMA,
            producer_build=producer_build, credential_provenance=None,
            credentialed=False, observed_seconds=None, measured_at=measured_at,
            probe_id=probe_id, probe_path=probe_path, reason=reason,
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


_MEASUREMENT_KEYS = frozenset({
    "schema", "state", "team", "host_identity", "display_label",
    "principal_identity", "principal_source", "transport_authority",
    "probe_schema",
    "producer_build", "credential_provenance",
    "credentialed", "observed_seconds", "measured_at", "probe_id",
    "probe_path", "event_at", "observed_at", "update_id", "reason",
})


def _measurement_provenance(
    row: Mapping[str, Any], *, expired: Optional[Callable[[], bool]] = None,
) -> Optional[str]:
    payload = {key: value for key, value in row.items()
               if key != "credential_provenance"}
    try:
        encoded = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), allow_nan=False,
        )
    except (TypeError, ValueError):
        return None
    if expired is not None and expired():
        return None
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    if expired is not None and expired():
        return None
    return "evidence-sha256:" + digest


def _valid_update_id(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return str(uuid.UUID(value)) == value.lower()
    except (ValueError, AttributeError):
        return False


def _valid_transport_authority(value: Any) -> bool:
    if not isinstance(value, Mapping):
        return False
    keys = set(value)
    base = set(_AUTHORITY_BASE_FIELDS)
    versioned = base | set(_AUTHORITY_VERSIONED_FIELDS)
    if keys not in (base, versioned):
        return False
    for name in _AUTHORITY_BASE_FIELDS:
        item = value.get(name)
        if (not isinstance(item, str) or not item
                or item != item.strip()):
            return False
    try:
        parsed = records._parse_config(json.dumps(
            dict(value), sort_keys=True, separators=(",", ":"), allow_nan=False,
        ))
    except (TypeError, ValueError):
        return False
    if parsed is None:
        return False
    expected_mode = "legacy" if keys == base else "versioned"
    actual_mode = parsed.get("authority_mode", "legacy")
    return actual_mode == expected_mode and all(
        parsed.get(name) == value.get(name) for name in keys
    )


def _authority_from_config(config: Mapping[str, Any]) -> Optional[dict[str, Any]]:
    names = list(_AUTHORITY_BASE_FIELDS)
    if config.get("authority_mode") == "versioned":
        names.extend(_AUTHORITY_VERSIONED_FIELDS)
    authority = {name: config.get(name) for name in names}
    return authority if _valid_transport_authority(authority) else None


def _valid_measurement_row(row: Mapping[str, Any]) -> bool:
    if (set(row) != _MEASUREMENT_KEYS
            or row.get("schema") != MEASUREMENT_SCHEMA
            or row.get("state") != "DATA" or row.get("reason") is not None
            or row.get("credentialed") is not True):
        return False
    team = row.get("team")
    host_identity = row.get("host_identity")
    principal = row.get("principal_identity")
    principal_source = row.get("principal_source")
    probe_id = row.get("probe_id")
    probe_path = row.get("probe_path")
    observed = row.get("observed_seconds")
    provenance = row.get("credential_provenance")
    if (not isinstance(team, str) or _CANONICAL_TEAM.fullmatch(team) is None
            or not isinstance(host_identity, str)
            or _ATTESTED_HOST_IDENTITY.fullmatch(host_identity) is None
            or not isinstance(principal, str) or not principal.strip()
            or principal_source not in _PRINCIPAL_SOURCES
            or not _valid_transport_authority(row.get("transport_authority"))
            or row.get("probe_schema") != PROBE_SCHEMA
            or not isinstance(row.get("producer_build"), str)
            or _BUILD_IDENTITY.fullmatch(row["producer_build"]) is None
            or not isinstance(probe_id, str) or _PROBE_ID.fullmatch(probe_id) is None
            or probe_path != (
                f"team/{team}/_coord/projections/lag-probes/{probe_id}.json"
            )
            or not _valid_update_id(row.get("update_id"))
            or not isinstance(observed, (int, float)) or isinstance(observed, bool)
            or not math.isfinite(float(observed)) or float(observed) < 0
            or not isinstance(provenance, str)
            or _PROVENANCE.fullmatch(provenance) is None
            or provenance != _measurement_provenance(row)):
        return False
    measured_at = _parse_aware_utc(row.get("measured_at"))
    event_at = _parse_aware_utc(row.get("event_at"))
    observed_at = _parse_aware_utc(row.get("observed_at"))
    if (measured_at is None or event_at is None or observed_at is None
            or not (measured_at <= event_at <= observed_at)):
        return False
    derived = (observed_at - event_at).total_seconds()
    return abs(derived - float(observed)) <= LAG_TOLERANCE_SECONDS


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
    cohort: Optional[dict[str, Any]] = None
    if not isinstance(hosts, list):
        reasons.append("host measurement evidence unavailable")
        hosts = []
    for row in hosts:
        if not isinstance(row, Mapping):
            reasons.append("host measurement evidence malformed")
            continue
        host_identity = row.get("host_identity")
        if not _valid_measurement_row(row):
            row_reasons: list[str] = []
            team = row.get("team")
            authority = row.get("transport_authority")
            if (not isinstance(team, str)
                    or _CANONICAL_TEAM.fullmatch(team) is None):
                row_reasons.append("measurement canonical team malformed")
            if not _valid_transport_authority(authority):
                row_reasons.append("measurement transport authority malformed")
            if row.get("schema") != MEASUREMENT_SCHEMA:
                row_reasons.append("measurement schema/protocol malformed")
            if row.get("probe_schema") != PROBE_SCHEMA:
                row_reasons.append("measurement probe protocol malformed")
            if row.get("principal_source") not in _PRINCIPAL_SOURCES:
                row_reasons.append("measurement principal source malformed")
            build = row.get("producer_build")
            if (not isinstance(build, str)
                    or _BUILD_IDENTITY.fullmatch(build) is None):
                row_reasons.append("measurement producer build malformed")
            reasons.extend(row_reasons or (
                "credentialed host measurement evidence malformed",
            ))
            continue
        current_cohort = {
            "team": row["team"],
            "authority": dict(row["transport_authority"]),
            "schema": row["schema"],
            "probe_schema": row["probe_schema"],
            "producer_build": row["producer_build"],
            "principal_source": row["principal_source"],
        }
        if cohort is None:
            cohort = current_cohort
        else:
            if current_cohort["team"] != cohort["team"]:
                reasons.append("measurement cohort mismatch: canonical team")
            if current_cohort["authority"] != cohort["authority"]:
                reasons.append("measurement cohort mismatch: transport authority")
            if current_cohort["schema"] != cohort["schema"]:
                reasons.append("measurement cohort mismatch: measurement schema")
            if current_cohort["probe_schema"] != cohort["probe_schema"]:
                reasons.append("measurement cohort mismatch: probe protocol")
            if current_cohort["producer_build"] != cohort["producer_build"]:
                reasons.append("measurement cohort mismatch: producer build")
            if current_cohort["principal_source"] != cohort["principal_source"]:
                reasons.append("measurement cohort mismatch: principal source")
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
    observed_max = max(float(row["observed_seconds"]) for row in measured.values())
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


def _accepts_deadline(operation: Any) -> bool:
    if not callable(operation):
        return False
    try:
        params = inspect.signature(operation).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(
        param.name == "deadline" or param.kind is inspect.Parameter.VAR_KEYWORD
        for param in params
    )


def measure_feed_visibility_lag(
    transport: Any, team: str, display_label: str, *,
    persisted: Callable[[], classifier.PersistedIdentity],
    hostname: Callable[[], str],
    explicit_identity: Optional[str] = None,
    environ: Optional[Mapping[str, str]] = None,
    timeout_seconds: float = 30.0,
    poll_seconds: float = 0.25,
    deadline: Optional[Any] = None,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> LagMeasurement:
    """Measure one probe under a single preflight-to-verification deadline."""
    start = monotonic()
    measured_at = ""
    probe_id = ""
    path = ""

    host_identity: Optional[str] = None
    principal_identity: Optional[str] = None
    principal_source: Optional[str] = None
    authority: Optional[dict[str, Any]] = None
    producer_build: Optional[str] = None

    def unknown(reason: str) -> LagMeasurement:
        return LagMeasurement.unknown(
            team=team, host_identity=host_identity,
            principal_identity=principal_identity,
            principal_source=principal_source,
            transport_authority=authority, display_label=display_label,
            reason=reason, measured_at=measured_at, probe_id=probe_id,
            probe_path=path, producer_build=producer_build,
        )

    if (not isinstance(timeout_seconds, (int, float))
            or isinstance(timeout_seconds, bool)
            or not math.isfinite(float(timeout_seconds))
            or timeout_seconds <= 0
            or not isinstance(poll_seconds, (int, float))
            or isinstance(poll_seconds, bool)
            or not math.isfinite(float(poll_seconds))
            or poll_seconds <= 0):
        return unknown("positive finite bounds required")

    class _Bound:
        def remaining(self) -> float:
            return max(0.0, float(timeout_seconds) - (monotonic() - start))

        def expired(self) -> bool:
            return self.remaining() <= 0.0

    bound = deadline if deadline is not None else _Bound()

    if bound.expired():
        return unknown("harness deadline expired before initial observation")
    if not isinstance(team, str) or _CANONICAL_TEAM.fullmatch(team) is None:
        return unknown("canonical measurement team unavailable")

    try:
        measured_instant = now()
    except Exception:
        return unknown("initial observation clock unavailable")
    if bound.expired():
        return unknown("initial observation exceeded harness deadline")
    try:
        measured_at = _iso(measured_instant)
        probe_id = uuid.uuid4().hex
        path = f"team/{team}/_coord/projections/lag-probes/{probe_id}.json"
    except Exception:
        return unknown("measurement probe construction failed")
    if bound.expired():
        return unknown("measurement probe construction exceeded harness deadline")

    # A method that cannot receive the shared remaining budget is not licensed
    # to start: falling back to its COORD_TRANSPORT_TIMEOUT would expand this
    # harness's caller-selected bound.
    reader = getattr(transport, "read_classified", None)
    writer = getattr(transport, "write", None)
    feed = getattr(transport, "data_updates", None)
    if not all(_accepts_deadline(op) for op in (reader, writer, feed)):
        return unknown("transport operation lacks shared deadline support")

    identity_env = environ or {}
    try:
        principal_identity = classifier.resolve_identity(
            explicit_identity, environ=identity_env, persisted=persisted,
            hostname=None,
        )
    except Exception:
        return unknown("principal identity unavailable")
    if bound.expired():
        return unknown("principal identity preflight exceeded harness deadline")
    if not isinstance(principal_identity, str) or not principal_identity.strip():
        principal_identity = None
        return unknown("principal identity unavailable")
    principal_identity = principal_identity.strip()
    if explicit_identity:
        principal_source = "explicit"
    elif identity_env.get("FULCRA_COORD_AGENT"):
        principal_source = "env"
    else:
        principal_source = "persisted"
    try:
        raw_host = hostname()
    except Exception:
        return unknown("machine identity unavailable")
    if bound.expired():
        return unknown("machine identity preflight exceeded harness deadline")
    if not isinstance(raw_host, str):
        return unknown("machine identity malformed")
    safe_host, _rewritten = classifier.sanitize_hostname(raw_host)
    if not safe_host:
        return unknown("machine identity unusable")
    host_identity = f"coord-reconcile:{safe_host}"

    try:
        producer_build = pin_currency.build_sha()
    except Exception:
        producer_build = None
    if bound.expired():
        return unknown("producer build preflight exceeded harness deadline")
    if (not isinstance(producer_build, str)
            or _BUILD_IDENTITY.fullmatch(producer_build) is None):
        return unknown("stable producer build identity unavailable")

    config, status = records.load_canonical_config_classified(
        transport, team, deadline=bound,
    )
    if bound.expired():
        return unknown("transport authority preflight exceeded harness deadline")
    if status != "ok" or not isinstance(config, Mapping):
        return unknown("stable canonical transport authority unavailable")
    candidate_authority = _authority_from_config(config)
    if candidate_authority is None:
        return unknown("stable canonical transport authority malformed")
    authority = candidate_authority

    content = jsonutil.dumps({
        "schema": PROBE_SCHEMA,
        "id": probe_id,
        "team": team,
        "host_identity": host_identity,
        "principal_identity": principal_identity,
        "principal_source": principal_source,
        "transport_authority": authority,
        "producer_build": producer_build,
        "display_label": display_label,
        "written_at": measured_at,
    })
    if bound.expired():
        return unknown("harness deadline expired before probe upload")
    try:
        landed = writer(path, content, deadline=bound)
    except Exception:
        landed = False
    if bound.expired():
        return unknown("probe upload exceeded harness deadline")
    if landed is not True:
        return unknown("probe write did not persist")
    feed_period = f"{math.ceil(float(timeout_seconds) + FEED_QUERY_SKEW_SECONDS)} seconds"
    while True:
        if bound.expired():
            return unknown("feed visibility bound expired")
        try:
            result = feed(feed_period, deadline=bound)
        except Exception:
            result = None
        if bound.expired():
            return unknown("feed read exceeded harness deadline")
        if not isinstance(result, Mapping):
            return unknown("data-updates envelope unavailable")
        rows = result.get("file_changes")
        if bound.expired():
            return unknown("feed envelope validation exceeded harness deadline")
        if not isinstance(rows, list):
            return unknown("data-updates file_changes unavailable")
        matched: list[tuple[str, datetime, str]] = []
        identities: set[str] = set()
        for row in rows:
            if bound.expired():
                return unknown("feed lifecycle validation exceeded harness deadline")
            if not isinstance(row, Mapping):
                reason = "data-updates contains malformed lifecycle row"
                break
            raw_path = row.get("path", row.get("full_name"))
            state = row.get("state")
            update_id = change_detection._file_identity(row)
            instant = change_detection._instant(row, state) if isinstance(state, str) else None
            if (not isinstance(raw_path, str) or not raw_path.strip()
                    or state not in ("uploaded", "archived", "deleted")
                    or not _valid_update_id(update_id) or instant is None):
                reason = "data-updates lifecycle row is malformed"
                break
            if update_id in identities:
                reason = "data-updates contains duplicate update identity"
                break
            identities.add(update_id)
            normalized_path = raw_path.strip().lstrip("/")
            if normalized_path == path:
                if state != "uploaded":
                    reason = "probe lifecycle is not uploaded"
                    break
                matched.append((update_id, instant[0], instant[1]))
        else:
            reason = ""
        if bound.expired():
            return unknown("feed lifecycle validation exceeded harness deadline")
        if reason:
            return unknown(reason)
        if matched:
            if len(matched) != 1:
                return unknown("probe update identity is not unique")
            update_id, event_instant, event_at = matched[0]
            try:
                observed_value = now()
            except Exception:
                return unknown("final observation clock unavailable")
            if bound.expired():
                return unknown("final observation exceeded harness deadline")
            try:
                observed_instant = observed_value.astimezone(timezone.utc)
                lag = (observed_instant - event_instant).total_seconds()
                observed_at = _iso(observed_instant)
            except Exception:
                return unknown("final observation is malformed")
            if bound.expired():
                return unknown("lag computation exceeded harness deadline")
            if not math.isfinite(lag) or lag < 0:
                return unknown("authoritative event timestamp is after observation")
            measurement = LagMeasurement(
                state="DATA", team=team, host_identity=host_identity,
                display_label=display_label, principal_identity=principal_identity,
                principal_source=principal_source,
                transport_authority=authority, probe_schema=PROBE_SCHEMA,
                producer_build=producer_build, credential_provenance=None,
                credentialed=True, observed_seconds=lag, measured_at=measured_at,
                probe_id=probe_id, probe_path=path, event_at=event_at,
                observed_at=observed_at, update_id=update_id,
            )
            if bound.expired():
                return unknown("measurement construction exceeded harness deadline")
            provenance = _measurement_provenance(
                measurement.as_dict(), expired=bound.expired,
            )
            if bound.expired():
                return unknown("measurement serialization exceeded harness deadline")
            if provenance is None:
                return unknown("measurement serialization failed")
            final = replace(measurement, credential_provenance=provenance)
            if bound.expired():
                return unknown("measurement finalization exceeded harness deadline")
            final_row = final.as_dict()
            if bound.expired():
                return unknown("measurement result serialization exceeded harness deadline")
            if not _valid_measurement_row(final_row):
                return unknown("measurement result failed activation validation")
            # Definitive final gate: DATA is licensed only while the original
            # preflight-to-result deadline still has positive budget.
            if bound.expired():
                return unknown("measurement deadline expired before DATA return")
            return final
        remaining = bound.remaining()
        if remaining <= 0:
            return unknown("feed visibility bound expired")
        sleep(min(float(poll_seconds), remaining))
