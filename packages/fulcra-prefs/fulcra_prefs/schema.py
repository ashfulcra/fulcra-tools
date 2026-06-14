"""Signal model + canonical JSON. Determinism lives here: every byte the
package emits flows through canonical_json, and every signal has exactly one
stable id (persisted record id, else deterministic temp id)."""
from __future__ import annotations
import hashlib
import json
from dataclasses import dataclass, field

SCHEMA_V = 1
KINDS = ("preference", "fact", "consent")
FLOAT_DP = 6
TEMP_ID_PREFIX = "com.fulcra-prefs.sig."
CAPTURE_SOURCE_PREFIX = "com.fulcra-prefs.capture."
ANNOTATION_SOURCE_PREFIX = "com.fulcradynamics.annotation."


def _normalize(obj):
    if isinstance(obj, float):
        r = round(obj, FLOAT_DP)
        return 0.0 if r == 0 else r  # collapse -0.0 -> 0.0 for byte-stable output
    if isinstance(obj, dict):
        return {k: _normalize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_normalize(v) for v in obj]
    return obj


def canonical_json(obj) -> str:
    return json.dumps(_normalize(obj), sort_keys=True,
                      separators=(",", ":"), ensure_ascii=False)


def temp_signal_id(key: str, observed_at: str, platform: str) -> str:
    digest = hashlib.sha256(
        f"{key}|{observed_at}|{platform}".encode()).hexdigest()[:24]
    return f"{TEMP_ID_PREFIX}{digest}"


def _valid_scope(scope: str) -> bool:
    return scope == "global" or scope.startswith("platform:")


@dataclass(frozen=True)
class Signal:
    id: str | None
    kind: str
    key: str
    scope: str
    value: object
    strength: float
    confidence: float
    half_life_days: float | None
    observed_at: str
    platform: str
    agent: str | None
    session: str | None
    supersedes: str | None
    source_ids: tuple[str, ...] = field(default_factory=tuple, compare=False)

    def __post_init__(self):
        if self.kind not in KINDS:
            raise ValueError(f"kind must be one of {KINDS}, got {self.kind!r}")
        if not _valid_scope(self.scope):
            raise ValueError(f"scope must be 'global' or 'platform:<p>', got {self.scope!r}")
        if not self.key:
            raise ValueError("key is required")
        if self.half_life_days is not None and self.half_life_days <= 0:
            # 0 → divide-by-zero in effective_weight; <0 → decay that grows.
            # Reject at construction so a poisoned signal never reaches the
            # cache and breaks every later compile. None = no decay (valid).
            raise ValueError(
                f"half_life_days must be > 0 or None, got {self.half_life_days!r}")

    def to_payload(self) -> dict:
        return {
            "v": SCHEMA_V, "kind": self.kind, "key": self.key,
            "scope": self.scope, "value": self.value,
            "strength": self.strength, "confidence": self.confidence,
            "half_life_days": self.half_life_days,
            "source": {"platform": self.platform, "agent": self.agent,
                       "session": self.session},
            "supersedes": self.supersedes,
        }


def parse_record(env: dict) -> Signal:
    """env: one record as returned by get-records / ingest echo.
    Persisted record id wins; else the deterministic temp id from sources."""
    payload = json.loads(env["data"]) if isinstance(env.get("data"), str) else env["data"]
    sources = env.get("sources") or []
    temp = next((s for s in sources if s.startswith(TEMP_ID_PREFIX)), None)
    sid = env.get("id") or temp
    src = payload.get("source") or {}
    return Signal(
        id=sid, kind=payload["kind"], key=payload["key"],
        scope=payload["scope"], value=payload.get("value"),
        strength=float(payload["strength"]),
        confidence=float(payload.get("confidence", 1.0)),
        half_life_days=(None if payload.get("half_life_days") is None
                        else float(payload["half_life_days"])),
        observed_at=env.get("recorded_at") or payload.get("observed_at"),
        platform=src.get("platform", "unknown"),
        agent=src.get("agent"), session=src.get("session"),
        supersedes=payload.get("supersedes"),
        source_ids=tuple(sources),
    )
