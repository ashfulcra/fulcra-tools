# fulcra-prefs v1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the v1 of `fulcra-prefs` — typed preference signals with decay ingested as Fulcra annotation records, deterministically compiled into versioned preference files, with a deterministic group-decision solver and consent-gated export — per the reviewed spec at `packages/fulcra-prefs/docs/SPEC.md` (PR #146, incl. reviewer fix `db46fb5`).

**Architecture:** Event-sourced two layers: signals (annotation records via `POST /ingest/v1/record`) → compiled projections (canonical-JSON files under `prefs/` in the Fulcra file library). All computation is pure-function Python in `fulcra_prefs`; I/O isolated in a thin store wrapping the `fulcra_api` library. CLI (`fulcra-prefs`) is the tier-1 surface; a skill + HTTP recipes serve tiers 2/3.

**Tech Stack:** Python ≥3.11, hatchling, pytest (<8), `fulcra-api>=0.1.33` (the only runtime dep), stdlib `argparse` for the CLI (no click dep). Workspace member of ashfulcra/fulcra-tools; all work on branch `claude-code/fulcra-prefs`, worktree `/Users/Scanning/Developer/fulcra-tools-prefs`.

**Conventions for every task:** run commands from `packages/fulcra-prefs/` inside the worktree unless stated. Test runner: `uv run --package fulcra-prefs pytest <path> -v` (run from repo root). Commit after every green test cycle. Determinism rules everywhere: canonical JSON = `sort_keys=True, separators=(",", ":"), ensure_ascii=False`, floats rounded to 6 dp, **stable signal-id sort before any conflict resolution**, `now` always an explicit argument — never `datetime.now()` inside library code.

---

## File structure (locked by this plan)

```
packages/fulcra-prefs/
  fulcra_prefs/
    __init__.py        # exists (version)
    schema.py          # Signal model, payload (de)serialization, canonical JSON, signal ids
    decay.py           # effective_weight(signal, now), staleness flag
    compileprefs.py    # compile_signals(signals, now) -> {global, platforms} docs
    solver.py          # solve(options, participant_docs, policy) -> ranking + trace
    consent.py         # Grant model, filter_for_audience, disclosure signal builder
    store.py           # FulcraStore: file read/write + record ingest over fulcra_api
    outbox.py          # local spool for failed ingests (~/.local/state/fulcra-prefs/outbox)
    capture.py         # build + ingest a signal (outbox fallback)
    inject.py          # render compiled doc as a session-context block per platform
    cli.py             # argparse CLI: onboard|capture|compile|get|solve|consent|inject
  tests/
    conftest.py        # FakeFulcraAPI + fixture signals
    test_schema.py
    test_decay.py
    test_compile.py
    test_determinism.py
    test_solver.py
    test_consent.py
    test_store.py
    test_capture_outbox.py
    test_cli.py
  skill/
    SKILL.md           # agent-skills-convention skill (tier routing)
    references/
      fulcra-prefs-tier2-http.md   # raw-HTTP recipes (device flow, ingest, file read)
      fulcra-prefs-capture.md      # capture heuristics for agents
  docs/SPEC.md         # exists (reviewed)
  docs/DESIGN.md       # exists (historical)
  docs/PLAN.md         # this file
  README.md            # Task 12
  pyproject.toml       # exists; Task 7 adds the fulcra-api dependency
```

Module name note: `compileprefs.py` (not `compile.py`) avoids shadowing the
`compile` builtin in imports and tooling.

---

### Task 1: Signal schema + canonical JSON (`schema.py`)

**Files:**
- Create: `packages/fulcra-prefs/fulcra_prefs/schema.py`
- Test: `packages/fulcra-prefs/tests/test_schema.py`
- Modify: `packages/fulcra-prefs/pyproject.toml` (pytest import mode)

- [ ] **Step 0: Switch pytest to default import mode**

Tests in this plan share helpers via bare cross-file imports
(`from test_schema import make_signal`), which requires pytest's default
*prepend* import mode (the tests directory goes on `sys.path`). The scaffold
pinned importlib mode; change the `addopts` line in
`packages/fulcra-prefs/pyproject.toml` from:

```toml
addopts = "-ra --strict-markers --import-mode=importlib"
```
to:
```toml
addopts = "-ra --strict-markers"
```

Include this pyproject change in Task 1's commit.

- [ ] **Step 1: Write the failing tests**

```python
# packages/fulcra-prefs/tests/test_schema.py
import json
import pytest
from fulcra_prefs.schema import (
    Signal, canonical_json, parse_record, temp_signal_id, SCHEMA_V,
)

def make_signal(**over):
    base = dict(
        id="rec-001", kind="preference", key="dining.cuisine.thai",
        scope="global", value={"liked": True}, strength=0.8, confidence=0.9,
        half_life_days=90.0, observed_at="2026-06-01T12:00:00+00:00",
        platform="claude-code", agent="a", session="s", supersedes=None,
    )
    base.update(over)
    return Signal(**base)

def test_canonical_json_is_sorted_compact_and_float_normalized():
    s = canonical_json({"b": 1.23456789, "a": {"y": 2, "x": 1}})
    assert s == '{"a":{"x":1,"y":2},"b":1.234568}'

def test_canonical_json_is_stable_under_key_insertion_order():
    assert canonical_json({"a": 1, "b": 2}) == canonical_json({"b": 2, "a": 1})

def test_signal_roundtrip_through_record_payload():
    sig = make_signal()
    payload = sig.to_payload()           # dict for the record `data` field
    env = {                              # what get-records returns
        "id": "rec-001",
        "recorded_at": "2026-06-01T12:00:00+00:00",
        "sources": ["com.fulcra-prefs.sig.0000-aaaa",
                    "com.fulcra-prefs.capture.claude-code"],
        "data": json.dumps(payload),
    }
    back = parse_record(env)
    assert back == sig
    assert back.id == "rec-001"          # persisted id wins over temp id

def test_parse_record_uses_temp_id_when_unpersisted():
    sig = make_signal()
    env = {"id": None, "recorded_at": "2026-06-01T12:00:00+00:00",
           "sources": ["com.fulcra-prefs.sig.0000-aaaa",
                       "com.fulcra-prefs.capture.claude-code"],
           "data": json.dumps(sig.to_payload())}
    assert parse_record(env).id == "com.fulcra-prefs.sig.0000-aaaa"

def test_parse_record_preserves_source_ids_for_supersedes_aliases():
    sig = make_signal()
    env = {"id": "rec-001", "recorded_at": "2026-06-01T12:00:00+00:00",
           "sources": ["com.fulcra-prefs.sig.0000-aaaa",
                       "com.fulcra-prefs.capture.claude-code"],
           "data": json.dumps(sig.to_payload())}
    back = parse_record(env)
    assert back.id == "rec-001"
    assert "com.fulcra-prefs.sig.0000-aaaa" in back.source_ids

def test_temp_signal_id_is_deterministic_for_same_inputs():
    a = temp_signal_id("dining.cuisine.thai", "2026-06-01T12:00:00+00:00", "claude-code")
    b = temp_signal_id("dining.cuisine.thai", "2026-06-01T12:00:00+00:00", "claude-code")
    assert a == b and a.startswith("com.fulcra-prefs.sig.")

def test_payload_carries_schema_version():
    assert make_signal().to_payload()["v"] == SCHEMA_V

def test_invalid_kind_rejected():
    with pytest.raises(ValueError):
        make_signal(kind="whim")

def test_invalid_scope_rejected():
    with pytest.raises(ValueError):
        make_signal(scope="galaxy")
```

- [ ] **Step 2: Run tests to verify they fail**

Run (repo root): `uv run --package fulcra-prefs pytest packages/fulcra-prefs/tests/test_schema.py -v`
Expected: FAIL — `ModuleNotFoundError: fulcra_prefs.schema`

- [ ] **Step 3: Implement `schema.py`**

```python
# packages/fulcra-prefs/fulcra_prefs/schema.py
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


def _normalize(obj):
    if isinstance(obj, float):
        return round(obj, FLOAT_DP)
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --package fulcra-prefs pytest packages/fulcra-prefs/tests/test_schema.py -v`
Expected: 9 PASS

- [ ] **Step 5: Commit**

```bash
git add packages/fulcra-prefs/fulcra_prefs/schema.py packages/fulcra-prefs/tests/test_schema.py
git commit -m "feat(prefs): signal schema, canonical JSON, deterministic signal ids"
```

---

### Task 2: Decay (`decay.py`)

**Files:**
- Create: `packages/fulcra-prefs/fulcra_prefs/decay.py`
- Test: `packages/fulcra-prefs/tests/test_decay.py`

- [ ] **Step 1: Write the failing tests**

```python
# packages/fulcra-prefs/tests/test_decay.py
from datetime import datetime, timezone
from fulcra_prefs.decay import effective_weight, is_stale, STALE_FACT_DAYS
from test_schema import make_signal

NOW = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)

def test_zero_age_weight_equals_strength():
    s = make_signal(observed_at="2026-06-10T12:00:00+00:00")
    assert effective_weight(s, NOW) == 0.8

def test_one_half_life_halves_weight():
    s = make_signal(observed_at="2026-03-12T12:00:00+00:00", half_life_days=90.0)
    assert abs(effective_weight(s, NOW) - 0.4) < 1e-9

def test_negative_strength_decays_toward_zero_not_positive():
    s = make_signal(strength=-0.8, observed_at="2026-03-12T12:00:00+00:00",
                    half_life_days=90.0)
    assert abs(effective_weight(s, NOW) + 0.4) < 1e-9

def test_no_half_life_means_no_decay():
    s = make_signal(half_life_days=None, observed_at="2020-01-01T00:00:00+00:00")
    assert effective_weight(s, NOW) == 0.8

def test_staleness_flag_only_for_undecaying_old_signals():
    old = "2020-01-01T00:00:00+00:00"
    assert is_stale(make_signal(half_life_days=None, observed_at=old), NOW)
    assert not is_stale(make_signal(half_life_days=90.0, observed_at=old), NOW)
    fresh = "2026-06-01T00:00:00+00:00"
    assert not is_stale(make_signal(half_life_days=None, observed_at=fresh), NOW)
    assert STALE_FACT_DAYS == 180
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run --package fulcra-prefs pytest packages/fulcra-prefs/tests/test_decay.py -v`
Expected: FAIL — `ModuleNotFoundError: fulcra_prefs.decay`

- [ ] **Step 3: Implement `decay.py`**

```python
# packages/fulcra-prefs/fulcra_prefs/decay.py
"""Half-life decay. Pure functions of (signal, now); `now` is always explicit
so compile output is reproducible (the determinism contract in SPEC.md)."""
from __future__ import annotations
from datetime import datetime
from .schema import Signal

STALE_FACT_DAYS = 180  # undecaying facts older than this get flagged, not dropped


def _age_days(observed_at: str, now: datetime) -> float:
    observed = datetime.fromisoformat(observed_at)
    return (now - observed).total_seconds() / 86400.0


def effective_weight(sig: Signal, now: datetime) -> float:
    if sig.half_life_days is None:
        return sig.strength
    return sig.strength * 2 ** (-_age_days(sig.observed_at, now) / sig.half_life_days)


def is_stale(sig: Signal, now: datetime) -> bool:
    return sig.half_life_days is None and _age_days(sig.observed_at, now) > STALE_FACT_DAYS
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run --package fulcra-prefs pytest packages/fulcra-prefs/tests/test_decay.py -v`
Expected: 5 PASS

- [ ] **Step 5: Commit**

```bash
git add packages/fulcra-prefs/fulcra_prefs/decay.py packages/fulcra-prefs/tests/test_decay.py
git commit -m "feat(prefs): half-life decay with explicit now and fact staleness flag"
```

---

### Task 3: Compile (`compileprefs.py`)

**Files:**
- Create: `packages/fulcra-prefs/fulcra_prefs/compileprefs.py`
- Test: `packages/fulcra-prefs/tests/test_compile.py`

- [ ] **Step 1: Write the failing tests**

```python
# packages/fulcra-prefs/tests/test_compile.py
from datetime import datetime, timezone
from fulcra_prefs.compileprefs import compile_signals
from test_schema import make_signal

NOW = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)

def test_single_signal_lands_in_global_doc():
    docs = compile_signals([make_signal()], NOW)
    entry = docs["global"]["keys"]["dining.cuisine.thai"]
    assert entry["value"] == {"liked": True}
    assert entry["n_signals"] == 1
    assert docs["global"]["compiled_at"] == "2026-06-10T12:00:00+00:00"

def test_conflict_resolves_to_highest_abs_effective_weight():
    a = make_signal(id="rec-a", strength=0.3, value={"liked": True},
                    observed_at="2026-06-09T12:00:00+00:00")
    b = make_signal(id="rec-b", strength=-0.9, value={"liked": False},
                    observed_at="2026-06-08T12:00:00+00:00")
    docs = compile_signals([a, b], NOW)
    assert docs["global"]["keys"]["dining.cuisine.thai"]["value"] == {"liked": False}
    assert docs["global"]["keys"]["dining.cuisine.thai"]["n_signals"] == 2

def test_tie_resolves_to_newer_observed_at():
    a = make_signal(id="rec-a", half_life_days=None, strength=0.5,
                    value={"v": "old"}, observed_at="2026-06-01T00:00:00+00:00")
    b = make_signal(id="rec-b", half_life_days=None, strength=0.5,
                    value={"v": "new"}, observed_at="2026-06-09T00:00:00+00:00")
    docs = compile_signals([a, b], NOW)
    assert docs["global"]["keys"]["dining.cuisine.thai"]["value"] == {"v": "new"}

def test_superseded_signals_dropped_including_chains():
    a = make_signal(id="rec-a", value={"gen": 1})
    b = make_signal(id="rec-b", value={"gen": 2}, supersedes="rec-a")
    c = make_signal(id="rec-c", value={"gen": 3}, supersedes="rec-b")
    docs = compile_signals([a, b, c], NOW)
    entry = docs["global"]["keys"]["dining.cuisine.thai"]
    assert entry["value"] == {"gen": 3}
    assert entry["n_signals"] == 1   # superseded signals are gone, not merged

def test_supersedes_temp_id_still_drops_persisted_record():
    # Spec contract: `supersedes` may reference either the local temp id or the
    # persisted Fulcra record id. Once a record is persisted, its temp id still
    # appears in metadata.source and must remain a valid alias.
    old = make_signal(id="rec-a", value={"gen": 1},
                      source_ids=("com.fulcra-prefs.sig.temp-a",))
    new = make_signal(id="rec-b", value={"gen": 2},
                      supersedes="com.fulcra-prefs.sig.temp-a")
    docs = compile_signals([old, new], NOW)
    assert docs["global"]["keys"]["dining.cuisine.thai"]["value"] == {"gen": 2}

def test_supersedes_dangling_ref_does_not_drop_replacement():
    sig = make_signal(id="rec-b", value={"gen": 2}, supersedes="missing-id")
    docs = compile_signals([sig], NOW)
    assert docs["global"]["keys"]["dining.cuisine.thai"]["value"] == {"gen": 2}

def test_supersedes_cycle_drops_all_cycle_members():
    a = make_signal(id="rec-a", value={"gen": 1}, supersedes="rec-b")
    b = make_signal(id="rec-b", value={"gen": 2}, supersedes="rec-a")
    assert compile_signals([a, b], NOW)["global"]["keys"] == {}

def test_platform_scope_overlays_global():
    g = make_signal(id="rec-g", value={"v": "global"})
    p = make_signal(id="rec-p", scope="platform:claude-code", value={"v": "cc"})
    docs = compile_signals([g, p], NOW)
    assert docs["global"]["keys"]["dining.cuisine.thai"]["value"] == {"v": "global"}
    assert docs["platforms"]["claude-code"]["keys"]["dining.cuisine.thai"]["value"] == {"v": "cc"}

def test_consent_kind_signals_excluded_from_pref_docs():
    docs = compile_signals([make_signal(kind="consent", key="consent.disclosure.x")], NOW)
    assert docs["global"]["keys"] == {}

def test_stale_fact_carries_flag():
    f = make_signal(kind="fact", half_life_days=None,
                    observed_at="2020-01-01T00:00:00+00:00")
    docs = compile_signals([f], NOW)
    assert docs["global"]["keys"]["dining.cuisine.thai"]["stale"] is True
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run --package fulcra-prefs pytest packages/fulcra-prefs/tests/test_compile.py -v`
Expected: FAIL — `ModuleNotFoundError: fulcra_prefs.compileprefs`

- [ ] **Step 3: Implement `compileprefs.py`**

```python
# packages/fulcra-prefs/fulcra_prefs/compileprefs.py
"""Full-recompute compile: signals -> compiled docs. Pure function of
(signals, now). Stable signal-id sort happens BEFORE conflict resolution —
that ordering is part of the reviewed determinism contract (PR #146)."""
from __future__ import annotations
from datetime import datetime
from .decay import effective_weight, is_stale
from .schema import Signal, SCHEMA_V


def _signal_ids(sig: Signal) -> set[str]:
    return {x for x in (sig.id, *sig.source_ids) if x}


def _live_signals(signals: list[Signal]) -> list[Signal]:
    superseded = {s.supersedes for s in signals if s.supersedes}
    return [s for s in signals if not (_signal_ids(s) & superseded)]


def _entry(sig: Signal, weight: float, n: int, now: datetime) -> dict:
    e = {"value": sig.value, "weight": weight, "confidence": sig.confidence,
         "observed_at": sig.observed_at, "n_signals": n,
         "sources": [sig.platform]}
    if is_stale(sig, now):
        e["stale"] = True
    return e


def _reduce(signals: list[Signal], now: datetime) -> dict:
    by_key: dict[str, list[Signal]] = {}
    for s in signals:
        by_key.setdefault(s.key, []).append(s)
    keys: dict[str, dict] = {}
    for key, group in by_key.items():
        # Stable id sort first — conflict resolution must not depend on input order.
        group = sorted(group, key=lambda s: s.id or "")
        best = max(group, key=lambda s: (abs(effective_weight(s, now)), s.observed_at))
        keys[key] = _entry(best, effective_weight(best, now), len(group), now)
    return keys


def compile_signals(signals: list[Signal], now: datetime) -> dict:
    live = [s for s in _live_signals(signals) if s.kind in ("preference", "fact")]
    compiled_at = now.isoformat()
    global_keys = _reduce([s for s in live if s.scope == "global"], now)
    docs = {"global": {"v": SCHEMA_V, "compiled_at": compiled_at, "keys": global_keys},
            "platforms": {}}
    platforms = sorted({s.scope.split(":", 1)[1] for s in live
                        if s.scope.startswith("platform:")})
    for p in platforms:
        overlay = _reduce([s for s in live if s.scope == f"platform:{p}"], now)
        merged = dict(global_keys)
        merged.update(overlay)          # platform beats global
        docs["platforms"][p] = {"v": SCHEMA_V, "compiled_at": compiled_at,
                                "keys": merged}
    return docs
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run --package fulcra-prefs pytest packages/fulcra-prefs/tests/test_compile.py -v`
Expected: 10 PASS

- [ ] **Step 5: Commit**

```bash
git add packages/fulcra-prefs/fulcra_prefs/compileprefs.py packages/fulcra-prefs/tests/test_compile.py
git commit -m "feat(prefs): deterministic compile with supersedes, decay, platform overlay"
```

---

### Task 4: Determinism contract test (`test_determinism.py`)

**Files:**
- Test: `packages/fulcra-prefs/tests/test_determinism.py`

- [ ] **Step 1: Write the tests (against existing code — these must pass immediately; if any fails, the bug is in Tasks 1–3 and gets fixed there)**

```python
# packages/fulcra-prefs/tests/test_determinism.py
"""The byte-identical contract from SPEC.md. If these tests ever flake,
determinism is broken — treat as P0, not as test noise."""
import random
from datetime import datetime, timezone
from fulcra_prefs.compileprefs import compile_signals
from fulcra_prefs.schema import canonical_json
from test_schema import make_signal

NOW = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)

def _fixture_signals():
    sigs = []
    for i in range(40):
        sigs.append(make_signal(
            id=f"rec-{i:03d}",
            key=f"k.{i % 7}",
            scope="global" if i % 3 else "platform:claude-code",
            strength=((i % 11) - 5) / 5.0,
            observed_at=f"2026-05-{(i % 28) + 1:02d}T08:00:00+00:00",
            half_life_days=None if i % 5 == 0 else 60.0,
            supersedes=f"rec-{i - 1:03d}" if i % 13 == 0 and i else None,
            value={"i": i},
        ))
    return sigs

def test_same_inputs_byte_identical_output():
    a = canonical_json(compile_signals(_fixture_signals(), NOW))
    b = canonical_json(compile_signals(_fixture_signals(), NOW))
    assert a == b

def test_input_order_does_not_change_output():
    base = canonical_json(compile_signals(_fixture_signals(), NOW))
    for seed in (1, 7, 42):
        shuffled = _fixture_signals()
        random.Random(seed).shuffle(shuffled)
        assert canonical_json(compile_signals(shuffled, NOW)) == base

def test_output_contains_no_unnormalized_floats():
    out = canonical_json(compile_signals(_fixture_signals(), NOW))
    for token in out.replace("{", ",").replace("}", ",").split(","):
        if "." in token and token.split(":")[-1].replace("-", "").replace(".", "").isdigit():
            frac = token.split(".")[-1].rstrip("}")
            assert len(frac) <= 6
```

- [ ] **Step 2: Run**

Run: `uv run --package fulcra-prefs pytest packages/fulcra-prefs/tests/test_determinism.py -v`
Expected: 3 PASS (if FAIL → fix the responsible module from Tasks 1–3, then re-run)

- [ ] **Step 3: Commit**

```bash
git add packages/fulcra-prefs/tests/test_determinism.py
git commit -m "test(prefs): byte-identical determinism contract incl. shuffled-input invariance"
```

---

### Task 5: Solver (`solver.py`)

**Files:**
- Create: `packages/fulcra-prefs/fulcra_prefs/solver.py`
- Test: `packages/fulcra-prefs/tests/test_solver.py`

- [ ] **Step 1: Write the failing tests**

```python
# packages/fulcra-prefs/tests/test_solver.py
import pytest
from fulcra_prefs.solver import solve, VETO_THRESHOLD_DEFAULT

def doc(**keys):
    return {"v": 1, "compiled_at": "2026-06-10T12:00:00+00:00",
            "keys": {k: {"value": True, "weight": w, "confidence": 1.0,
                         "observed_at": "2026-06-01T00:00:00+00:00",
                         "n_signals": 1, "sources": ["test"]}
                     for k, w in keys.items()}}

OPTIONS = [
    {"id": "thai-spot",  "keys": ["dining.cuisine.thai", "dining.noise.quiet"]},
    {"id": "bbq-barn",   "keys": ["dining.cuisine.bbq"]},
    {"id": "pizza-place","keys": ["dining.cuisine.pizza"]},
]

def test_weighted_sum_ranks_by_total_weight():
    alice = {"alice": doc(**{"dining.cuisine.thai": 0.9, "dining.cuisine.bbq": 0.2})}
    res = solve(OPTIONS, alice, policy="weighted-sum")
    assert [o["id"] for o in res["ranked"]] == ["thai-spot", "bbq-barn", "pizza-place"]
    assert res["ranked"][0]["score"] == 0.9

def test_multi_participant_scores_sum():
    docs = {"alice": doc(**{"dining.cuisine.thai": 0.9}),
            "bob":   doc(**{"dining.cuisine.thai": -0.3, "dining.cuisine.bbq": 0.8})}
    res = solve(OPTIONS, docs, policy="weighted-sum")
    thai = next(o for o in res["ranked"] if o["id"] == "thai-spot")
    assert abs(thai["score"] - 0.6) < 1e-9

def test_hard_veto_removes_option_and_traces_it():
    docs = {"alice": doc(**{"dining.cuisine.bbq": 0.9}),
            "bob":   doc(**{"dining.cuisine.bbq": -0.8})}
    res = solve(OPTIONS, docs, policy="hard-veto")
    assert "bbq-barn" not in [o["id"] for o in res["ranked"]]
    assert any("veto" in line and "bob" in line for line in res["trace"])

def test_tie_breaks_lexicographically_by_option_id():
    res = solve(OPTIONS, {"alice": doc()}, policy="weighted-sum")
    assert [o["id"] for o in res["ranked"]] == ["bbq-barn", "pizza-place", "thai-spot"]

def test_trace_explains_every_option():
    res = solve(OPTIONS, {"alice": doc(**{"dining.cuisine.thai": 0.9})},
                policy="weighted-sum")
    for opt in OPTIONS:
        assert any(opt["id"] in line for line in res["trace"])

def test_unknown_policy_rejected():
    with pytest.raises(ValueError):
        solve(OPTIONS, {"alice": doc()}, policy="vibes")

def test_default_veto_threshold():
    assert VETO_THRESHOLD_DEFAULT == -0.5
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run --package fulcra-prefs pytest packages/fulcra-prefs/tests/test_solver.py -v`
Expected: FAIL — `ModuleNotFoundError: fulcra_prefs.solver`

- [ ] **Step 3: Implement `solver.py`**

```python
# packages/fulcra-prefs/fulcra_prefs/solver.py
"""Deterministic group-decision solver. Pure function; canonical ordering
(participants and options sorted) and a lexicographic tie-breaker make the
ranking reproducible. The trace is the product, not a debug aid — it is the
human-readable 'why' the spec promises."""
from __future__ import annotations

POLICIES = ("weighted-sum", "hard-veto")
VETO_THRESHOLD_DEFAULT = -0.5


def solve(options: list[dict], participant_docs: dict[str, dict],
          policy: str = "weighted-sum",
          veto_threshold: float = VETO_THRESHOLD_DEFAULT) -> dict:
    if policy not in POLICIES:
        raise ValueError(f"policy must be one of {POLICIES}, got {policy!r}")
    trace: list[str] = []
    participants = sorted(participant_docs)          # canonical ordering
    opts = sorted(options, key=lambda o: o["id"])
    scored, vetoed = [], []
    for opt in opts:
        total = 0.0
        veto = None
        for who in participants:
            keys = participant_docs[who].get("keys", {})
            for k in opt["keys"]:
                if k not in keys:
                    continue
                w = keys[k]["weight"]
                total += w
                trace.append(f"{opt['id']}: {who} {k} weight {w:+.6f}")
                if policy == "hard-veto" and w < veto_threshold and veto is None:
                    veto = (who, k, w)
        if veto:
            who, k, w = veto
            vetoed.append(opt["id"])
            trace.append(f"{opt['id']}: VETOED by {who} on {k} ({w:+.6f} < {veto_threshold})")
        else:
            scored.append({"id": opt["id"], "score": round(total, 6)})
            trace.append(f"{opt['id']}: total {total:+.6f}")
    ranked = sorted(scored, key=lambda o: (-o["score"], o["id"]))
    trace.append("ranking: " + " > ".join(o["id"] for o in ranked)
                 + (f" | vetoed: {', '.join(vetoed)}" if vetoed else ""))
    return {"ranked": ranked, "vetoed": vetoed, "trace": trace}
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run --package fulcra-prefs pytest packages/fulcra-prefs/tests/test_solver.py -v`
Expected: 7 PASS

- [ ] **Step 5: Commit**

```bash
git add packages/fulcra-prefs/fulcra_prefs/solver.py packages/fulcra-prefs/tests/test_solver.py
git commit -m "feat(prefs): deterministic weighted-sum + hard-veto solver with explainable trace"
```

---

### Task 6: Consent (`consent.py`)

**Files:**
- Create: `packages/fulcra-prefs/fulcra_prefs/consent.py`
- Test: `packages/fulcra-prefs/tests/test_consent.py`

- [ ] **Step 1: Write the failing tests**

```python
# packages/fulcra-prefs/tests/test_consent.py
from datetime import datetime, timezone
from fulcra_prefs.consent import filter_for_audience, disclosure_signal

NOW = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)

DOC = {"v": 1, "compiled_at": "2026-06-10T12:00:00+00:00",
       "keys": {"dining.cuisine.thai": {"value": True, "weight": 0.8},
                "health.sleep.target": {"value": 8, "weight": 1.0},
                "dining.noise.quiet":  {"value": True, "weight": 0.5}}}

def grant(glob="dining.*", audience="ea-agent", level="read", expires=None):
    return {"key_glob": glob, "audience": audience, "level": level,
            "granted_at": "2026-06-01T00:00:00+00:00", "expires": expires}

def test_filter_keeps_only_granted_keys():
    out = filter_for_audience(DOC, [grant()], "ea-agent", NOW)
    assert sorted(out["keys"]) == ["dining.cuisine.thai", "dining.noise.quiet"]

def test_no_grants_means_empty_doc():
    out = filter_for_audience(DOC, [], "ea-agent", NOW)
    assert out["keys"] == {}

def test_wrong_audience_gets_nothing():
    out = filter_for_audience(DOC, [grant(audience="other")], "ea-agent", NOW)
    assert out["keys"] == {}

def test_expired_grant_ignored():
    g = grant(expires="2026-06-09T00:00:00+00:00")
    assert filter_for_audience(DOC, [g], "ea-agent", NOW)["keys"] == {}

def test_disclosure_signal_records_what_was_shared():
    sig = disclosure_signal(["dining.cuisine.thai"], "ea-agent",
                            platform="claude-code", now=NOW)
    assert sig.kind == "consent"
    assert sig.key == "consent.disclosure.ea-agent"
    assert sig.value == {"keys": ["dining.cuisine.thai"], "audience": "ea-agent"}
    assert sig.observed_at == "2026-06-10T12:00:00+00:00"
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run --package fulcra-prefs pytest packages/fulcra-prefs/tests/test_consent.py -v`
Expected: FAIL — `ModuleNotFoundError: fulcra_prefs.consent`

- [ ] **Step 3: Implement `consent.py`**

```python
# packages/fulcra-prefs/fulcra_prefs/consent.py
"""Consent enforcement at the export boundary. Filtering happens at `get
--for <audience>` time (not at storage time) so revoking a grant immediately
affects the next export. Every export is itself a consent-kind signal — the
disclosure log IS the Privacy Ledger."""
from __future__ import annotations
from datetime import datetime
from fnmatch import fnmatch
from .schema import Signal, temp_signal_id


def _active(grant: dict, audience: str, now: datetime) -> bool:
    if grant["audience"] != audience:
        return False
    exp = grant.get("expires")
    return exp is None or datetime.fromisoformat(exp) > now


def filter_for_audience(doc: dict, grants: list[dict], audience: str,
                        now: datetime) -> dict:
    live = [g for g in grants if _active(g, audience, now)]
    keys = {k: v for k, v in doc.get("keys", {}).items()
            if any(fnmatch(k, g["key_glob"]) for g in live)}
    return {**doc, "keys": keys}


def disclosure_signal(shared_keys: list[str], audience: str, platform: str,
                      now: datetime) -> Signal:
    observed = now.isoformat()
    key = f"consent.disclosure.{audience}"
    return Signal(
        id=temp_signal_id(key, observed, platform),
        kind="consent", key=key, scope="global",
        value={"keys": sorted(shared_keys), "audience": audience},
        strength=1.0, confidence=1.0, half_life_days=None,
        observed_at=observed, platform=platform, agent=None, session=None,
        supersedes=None,
    )
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run --package fulcra-prefs pytest packages/fulcra-prefs/tests/test_consent.py -v`
Expected: 5 PASS

- [ ] **Step 5: Commit**

```bash
git add packages/fulcra-prefs/fulcra_prefs/consent.py packages/fulcra-prefs/tests/test_consent.py
git commit -m "feat(prefs): consent grants filtered at export boundary + disclosure signals"
```

---

### Task 7: Store (`store.py`) + dependency

**Files:**
- Modify: `packages/fulcra-prefs/pyproject.toml` (add dependency)
- Create: `packages/fulcra-prefs/fulcra_prefs/store.py`
- Create: `packages/fulcra-prefs/tests/conftest.py`
- Test: `packages/fulcra-prefs/tests/test_store.py`

- [ ] **Step 1: Add the runtime dependency**

In `packages/fulcra-prefs/pyproject.toml`, change:

```toml
dependencies = []
```
to:
```toml
dependencies = [
    "fulcra-api>=0.1.33",
]
```

Run (repo root): `uv sync --all-packages` — Expected: resolves clean, lock updated.

- [ ] **Step 2: Write the fake + failing tests**

```python
# packages/fulcra-prefs/tests/conftest.py
"""FakeFulcraAPI mirrors the exact fulcra_api.core.FulcraAPI methods the
store uses: list_files / resolve_filepath / download_file / upload_file /
fulcra_api (generic request). Keep method signatures in lockstep with the
real library (fulcra-api>=0.1.33)."""
import io
import json
import pytest


class FakeResponse:
    def __init__(self, body: bytes):
        self._body = body
    def read(self) -> bytes:
        return self._body


class FakeFulcraAPI:
    def __init__(self):
        self.files: dict[str, bytes] = {}      # path -> content
        self.ingested: list[dict] = []         # posted record bodies
        self.fail_ingest = False

    # --- file library (matches fulcra_api.core.FulcraAPI shapes) ---
    def resolve_filepath(self, filepath, all_versions=False):
        if filepath not in self.files:
            return []
        return [{"id": f"v-{filepath}", "name": filepath.rsplit('/', 1)[-1]}]

    def download_file(self, file_id):
        path = file_id[2:]                      # "v-<path>"
        return FakeResponse(self.files[path])

    def upload_file(self, data: io.BufferedReader, file_type, file_size, filepath):
        self.files[filepath] = data.read()
        return {"url": "fake://uploaded", "id": f"v-{filepath}"}

    def list_files(self, folder_path):
        prefix = folder_path.rstrip("/") + "/"
        return [{"id": f"v-{path}", "path": path, "name": path.rsplit("/", 1)[-1]}
                for path in sorted(self.files) if path.startswith(prefix)]

    # --- generic API request (matches FulcraAPI.fulcra_api) ---
    def fulcra_api(self, path, query=None, data=None, method="GET",
                   return_http_response=False):
        if path == "/ingest/v1/record" and method == "POST":
            if self.fail_ingest:
                raise ConnectionError("simulated ingest outage")
            self.ingested.append(data)
            return b"{}"
        raise NotImplementedError(path)


@pytest.fixture
def fake_api():
    return FakeFulcraAPI()
```

```python
# packages/fulcra-prefs/tests/test_store.py
import json
import pytest
from fulcra_prefs.store import FulcraStore, PREFS_ROOT
from test_schema import make_signal

def test_prefs_root_is_namespaced():
    assert PREFS_ROOT == "prefs"

def test_write_then_read_json_roundtrip(fake_api):
    store = FulcraStore(fake_api)
    store.write_json("prefs/compiled.json", {"v": 1, "keys": {}})
    assert store.read_json("prefs/compiled.json") == {"v": 1, "keys": {}}

def test_list_json_reads_folder_children_deterministically(fake_api):
    store = FulcraStore(fake_api)
    store.write_json("prefs/signals-cache/b.json", {"id": "b"})
    store.write_json("prefs/signals-cache/a.json", {"id": "a"})
    assert [rec["id"] for rec in store.list_json("prefs/signals-cache")] == ["a", "b"]

def test_read_missing_returns_none(fake_api):
    assert FulcraStore(fake_api).read_json("prefs/compiled.json") is None

def test_written_bytes_are_canonical(fake_api):
    store = FulcraStore(fake_api)
    store.write_json("prefs/meta.json", {"b": 1.23456789, "a": 1})
    assert fake_api.files["prefs/meta.json"] == b'{"a":1,"b":1.234568}'

def test_ingest_signal_posts_data_record_v1(fake_api):
    store = FulcraStore(fake_api)
    store.ingest_signal(make_signal(id=None), data_type="MomentAnnotation/def-123")
    rec = fake_api.ingested[0]
    assert rec["specversion"] == 1
    assert rec["metadata"]["data_type"] == "MomentAnnotation/def-123"
    assert rec["metadata"]["recorded_at"] == "2026-06-01T12:00:00+00:00"
    assert rec["metadata"]["source"][0].startswith("com.fulcra-prefs.sig.")
    assert rec["metadata"]["source"][1] == "com.fulcra-prefs.capture.claude-code"
    assert json.loads(rec["data"])["key"] == "dining.cuisine.thai"
```

- [ ] **Step 3: Run to verify failure**

Run: `uv run --package fulcra-prefs pytest packages/fulcra-prefs/tests/test_store.py -v`
Expected: FAIL — `ModuleNotFoundError: fulcra_prefs.store`

- [ ] **Step 4: Implement `store.py`**

```python
# packages/fulcra-prefs/fulcra_prefs/store.py
"""The ONLY module that talks to Fulcra. Files via the fulcra_api library
(list/resolve/download/upload); record writes via the generic request method
to /ingest/v1/record — the library has no record-write helper yet (see
FULCRA-PRIMITIVES.md); switch to CLI/lib annotation commands when they land."""
from __future__ import annotations
import io
import json
from .schema import Signal, canonical_json, temp_signal_id, CAPTURE_SOURCE_PREFIX

PREFS_ROOT = "prefs"
META_PATH = f"{PREFS_ROOT}/meta.json"
COMPILED_PATH = f"{PREFS_ROOT}/compiled.json"
CONSENT_PATH = f"{PREFS_ROOT}/consent.json"


def platform_path(platform: str) -> str:
    return f"{PREFS_ROOT}/platforms/{platform}.json"


class FulcraStore:
    def __init__(self, api):
        self._api = api                      # fulcra_api.core.FulcraAPI (or fake)

    def read_json(self, path: str):
        matches = self._api.resolve_filepath(path)
        if not matches:
            return None
        resp = self._api.download_file(matches[0]["id"])
        return json.loads(resp.read().decode())

    def write_json(self, path: str, obj) -> None:
        body = canonical_json(obj).encode()
        self._api.upload_file(io.BytesIO(body), "application/json",
                              len(body), path)

    def list_json(self, folder_path: str) -> list[dict]:
        """List direct JSON children under a folder. Used by the v1 signals-cache
        workaround as one-file-per-signal shards, avoiding a shared remote RMW
        file that concurrent captures could clobber."""
        out = []
        for rec in self._api.list_files(folder_path):
            resp = self._api.download_file(rec["id"])
            out.append(json.loads(resp.read().decode()))
        return out

    def ingest_signal(self, sig: Signal, data_type: str) -> None:
        sid = sig.id or temp_signal_id(sig.key, sig.observed_at, sig.platform)
        record = {
            "data": json.dumps(sig.to_payload()),
            "metadata": {
                "content_type": "application/json",
                "data_type": data_type,
                "recorded_at": sig.observed_at,
                "source": [sid, f"{CAPTURE_SOURCE_PREFIX}{sig.platform}"],
            },
            "specversion": 1,
        }
        self._api.fulcra_api("/ingest/v1/record", data=record, method="POST")
```

- [ ] **Step 5: Run to verify pass**

Run: `uv run --package fulcra-prefs pytest packages/fulcra-prefs/tests/test_store.py -v`
Expected: 6 PASS

- [ ] **Step 6: Commit**

```bash
git add packages/fulcra-prefs/pyproject.toml uv.lock packages/fulcra-prefs/fulcra_prefs/store.py packages/fulcra-prefs/tests/conftest.py packages/fulcra-prefs/tests/test_store.py
git commit -m "feat(prefs): Fulcra store — canonical file writes + DataRecordV1 signal ingest"
```

---

### Task 8: Outbox + capture (`outbox.py`, `capture.py`)

**Files:**
- Create: `packages/fulcra-prefs/fulcra_prefs/outbox.py`
- Create: `packages/fulcra-prefs/fulcra_prefs/capture.py`
- Test: `packages/fulcra-prefs/tests/test_capture_outbox.py`

- [ ] **Step 1: Write the failing tests**

```python
# packages/fulcra-prefs/tests/test_capture_outbox.py
import json
from datetime import datetime, timezone
from fulcra_prefs.capture import capture_signal
from fulcra_prefs.outbox import Outbox
from fulcra_prefs.schema import parse_record

NOW = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)

def _capture(fake_api, tmp_path, **over):
    from fulcra_prefs.store import FulcraStore
    args = dict(key="dining.cuisine.thai", value={"liked": True}, strength=0.8,
                kind="preference", scope="global", confidence=0.9,
                half_life_days=90.0, platform="claude-code", agent=None,
                session=None, supersedes=None)
    args.update(over)
    return capture_signal(FulcraStore(fake_api), Outbox(tmp_path / "outbox"),
                          data_type="MomentAnnotation/def-123", now=NOW, **args)

def test_capture_ingests_one_record(fake_api, tmp_path):
    sig = _capture(fake_api, tmp_path)
    assert len(fake_api.ingested) == 1
    assert sig.key == "dining.cuisine.thai"
    assert sig.observed_at == "2026-06-10T12:00:00+00:00"

def test_capture_spools_to_outbox_on_failure(fake_api, tmp_path):
    fake_api.fail_ingest = True
    _capture(fake_api, tmp_path)
    box = Outbox(tmp_path / "outbox")
    assert len(box.pending()) == 1
    spooled = box.pending()[0]
    assert json.loads(spooled["data"])["key"] == "dining.cuisine.thai"

def test_outbox_flush_retries_and_clears(fake_api, tmp_path):
    fake_api.fail_ingest = True
    _capture(fake_api, tmp_path)
    fake_api.fail_ingest = False
    from fulcra_prefs.store import FulcraStore
    box = Outbox(tmp_path / "outbox")
    flushed = box.flush(FulcraStore(fake_api))
    assert flushed == 1
    assert box.pending() == []
    assert len(fake_api.ingested) == 1

def test_spooled_record_parses_back_to_signal_with_temp_id(fake_api, tmp_path):
    fake_api.fail_ingest = True
    _capture(fake_api, tmp_path)
    spooled = Outbox(tmp_path / "outbox").pending()[0]
    env = {"id": None, "recorded_at": spooled["metadata"]["recorded_at"],
           "sources": spooled["metadata"]["source"], "data": spooled["data"]}
    assert parse_record(env).id.startswith("com.fulcra-prefs.sig.")
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run --package fulcra-prefs pytest packages/fulcra-prefs/tests/test_capture_outbox.py -v`
Expected: FAIL — `ModuleNotFoundError: fulcra_prefs.capture`

- [ ] **Step 3: Implement `outbox.py` and `capture.py`**

```python
# packages/fulcra-prefs/fulcra_prefs/outbox.py
"""Local spool for records that failed to ingest (tier-1 resilience: a capture
never loses data because the network blinked). One JSON file per record;
flush() re-posts and deletes on success, keeps on failure."""
from __future__ import annotations
import json
from pathlib import Path


class Outbox:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def spool(self, record: dict) -> Path:
        sid = record["metadata"]["source"][0].rsplit(".", 1)[-1]
        p = self.root / f"{sid}.json"
        p.write_text(json.dumps(record, sort_keys=True))
        return p

    def pending(self) -> list[dict]:
        return [json.loads(p.read_text())
                for p in sorted(self.root.glob("*.json"))]

    def flush(self, store) -> int:
        flushed = 0
        for p in sorted(self.root.glob("*.json")):
            record = json.loads(p.read_text())
            try:
                store._api.fulcra_api("/ingest/v1/record", data=record,
                                      method="POST")
            except Exception:
                continue                     # keep spooled; retry next flush
            p.unlink()
            flushed += 1
        return flushed
```

```python
# packages/fulcra-prefs/fulcra_prefs/capture.py
"""Build + ingest one signal. On ingest failure the fully-formed record is
spooled to the outbox — the temp signal id in metadata.source survives, so
supersedes references stay valid after a later flush (SPEC.md, db46fb5)."""
from __future__ import annotations
import json
from datetime import datetime
from .outbox import Outbox
from .schema import Signal, temp_signal_id, CAPTURE_SOURCE_PREFIX
from .store import FulcraStore


def capture_signal(store: FulcraStore, outbox: Outbox, *, data_type: str,
                   now: datetime, key: str, value, strength: float,
                   kind: str = "preference", scope: str = "global",
                   confidence: float = 1.0, half_life_days: float | None = 90.0,
                   platform: str = "unknown", agent: str | None = None,
                   session: str | None = None,
                   supersedes: str | None = None) -> Signal:
    observed = now.isoformat()
    sig = Signal(id=temp_signal_id(key, observed, platform), kind=kind, key=key,
                 scope=scope, value=value, strength=strength,
                 confidence=confidence, half_life_days=half_life_days,
                 observed_at=observed, platform=platform, agent=agent,
                 session=session, supersedes=supersedes)
    try:
        store.ingest_signal(sig, data_type=data_type)
    except Exception:
        record = {"data": json.dumps(sig.to_payload()),
                  "metadata": {"content_type": "application/json",
                               "data_type": data_type,
                               "recorded_at": observed,
                               "source": [sig.id,
                                          f"{CAPTURE_SOURCE_PREFIX}{platform}"]},
                  "specversion": 1}
        outbox.spool(record)
    return sig
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run --package fulcra-prefs pytest packages/fulcra-prefs/tests/test_capture_outbox.py -v`
Expected: 4 PASS

- [ ] **Step 5: Commit**

```bash
git add packages/fulcra-prefs/fulcra_prefs/outbox.py packages/fulcra-prefs/fulcra_prefs/capture.py packages/fulcra-prefs/tests/test_capture_outbox.py
git commit -m "feat(prefs): capture with outbox spool/flush so failed ingests never lose signals"
```

---

### Task 9: Inject (`inject.py`)

**Files:**
- Create: `packages/fulcra-prefs/fulcra_prefs/inject.py`
- Test: `packages/fulcra-prefs/tests/test_inject.py` (add to plan structure — small, focused)

- [ ] **Step 1: Write the failing tests**

```python
# packages/fulcra-prefs/tests/test_inject.py
from fulcra_prefs.inject import render_block

DOC = {"v": 1, "compiled_at": "2026-06-10T12:00:00+00:00",
       "keys": {"dining.cuisine.thai": {"value": True, "weight": 0.8,
                                        "confidence": 0.9,
                                        "observed_at": "2026-06-01T00:00:00+00:00",
                                        "n_signals": 3, "sources": ["claude-code"]},
                "schedule.no-meetings-before": {"value": "10:00", "weight": 1.0,
                                                "confidence": 1.0,
                                                "observed_at": "2026-05-01T00:00:00+00:00",
                                                "n_signals": 1, "sources": ["codex"],
                                                "stale": True}}}

def test_render_contains_keys_weights_and_header():
    out = render_block(DOC, platform="claude-code")
    assert "# User preferences (fulcra-prefs)" in out
    assert "dining.cuisine.thai" in out and "+0.80" in out
    assert "compiled 2026-06-10" in out

def test_stale_entries_marked():
    out = render_block(DOC, platform="claude-code")
    assert "schedule.no-meetings-before" in out and "(stale)" in out

def test_empty_doc_renders_nothing():
    assert render_block({"v": 1, "compiled_at": "x", "keys": {}},
                        platform="claude-code") == ""

def test_none_doc_renders_nothing():
    assert render_block(None, platform="claude-code") == ""
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run --package fulcra-prefs pytest packages/fulcra-prefs/tests/test_inject.py -v`
Expected: FAIL — `ModuleNotFoundError: fulcra_prefs.inject`

- [ ] **Step 3: Implement `inject.py`**

```python
# packages/fulcra-prefs/fulcra_prefs/inject.py
"""Render a compiled doc as a session-bootstrap context block. Empty output
for missing/empty docs is a contract: the injector must NEVER break a session
start (SPEC.md errors & edges)."""
from __future__ import annotations


def render_block(doc: dict | None, platform: str) -> str:
    if not doc or not doc.get("keys"):
        return ""
    lines = [f"# User preferences (fulcra-prefs) — {platform}, "
             f"compiled {doc['compiled_at'][:10]}", ""]
    for key in sorted(doc["keys"]):
        e = doc["keys"][key]
        stale = " (stale)" if e.get("stale") else ""
        lines.append(f"- {key}: {e['value']!r} "
                     f"[{e['weight']:+.2f}]{stale}")
    lines.append("")
    lines.append("Apply these as standing user preferences. Weights in [-1,1]; "
                 "negative = aversion. Capture new/changed preferences via "
                 "fulcra-prefs (see the fulcra-prefs skill).")
    return "\n".join(lines)
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run --package fulcra-prefs pytest packages/fulcra-prefs/tests/test_inject.py -v`
Expected: 4 PASS

- [ ] **Step 5: Commit**

```bash
git add packages/fulcra-prefs/fulcra_prefs/inject.py packages/fulcra-prefs/tests/test_inject.py
git commit -m "feat(prefs): session-bootstrap context block renderer (never breaks session start)"
```

---

### Task 10: CLI (`cli.py`)

**Files:**
- Create: `packages/fulcra-prefs/fulcra_prefs/cli.py`
- Modify: `packages/fulcra-prefs/pyproject.toml` (console script)
- Test: `packages/fulcra-prefs/tests/test_cli.py`

- [ ] **Step 1: Add the console script to pyproject.toml**

```toml
[project.scripts]
fulcra-prefs = "fulcra_prefs.cli:main"
```

- [ ] **Step 2: Write the failing tests (CLI wired against the fake via dependency injection)**

```python
# packages/fulcra-prefs/tests/test_cli.py
import json
from datetime import datetime, timezone
import pytest
from fulcra_prefs.cli import run
from fulcra_prefs.store import FulcraStore, META_PATH, COMPILED_PATH, CONSENT_PATH
from test_schema import make_signal

NOW = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)

@pytest.fixture
def env(fake_api, tmp_path):
    """run(argv, api=..., outbox_dir=..., now=...) is the testable entrypoint;
    main() only adds real FulcraAPI + real clock."""
    store = FulcraStore(fake_api)
    store.write_json(META_PATH, {"definition_id": "def-123",
                                 "data_type": "MomentAnnotation/def-123", "v": 1})
    def call(*argv):
        return run(list(argv), api=fake_api, outbox_dir=tmp_path / "outbox", now=NOW)
    return call, fake_api, store

def test_capture_then_compile_then_get(env, capsys):
    call, fake_api, store = env
    assert call("capture", "--key", "dining.cuisine.thai", "--value",
                '{"liked": true}', "--strength", "0.8",
                "--platform", "claude-code") == 0
    assert len(fake_api.ingested) == 1
    # compile reads signals back; fake get-records: feed ingested through store
    assert call("compile") == 0
    compiled = store.read_json(COMPILED_PATH)
    assert "dining.cuisine.thai" in compiled["keys"]
    assert call("get") == 0
    out = json.loads(capsys.readouterr().out)
    assert out["keys"]["dining.cuisine.thai"]["value"] == {"liked": True}

def test_get_for_audience_filters_and_logs_disclosure(env, capsys):
    call, fake_api, store = env
    call("capture", "--key", "dining.cuisine.thai", "--value", "true",
         "--strength", "0.8", "--platform", "claude-code")
    call("capture", "--key", "health.sleep.target", "--value", "8",
         "--strength", "1.0", "--platform", "claude-code")
    call("compile")
    call("consent", "grant", "--key-glob", "dining.*", "--audience", "ea")
    n_before = len(fake_api.ingested)
    assert call("get", "--for", "ea") == 0
    out = json.loads(capsys.readouterr().out)
    assert list(out["keys"]) == ["dining.cuisine.thai"]
    assert len(fake_api.ingested) == n_before + 1          # disclosure logged
    disclosure = json.loads(fake_api.ingested[-1]["data"])
    assert disclosure["kind"] == "consent"

def test_inject_prints_block_or_nothing(env, capsys):
    call, *_ = env
    assert call("inject", "--platform", "claude-code") == 0
    assert capsys.readouterr().out == ""                   # no compiled doc: silent
    call("capture", "--key", "k.a", "--value", "1", "--strength", "0.5",
         "--platform", "claude-code")
    call("compile")
    call("inject", "--platform", "claude-code")
    assert "# User preferences (fulcra-prefs)" in capsys.readouterr().out

def test_solve_from_files(env, tmp_path, capsys):
    call, *_ = env
    options = [{"id": "thai", "keys": ["dining.cuisine.thai"]},
               {"id": "bbq", "keys": ["dining.cuisine.bbq"]}]
    docs = {"alice": {"v": 1, "compiled_at": "x",
                      "keys": {"dining.cuisine.thai": {"weight": 0.9, "value": True}}}}
    (tmp_path / "options.json").write_text(json.dumps(options))
    (tmp_path / "docs.json").write_text(json.dumps(docs))
    assert call("solve", "--options", str(tmp_path / "options.json"),
                "--participants", str(tmp_path / "docs.json")) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ranked"][0]["id"] == "thai"
    assert out["trace"]

def test_missing_meta_gives_actionable_error(fake_api, tmp_path, capsys):
    rc = run(["capture", "--key", "k", "--value", "1", "--strength", "0.5",
              "--platform", "x"], api=fake_api, outbox_dir=tmp_path, now=NOW)
    assert rc == 2
    assert "onboard" in capsys.readouterr().err

def test_signal_cache_shards_do_not_clobber_each_other(env):
    from fulcra_prefs.cli import _append_signal_cache, _load_cached_signals
    call, fake_api, store = env
    _append_signal_cache(store, make_signal(id="sig-a", key="k.a"))
    _append_signal_cache(store, make_signal(id="sig-b", key="k.b"))
    assert "prefs/signals-cache/sig-a.json" in fake_api.files
    assert "prefs/signals-cache/sig-b.json" in fake_api.files
    assert {s.id for s in _load_cached_signals(store)} == {"sig-a", "sig-b"}
```

- [ ] **Step 3: Run to verify failure**

Run: `uv run --package fulcra-prefs pytest packages/fulcra-prefs/tests/test_cli.py -v`
Expected: FAIL — `ModuleNotFoundError: fulcra_prefs.cli`

- [ ] **Step 4: Implement `cli.py`**

```python
# packages/fulcra-prefs/fulcra_prefs/cli.py
"""fulcra-prefs CLI. run(argv, api, outbox_dir, now) is dependency-injected
for tests; main() binds the real FulcraAPI (reusing the user's `fulcra auth
login` credentials), the real outbox dir, and the real clock.

Signal reads in v1 go through a compile cache in the file library: capture
posts the canonical signal to Fulcra and writes one independent cache shard per
signal id under `prefs/signals-cache/`. Because the fulcra-api library has no
record-read-by-definition helper for arbitrary windows wired here yet, compile
lists those cache shards. Do NOT use one shared `signals-cache.json` file:
that would be a remote read-modify-write race across concurrently-capturing
platforms and would violate SPEC.md's atomic-capture rationale. The shard cache
is an implementation detail replaced by real get-records reads when CLI
annotation commands land (tracked on the bus)."""
from __future__ import annotations
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from .capture import capture_signal
from .compileprefs import compile_signals
from .consent import disclosure_signal, filter_for_audience
from .inject import render_block
from .outbox import Outbox
from .schema import Signal, canonical_json, parse_record
from .solver import solve
from .store import (FulcraStore, COMPILED_PATH, CONSENT_PATH, META_PATH,
                    PREFS_ROOT, platform_path)

SIGNALS_CACHE_PREFIX = f"{PREFS_ROOT}/signals-cache"


def _store(api) -> FulcraStore:
    return FulcraStore(api)


def _require_meta(store: FulcraStore) -> dict | None:
    meta = store.read_json(META_PATH)
    if not meta:
        print("fulcra-prefs: not onboarded — run `fulcra-prefs onboard` first "
              "(creates the Preference Signals definition + prefs/meta.json).",
              file=sys.stderr)
    return meta


def _load_cached_signals(store: FulcraStore) -> list[Signal]:
    return [parse_record(env) for env in store.list_json(SIGNALS_CACHE_PREFIX)]


def _append_signal_cache(store: FulcraStore, sig: Signal) -> None:
    # One file per signal id: concurrent captures write disjoint paths instead
    # of racing on a shared cache blob. The cache record mirrors get-records
    # enough for parse_record and preserves the temp id in sources.
    sid = sig.id
    env = {"id": sid, "recorded_at": sig.observed_at,
           "sources": [sid], "data": json.dumps(sig.to_payload())}
    store.write_json(f"{SIGNALS_CACHE_PREFIX}/{sid}.json", env)


def cmd_onboard(args, api, now) -> int:
    store = _store(api)
    if store.read_json(META_PATH):
        print("already onboarded")
        return 0
    created = api.create_annotation("moment", "Preference Signals",
                                    "Typed preference/fact/consent signals "
                                    "captured by fulcra-prefs.", [], None,
                                    None, None, None)
    def_id = created["id"] if isinstance(created, dict) else created
    store.write_json(META_PATH, {"definition_id": def_id,
                                 "data_type": f"MomentAnnotation/{def_id}",
                                 "v": 1})
    print(f"onboarded: definition {def_id}")
    return 0


def cmd_capture(args, api, outbox_dir, now) -> int:
    store = _store(api)
    meta = _require_meta(store)
    if not meta:
        return 2
    sig = capture_signal(
        store, Outbox(outbox_dir), data_type=meta["data_type"], now=now,
        key=args.key, value=json.loads(args.value), strength=args.strength,
        kind=args.kind, scope=args.scope, confidence=args.confidence,
        half_life_days=args.half_life, platform=args.platform,
        agent=args.agent, session=args.session, supersedes=args.supersedes)
    _append_signal_cache(store, sig)
    print(f"captured {sig.id}")
    return 0


def cmd_compile(args, api, outbox_dir, now) -> int:
    store = _store(api)
    if not _require_meta(store):
        return 2
    Outbox(outbox_dir).flush(store)
    docs = compile_signals(_load_cached_signals(store), now)
    store.write_json(COMPILED_PATH, docs["global"])
    for p, doc in docs["platforms"].items():
        store.write_json(platform_path(p), doc)
    print(f"compiled {len(docs['global']['keys'])} keys, "
          f"{len(docs['platforms'])} platform views")
    return 0


def cmd_get(args, api, outbox_dir, now) -> int:
    store = _store(api)
    path = platform_path(args.platform) if args.platform else COMPILED_PATH
    doc = store.read_json(path) or {"v": 1, "compiled_at": now.isoformat(),
                                    "keys": {}}
    if args.audience:
        grants = (store.read_json(CONSENT_PATH) or {"grants": []})["grants"]
        doc = filter_for_audience(doc, grants, args.audience, now)
        meta = store.read_json(META_PATH)
        if meta and doc["keys"]:
            sig = disclosure_signal(sorted(doc["keys"]), args.audience,
                                    platform=args.platform or "cli", now=now)
            store.ingest_signal(sig, data_type=meta["data_type"])
    print(canonical_json(doc))
    return 0


def cmd_consent(args, api, outbox_dir, now) -> int:
    store = _store(api)
    consent = store.read_json(CONSENT_PATH) or {"v": 1, "grants": []}
    if args.consent_action == "grant":
        consent["grants"].append({"key_glob": args.key_glob,
                                  "audience": args.audience,
                                  "level": args.level,
                                  "granted_at": now.isoformat(),
                                  "expires": args.expires})
        store.write_json(CONSENT_PATH, consent)
        print(f"granted {args.key_glob} -> {args.audience}")
    elif args.consent_action == "revoke":
        before = len(consent["grants"])
        consent["grants"] = [g for g in consent["grants"]
                             if not (g["audience"] == args.audience
                                     and g["key_glob"] == args.key_glob)]
        store.write_json(CONSENT_PATH, consent)
        print(f"revoked {before - len(consent['grants'])} grant(s)")
    else:
        print(canonical_json(consent))
    return 0


def cmd_inject(args, api, outbox_dir, now) -> int:
    store = _store(api)
    doc = store.read_json(platform_path(args.platform)) \
        or store.read_json(COMPILED_PATH)
    block = render_block(doc, platform=args.platform)
    if block:
        print(block)
    return 0


def cmd_solve(args, api, outbox_dir, now) -> int:
    options = json.loads(Path(args.options).read_text())
    participants = json.loads(Path(args.participants).read_text())
    result = solve(options, participants, policy=args.policy,
                   veto_threshold=args.veto_threshold)
    print(canonical_json(result))
    return 0


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="fulcra-prefs")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("onboard")

    c = sub.add_parser("capture")
    c.add_argument("--key", required=True)
    c.add_argument("--value", required=True, help="JSON value")
    c.add_argument("--strength", type=float, required=True)
    c.add_argument("--kind", default="preference",
                   choices=["preference", "fact", "consent"])
    c.add_argument("--scope", default="global")
    c.add_argument("--confidence", type=float, default=1.0)
    c.add_argument("--half-life", type=float, default=90.0, dest="half_life")
    c.add_argument("--platform", required=True)
    c.add_argument("--agent")
    c.add_argument("--session")
    c.add_argument("--supersedes")

    sub.add_parser("compile")

    g = sub.add_parser("get")
    g.add_argument("--platform")
    g.add_argument("--for", dest="audience")

    co = sub.add_parser("consent")
    co_sub = co.add_subparsers(dest="consent_action", required=True)
    gr = co_sub.add_parser("grant")
    gr.add_argument("--key-glob", required=True)
    gr.add_argument("--audience", required=True)
    gr.add_argument("--level", default="read", choices=["read", "solve"])
    gr.add_argument("--expires")
    rv = co_sub.add_parser("revoke")
    rv.add_argument("--key-glob", required=True)
    rv.add_argument("--audience", required=True)
    co_sub.add_parser("list")

    i = sub.add_parser("inject")
    i.add_argument("--platform", required=True)

    s = sub.add_parser("solve")
    s.add_argument("--options", required=True, help="path to options JSON")
    s.add_argument("--participants", required=True,
                   help="path to {name: compiled_doc} JSON")
    s.add_argument("--policy", default="weighted-sum",
                   choices=["weighted-sum", "hard-veto"])
    s.add_argument("--veto-threshold", type=float, default=-0.5)
    return p


def run(argv, api, outbox_dir, now) -> int:
    args = _parser().parse_args(argv)
    handlers = {"onboard": lambda: cmd_onboard(args, api, now),
                "capture": lambda: cmd_capture(args, api, outbox_dir, now),
                "compile": lambda: cmd_compile(args, api, outbox_dir, now),
                "get": lambda: cmd_get(args, api, outbox_dir, now),
                "consent": lambda: cmd_consent(args, api, outbox_dir, now),
                "inject": lambda: cmd_inject(args, api, outbox_dir, now),
                "solve": lambda: cmd_solve(args, api, outbox_dir, now)}
    return handlers[args.command]()


def main() -> int:
    from fulcra_api.core import FulcraAPI
    api = FulcraAPI()
    # Reuse the fulcra CLI's persisted credentials (~/.config/fulcra/...).
    from fulcra_api.cli.utils import load_creds, save_creds
    creds = load_creds()
    if creds is None:
        print("fulcra-prefs: not authenticated — run `fulcra auth login` first.",
              file=sys.stderr)
        return 2
    api.credentials = creds
    api.refresh_callback = save_creds
    outbox_dir = Path.home() / ".local/state/fulcra-prefs/outbox"
    return run(sys.argv[1:], api=api, outbox_dir=outbox_dir,
               now=datetime.now(timezone.utc))


if __name__ == "__main__":
    sys.exit(main())
```

**Note for the implementer:** `main()`'s credential wiring (`api.credentials = creds`, `api.refresh_callback = save_creds`) must be verified against `fulcra_api` 0.1.33's actual attribute names before committing — read `/tmp/fulcra-api-python/fulcra_api/cli/utils.py` `requires_auth` for the exact pattern and copy it. The `cmd_onboard` call signature for `create_annotation` likewise: verify against `fulcra_api/core.py:1548` and adjust arguments to match. These are the only two integration points not covered by the fake.

- [ ] **Step 5: Run to verify pass**

Run: `uv run --package fulcra-prefs pytest packages/fulcra-prefs/tests/test_cli.py -v`
Expected: 6 PASS

- [ ] **Step 6: Run the whole suite**

Run: `uv run --package fulcra-prefs pytest packages/fulcra-prefs/tests -v`
Expected: all tests pass (≈44)

- [ ] **Step 7: Commit**

```bash
git add packages/fulcra-prefs/pyproject.toml packages/fulcra-prefs/fulcra_prefs/cli.py packages/fulcra-prefs/tests/test_cli.py
git commit -m "feat(prefs): fulcra-prefs CLI — onboard/capture/compile/get/consent/inject/solve"
```

---

### Task 11: Skill + tier-2 recipes (`skill/`)

**Files:**
- Create: `packages/fulcra-prefs/skill/SKILL.md`
- Create: `packages/fulcra-prefs/skill/references/fulcra-prefs-tier2-http.md`
- Create: `packages/fulcra-prefs/skill/references/fulcra-prefs-capture.md`

No tests (docs); content quality gate = the adversarial review.

- [ ] **Step 1: Write `SKILL.md`** (agent-skills frontmatter conventions, per fulcradynamics/agent-skills)

```markdown
---
name: fulcra-prefs
description: "Read, capture, and apply the user's cross-platform preferences stored in Fulcra. Routes by agent capability: CLI (preferred), raw HTTP, or MCP read-only."
homepage: "https://github.com/ashfulcra/fulcra-tools/tree/main/packages/fulcra-prefs"
license: "MIT"
user-invocable: true
metadata: { "openclaw": { "emoji": "⚙️" } }
---

# fulcra-prefs

The user's preferences and facts live in their Fulcra account as typed,
decaying signals, compiled into per-platform preference documents. Your job:
LOAD them at session start, APPLY them, and CAPTURE new ones.

## Pick your path

1. **You can run shell commands** → use the CLI. Setup once:
   `uv tool install fulcra-prefs` (and `fulcra auth login` if not authed).
   - Load: `fulcra-prefs inject --platform <your-platform>` → prepend output
     to your working context. Empty output = no prefs yet; continue silently.
   - Capture: `fulcra-prefs capture --key <ns.key> --value '<json>'
     --strength <-1..1> --platform <your-platform>` (see
     references/fulcra-prefs-capture.md for when and what to capture).
   - Refresh: `fulcra-prefs compile` (run after captures; cheap).
2. **You can make HTTP requests but not run commands** → follow
   references/fulcra-prefs-tier2-http.md (device-flow auth + direct API).
3. **You only have the Fulcra MCP** → you can read user data the MCP exposes,
   but preference write/read of the compiled docs is not available via MCP
   today. Tell the user to run onboarding from a CLI-capable agent.

## Onboarding a new user

If `inject`/`get` report not-onboarded: run `fulcra-prefs onboard` (requires
`fulcra auth login` first — account auto-creates on first login). For a full
guided platform onboarding, hand off to the fulcra-onboarding skill:
https://github.com/fulcradynamics/agent-skills/blob/main/skills/fulcra-onboarding/SKILL.md

## Rules

- NEVER print or store the user's access token.
- Respect scopes: per-platform overrides beat global; negative weight =
  aversion (don't suggest what they dislike).
- Capture is consent-adjacent: only capture what the user said or confirmed —
  see the capture reference for the heuristics.
```

- [ ] **Step 2: Write `references/fulcra-prefs-tier2-http.md`**

```markdown
# fulcra-prefs over raw HTTP (no shell)

For agents that can make HTTP requests but cannot run a CLI. All endpoints on
`https://api.fulcradynamics.com`; auth domain `https://fulcra.us.auth0.com`.
Background: FULCRA-PRIMITIVES.md at the repo root.

## 1. Authenticate (device flow, three calls)

1. `POST https://fulcra.us.auth0.com/oauth/device/code`
   form: `client_id=48p3VbMnr5kMuJAUe9gJ9vjmdWLdnqZt`,
   `audience=https://api.fulcradynamics.com/`,
   `scope=openid profile email offline_access`
2. Show the user `verification_uri_complete`; they approve in a browser.
3. Poll `POST https://fulcra.us.auth0.com/oauth/token`
   form: `client_id=...`, `grant_type=urn:ietf:params:oauth:grant-type:device_code`,
   `device_code=<from step 1>` → `{access_token, refresh_token, expires_in}`.
   Send `Authorization: Bearer <access_token>` on every call below.
   NEVER show the token to the user or store it anywhere visible.

## 2. Read the compiled preferences (one GET each)

1. `GET /input/v1/file_upload?path=prefs&state=uploaded` → find
   `compiled.json` (or `platforms/<your-platform>.json`) and its id.
2. `GET /input/v1/file_upload/{id}/download` → the compiled doc. Apply it:
   keys are namespaced prefs, `weight` in [-1,1], negative = aversion,
   `stale: true` = verify with the user before relying on it.

## 3. Capture a signal (one POST)

`POST /ingest/v1/record` with JSON body:

    {"data": "{\"v\":1,\"kind\":\"preference\",\"key\":\"dining.cuisine.thai\",
      \"scope\":\"global\",\"value\":{\"liked\":true},\"strength\":0.8,
      \"confidence\":0.9,\"half_life_days\":90,
      \"source\":{\"platform\":\"chatgpt\",\"agent\":null,\"session\":null},
      \"supersedes\":null}",
     "metadata": {"content_type": "application/json",
       "data_type": "<data_type from prefs/meta.json>",
       "recorded_at": "<now, ISO8601 UTC>",
       "source": ["com.fulcra-prefs.sig.<24-hex-of-sha256(key|recorded_at|platform)>",
                   "com.fulcra-prefs.capture.<your-platform>"]},
     "specversion": 1}

Read `<data_type>` from `prefs/meta.json` (same two-GET pattern as step 2).
Retry once on failure, then tell the user the capture didn't stick.

## 4. What you cannot do at this tier

Compile and solve run only where code runs (CLI-capable agents or cron).
Your captures appear in compiled docs after the next compile elsewhere.
```

- [ ] **Step 3: Write `references/fulcra-prefs-capture.md`**

```markdown
# When and what to capture

Capture creates durable, user-visible records on the user's Fulcra timeline.
Be conservative: capture what the user SAID, not what you inferred silently.

CAPTURE when:
- Explicit ask: "remember that I…", "from now on…", "I always/never want…"
  → strength ±0.8–1.0, half_life 365 (or null for hard facts).
- Correction of your behavior: "no, I prefer X" → strength ±0.7, half_life 180,
  and set `supersedes` to the prior signal's id if you know it.
- Pattern you observed AND the user confirmed when asked → strength ±0.4–0.6,
  half_life 90.

DO NOT capture:
- Unconfirmed inferences, one-off task context, anything secret-like
  (credentials, health details the user didn't ask to store), or another
  person's preferences.

Conventions: keys are dot-namespaced (`dining.cuisine.thai`,
`schedule.no-meetings-before`, `comms.tone.concise`); `scope` is `global`
unless the user scoped it ("only in Claude Code" → `platform:claude-code`);
aversions are negative strength on the same key, not a `.not` key.
After capturing in a CLI session, run `fulcra-prefs compile`.
```

- [ ] **Step 4: Commit**

```bash
git add packages/fulcra-prefs/skill
git commit -m "feat(prefs): agent skill with tier routing, raw-HTTP recipes, capture heuristics"
```

---

### Task 12: README, Claude Code hook doc, final verification

**Files:**
- Create: `packages/fulcra-prefs/README.md`
- Modify: `packages/fulcra-prefs/pyproject.toml` (readme → README.md)

- [ ] **Step 1: Write `README.md`** — sections: what it is (3 sentences); install (`uv tool install fulcra-prefs`, needs `fulcra auth login`); quickstart (onboard → capture → compile → inject); the Claude Code hook:

```markdown
## Claude Code session hook

Add to `~/.claude/settings.json`:

    {"hooks": {"SessionStart": [{"hooks": [{"type": "command",
      "command": "fulcra-prefs compile >/dev/null 2>&1; fulcra-prefs inject --platform claude-code"}]}]}}

The hook recompiles (per the spec: compile runs at every tier-1 session
start), then prints the preference block as session context — or nothing at
all if you haven't onboarded. It never breaks a session start: both commands
fail silent by design.
```

…plus: how it works (signals → compile → files, two paragraphs, link SPEC.md), the skill (`skill/SKILL.md`), test instructions, and the v1 limitation list from the spec cut-line (incl. the signals-cache note and its bus-tracked replacement).

- [ ] **Step 2: Point pyproject readme at README.md**

```toml
readme = "README.md"
```

- [ ] **Step 3: Full verification**

```bash
uv sync --all-packages                                   # lock still clean
uv run --package fulcra-prefs pytest packages/fulcra-prefs/tests -v   # all pass
uv build --package fulcra-prefs --wheel --out-dir /tmp/prefs-wheel    # builds
git diff --check                                          # no whitespace damage
```

- [ ] **Step 4: Commit**

```bash
git add packages/fulcra-prefs/README.md packages/fulcra-prefs/pyproject.toml
git commit -m "docs(prefs): package README with quickstart + Claude Code hook; readme -> README.md"
```

- [ ] **Step 5: Open the PR**

```bash
git push -u origin claude-code/fulcra-prefs
gh pr create --repo ashfulcra/fulcra-tools \
  --title "feat(fulcra-prefs): v1 — signals, deterministic compile, solver, consent, CLI, skill" \
  --body "<summarize: what/why, spec link, test counts, determinism contract, v1 cut-line>"
```

Then request review on the bus from `Ashs-MBP-Work:Codex-Review-Workbook` per the global rule.

---

## Out of scope for v1 (do NOT build)

MCP write path; cross-user doc sharing; ChatGPT auto-injection; incremental
compile; cron installer; real get-records signal reads (the signals-cache
workaround is v1 — its replacement is tracked on the bus as part of the
FULCRA-PRIMITIVES rewrite task when CLI annotation commands land).

## Known risks the implementer must verify live

1. `fulcra_api` credential attribute names in `cli.main()` (Task 10 note).
2. `create_annotation` signature in `cmd_onboard` (Task 10 note).
3. The ingest endpoint accepting `data_type: "MomentAnnotation/<uuid>"` —
   verify with one live capture + `fulcra get-records` round-trip before the
   PR; if the data_type string differs, fix `meta.json`'s `data_type` value
   at onboard time (single source of truth — nothing else changes).
```
