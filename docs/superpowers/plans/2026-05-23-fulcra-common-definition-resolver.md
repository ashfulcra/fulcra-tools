# fulcra-common Definition Resolver Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Add a `fulcra_common.definitions.resolve_definition_id` helper that finds-or-creates Fulcra annotation definitions by canonical name, generalising the existing `fulcra-attention` `_find_attention_definition` pattern so every typed-annotation plugin handles multi-machine coherence the same way.

**Architecture:** A pure function in `fulcra-common` (`fulcra_common/definitions.py`) takes a canonical name + expected schema + injected fulcra client; returns the def id (existing or new). A new `definition_id` column on `PluginState` caches the resolution. A new `canonical_definition_name` field on `Plugin` opts in. A new `RunContext.resolved_definition_id` helper hides the resolver + state plumbing from plugin code. Three reference plugins (attention, lastfm, spotify-extended) are retrofitted to prove the pattern; the remaining 10+ typed-annotation plugins are queued as a follow-on plan.

**Tech Stack:** Python 3.11+ (matching existing packages), `pytest`, no new third-party deps.

**Spec:** `docs/superpowers/specs/2026-05-23-fulcra-common-definition-resolver-design.md` (committed as `1b15f45`).

**Branch:** All work commits directly on `main` per user direction. **Do not push.** **Each subagent MUST verify it is on `main` (not a detached HEAD) at the start and end of its task** — a prior subagent left HEAD detached. Add `git symbolic-ref HEAD` to your pre/post checks.

---

## File Structure

### Phase 1 — resolver core (new in `packages/fulcra-common/`)

| File | Change | Responsibility |
|---|---|---|
| `fulcra_common/definitions.py` | **New.** | `DefinitionSchemaMismatch`, `_spec_matches`, `resolve_definition_id`. |
| `tests/test_definitions.py` | **New.** | Unit tests against a fake `fulcra_client`. |

### Phase 2 — wire into the plugin runtime (in `packages/collect/`)

| File | Change | Responsibility |
|---|---|---|
| `fulcra_collect/state.py` | Modify | Add `definition_id: str | None = None` to `PluginState`; serialise it. |
| `tests/test_state.py` | Modify | Round-trip + old-state-file backwards-compat tests. |
| `fulcra_collect/plugin.py` | Modify | `canonical_definition_name` on `Plugin`; `resolved_definition_id` on `RunContext`. |
| `tests/test_plugin.py` | Modify (or create) | RunContext-helper tests with fake client + fake state. |

### Phase 3 — retrofits

| File | Change | Responsibility |
|---|---|---|
| `packages/attention/fulcra_attention/...` | Modify | Replace `_find_attention_definition` with the helper. |
| `packages/media-helpers/fulcra_media/collect_plugins.py` | Modify | Wire lastfm + spotify-extended plugins to use the helper. |

### Phase 4 — glue

| File | Change | Responsibility |
|---|---|---|
| `packages/collect/fulcra_collect/cli.py` | Modify | `fulcra-collect plugin reset-definition <id>` CLI command. |

All commands run from `/Users/Scanning/Developer/fulcra-tools`.

---

## Task 1: `definitions.py` foundation — `DefinitionSchemaMismatch` + `_spec_matches`

**Files:**
- Create: `packages/fulcra-common/fulcra_common/definitions.py`
- Create: `packages/fulcra-common/tests/test_definitions.py`

- [ ] **Step 0: Verify branch**

```bash
cd /Users/Scanning/Developer/fulcra-tools
git symbolic-ref HEAD     # must print: refs/heads/main
git status                # must be clean
```

If HEAD is detached, run `git checkout main` and stop. Report BLOCKED to the controller — do not try to recover the chain yourself.

- [ ] **Step 1: Write the failing tests**

`packages/fulcra-common/tests/test_definitions.py`:

```python
"""Tests for the definition resolver."""
from __future__ import annotations

import pytest

from fulcra_common.definitions import (
    DefinitionSchemaMismatch,
    _spec_matches,
)


def test_spec_matches_moment_by_annotation_type_only():
    existing = {"annotation_type": "moment", "name": "x"}
    assert _spec_matches(existing, {"annotation_type": "moment"}) is True
    assert _spec_matches(existing, {"annotation_type": "duration"}) is False


def test_spec_matches_duration_compares_measurement_spec():
    spec = {
        "annotation_type": "duration",
        "measurement_spec": {"unit": "seconds", "kind": "interval"},
    }
    existing = dict(spec, name="y")
    assert _spec_matches(existing, spec) is True

    different_unit = dict(spec)
    different_unit["measurement_spec"] = {"unit": "minutes", "kind": "interval"}
    assert _spec_matches(existing, different_unit) is False


def test_spec_matches_mixed_types_never_match():
    existing = {"annotation_type": "duration",
                "measurement_spec": {"unit": "seconds"}}
    assert _spec_matches(existing, {"annotation_type": "moment"}) is False


def test_definition_schema_mismatch_message_includes_both_shapes():
    existing = {"annotation_type": "duration",
                "measurement_spec": {"unit": "seconds"}}
    expected = {"annotation_type": "moment"}
    err = DefinitionSchemaMismatch("attention", existing, expected)
    msg = str(err)
    assert "attention" in msg
    assert "duration" in msg
    assert "moment" in msg
```

- [ ] **Step 2: Verify they fail**

Run: `uv run --package fulcra-common pytest packages/fulcra-common/tests/test_definitions.py -v`
Expected: ImportError on `fulcra_common.definitions`.

- [ ] **Step 3: Implement the foundation**

`packages/fulcra-common/fulcra_common/definitions.py`:

```python
"""Annotation-definition resolver — shared by every typed-annotation
plugin.

Multi-machine coherence works by adopting an existing Fulcra annotation
definition with the same canonical name across machines, instead of
each machine creating its own duplicate. See
`docs/superpowers/specs/2026-05-23-fulcra-common-definition-resolver-design.md`.
"""
from __future__ import annotations

from typing import Any


class DefinitionSchemaMismatch(RuntimeError):
    """Raised when an existing Fulcra definition with the requested
    canonical name has a schema the caller did not expect. The caller
    (typically the menubar) is expected to either retry with
    `force_new=True` or change the canonical name."""

    def __init__(self, name: str, existing: dict, expected: dict) -> None:
        self.name = name
        self.existing = existing
        self.expected = expected
        super().__init__(
            f"Fulcra definition {name!r} exists but its schema does not "
            f"match what the plugin expects; existing={existing}, "
            f"expected={expected}"
        )


def _spec_matches(existing: dict, expected: dict) -> bool:
    """Compare `existing` (as returned by Fulcra) with `expected` (as
    declared by the plugin). For Moment annotations only the
    `annotation_type` is compared (Moments carry no measurement_spec).
    For Duration annotations both `annotation_type` and
    `measurement_spec` must match."""
    if existing.get("annotation_type") != expected.get("annotation_type"):
        return False
    if expected.get("annotation_type") == "moment":
        return True
    return existing.get("measurement_spec") == expected.get("measurement_spec")
```

- [ ] **Step 4: Verify tests pass**

Run: `uv run --package fulcra-common pytest packages/fulcra-common/tests/test_definitions.py -v`
Expected: 4 passed.

- [ ] **Step 5: Post-task branch check**

```bash
git symbolic-ref HEAD     # must still print: refs/heads/main
```

- [ ] **Step 6: Commit**

```bash
git add packages/fulcra-common/fulcra_common/definitions.py packages/fulcra-common/tests/test_definitions.py
git commit -m "feat(common): definitions — DefinitionSchemaMismatch + _spec_matches

First slice of the multi-machine annotation-definition resolver. The
schema-comparison helper is the trickiest piece (Moment vs Duration
have different shapes), so it lands with its own tests before the
resolver function that uses it.

See docs/superpowers/specs/2026-05-23-fulcra-common-definition-resolver-design.md."
```

---

## Task 2: `resolve_definition_id` against a fake fulcra_client

**Files:**
- Modify: `packages/fulcra-common/fulcra_common/definitions.py`
- Modify: `packages/fulcra-common/tests/test_definitions.py`

- [ ] **Step 0: Verify branch** (same as Task 1).

- [ ] **Step 1: Write the failing tests**

Append to `packages/fulcra-common/tests/test_definitions.py`:

```python
class _FakeClient:
    """A fake fulcra_client for resolver tests. Records every call."""

    def __init__(self, existing: list[dict] | None = None) -> None:
        self.existing = list(existing or [])
        self.list_calls: list[dict] = []
        self.create_calls: list[dict] = []
        self._next_id = 100

    def list_definitions(self, *, name: str) -> list[dict]:
        self.list_calls.append({"name": name})
        return [d for d in self.existing if d.get("name") == name]

    def create_definition(self, *, name: str, **spec) -> dict:
        self.create_calls.append({"name": name, **spec})
        self._next_id += 1
        new = {"id": f"new-{self._next_id}", "name": name, **spec}
        self.existing.append(new)
        return new


from fulcra_common.definitions import resolve_definition_id


def test_resolve_creates_when_not_found():
    client = _FakeClient()
    out = resolve_definition_id(
        canonical_name="attention",
        expected_spec={"annotation_type": "duration",
                       "measurement_spec": {"unit": "seconds"}},
        fulcra_client=client,
    )
    assert out == "new-101"
    assert client.create_calls == [
        {"name": "attention", "annotation_type": "duration",
         "measurement_spec": {"unit": "seconds"}}
    ]


def test_resolve_adopts_existing_when_schema_matches():
    spec = {"annotation_type": "moment"}
    client = _FakeClient(existing=[{"id": "abc", "name": "lastfm-listens", **spec}])
    out = resolve_definition_id(
        canonical_name="lastfm-listens",
        expected_spec=spec, fulcra_client=client,
    )
    assert out == "abc"
    assert client.create_calls == []   # never created — adopted


def test_resolve_raises_on_schema_mismatch():
    client = _FakeClient(existing=[
        {"id": "abc", "name": "attention",
         "annotation_type": "moment"},
    ])
    with pytest.raises(DefinitionSchemaMismatch) as exc_info:
        resolve_definition_id(
            canonical_name="attention",
            expected_spec={"annotation_type": "duration",
                           "measurement_spec": {"unit": "seconds"}},
            fulcra_client=client,
        )
    assert exc_info.value.name == "attention"
    assert client.create_calls == []


def test_resolve_force_new_creates_even_when_match_exists():
    spec = {"annotation_type": "moment"}
    client = _FakeClient(existing=[{"id": "abc", "name": "attention", **spec}])
    out = resolve_definition_id(
        canonical_name="attention", expected_spec=spec,
        fulcra_client=client, force_new=True, machine_id="mini",
    )
    assert out == "new-101"
    assert client.create_calls == [
        {"name": "attention (mini)", "annotation_type": "moment"}
    ]


def test_resolve_force_new_defaults_machine_id_to_platform_node(monkeypatch):
    monkeypatch.setattr("platform.node", lambda: "Ash-MacBook.local")
    client = _FakeClient()
    out = resolve_definition_id(
        canonical_name="attention",
        expected_spec={"annotation_type": "moment"},
        fulcra_client=client, force_new=True,
    )
    assert out == "new-101"
    # Hostname suffix only the first dotted component:
    assert client.create_calls == [
        {"name": "attention (Ash-MacBook)", "annotation_type": "moment"}
    ]
```

- [ ] **Step 2: Verify tests fail**

Run: `uv run --package fulcra-common pytest packages/fulcra-common/tests/test_definitions.py -v -k resolve`
Expected: ImportError on `resolve_definition_id`.

- [ ] **Step 3: Implement `resolve_definition_id`**

Append to `packages/fulcra-common/fulcra_common/definitions.py`:

```python
import platform


def resolve_definition_id(
    *,
    canonical_name: str,
    expected_spec: dict,
    fulcra_client: Any,
    force_new: bool = False,
    machine_id: str | None = None,
) -> str:
    """Find an existing Fulcra definition with `canonical_name`, or
    create one. Returns the definition's id.

    `expected_spec` is the shape the plugin expects: at minimum an
    `annotation_type` key; for Duration annotations also a
    `measurement_spec` dict.

    `fulcra_client` exposes `list_definitions(name=...)` and
    `create_definition(name=..., **spec)`. It is injected (not built
    here) so tests can pass a fake and so the resolver itself never
    holds an HTTP connection.

    `force_new=True` always creates a new definition. The name carries
    `machine_id` (or `platform.node()`'s first dotted component) as a
    suffix so the new and existing defs are distinguishable in Fulcra.

    Raises `DefinitionSchemaMismatch` when an existing def with the
    same name has a different schema."""
    if force_new:
        suffix = machine_id or platform.node().split(".", 1)[0]
        new_name = f"{canonical_name} ({suffix})"
        return fulcra_client.create_definition(name=new_name, **expected_spec)["id"]

    candidates = fulcra_client.list_definitions(name=canonical_name)
    if not candidates:
        return fulcra_client.create_definition(name=canonical_name, **expected_spec)["id"]

    existing = candidates[0]
    if _spec_matches(existing, expected_spec):
        return existing["id"]
    raise DefinitionSchemaMismatch(canonical_name, existing, expected_spec)
```

- [ ] **Step 4: Verify tests pass**

Run: `uv run --package fulcra-common pytest packages/fulcra-common/tests/test_definitions.py -v`
Expected: 9 passed.

- [ ] **Step 5: Post-task branch check.**

- [ ] **Step 6: Commit**

```bash
git add packages/fulcra-common/fulcra_common/definitions.py packages/fulcra-common/tests/test_definitions.py
git commit -m "feat(common): resolve_definition_id — find-or-create with schema check

Adopt-by-canonical-name across machines for the typed-annotation
plugins. Returns the existing def's id when name + schema match,
creates one with the requested name + spec when not found, raises
DefinitionSchemaMismatch when name matches but schema doesn't.

force_new=True (used by the menubar's 'create separate stream'
toggle later) always creates and disambiguates the new name with
the machine's hostname. Default suffix uses platform.node() trimmed
to the first dotted component (so 'Ash-MacBook.local' → 'Ash-MacBook')."
```

---

## Task 3: `PluginState.definition_id` field + backwards-compat

**Files:**
- Modify: `packages/collect/fulcra_collect/state.py`
- Modify: `packages/collect/tests/test_state.py`

- [ ] **Step 0: Verify branch.**

- [ ] **Step 1: Write the failing tests**

Append to `packages/collect/tests/test_state.py`:

```python
def test_definition_id_round_trips(collect_home):
    from fulcra_collect import state
    st = state.load("attention")
    assert st.definition_id is None  # default
    st.definition_id = "fulcra-uuid-123"
    state.save(st)
    again = state.load("attention")
    assert again.definition_id == "fulcra-uuid-123"


def test_old_state_file_without_definition_id_loads_as_none(collect_home, tmp_path):
    import json
    from fulcra_collect import config as cfg, state
    # Hand-write an "old" state file with no definition_id key.
    path = cfg.config_dir() / "state" / "lastfm.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "plugin_id": "lastfm",
        "last_run": "2026-05-23T10:00:00+00:00",
        "last_outcome": "done",
        "last_error": None,
        "consecutive_failures": 0,
        "watermark": None,
        # NOTE: no definition_id key
    }
    path.write_text(json.dumps(payload), encoding="utf-8")

    loaded = state.load("lastfm")
    assert loaded.definition_id is None
    assert loaded.last_outcome == "done"  # rest of fields still load
```

- [ ] **Step 2: Verify they fail**

Run: `uv run pytest packages/collect/tests/test_state.py -k definition_id -v`
Expected: AttributeError on `definition_id`.

- [ ] **Step 3: Add the field and serialise it**

In `packages/collect/fulcra_collect/state.py`'s `PluginState` dataclass, add the new field at the bottom of the existing field list:

```python
@dataclass
class PluginState:
    plugin_id: str
    last_run: datetime | None = None
    last_outcome: str | None = None
    last_error: str | None = None
    consecutive_failures: int = 0
    watermark: str | None = None
    definition_id: str | None = None      # adopted-by-resolver Fulcra def id
```

In `load`, add the new field to the constructor call:

```python
        return PluginState(
            plugin_id=plugin_id,
            last_run=datetime.fromisoformat(lr) if lr else None,
            last_outcome=doc.get("last_outcome"),
            last_error=doc.get("last_error"),
            consecutive_failures=doc.get("consecutive_failures", 0),
            watermark=doc.get("watermark"),
            definition_id=doc.get("definition_id"),   # backwards compat: missing → None
        )
```

`save` uses `asdict(st)` already — the new field is included automatically.

- [ ] **Step 4: Verify tests pass**

Run: `uv run pytest packages/collect/tests/test_state.py -v`
Expected: all green, including the two new ones.

- [ ] **Step 5: Post-task branch check.**

- [ ] **Step 6: Commit**

```bash
git add packages/collect/fulcra_collect/state.py packages/collect/tests/test_state.py
git commit -m "feat(collect): PluginState.definition_id — cache for the multi-machine resolver

Per-plugin cache of the Fulcra annotation-definition id, populated
by RunContext.resolved_definition_id on first run. Cheaper than
re-querying Fulcra every dispatch; survives daemon restarts because
state.py already does atomic JSON persistence.

Backwards compatible: old state files with no definition_id key
load with the field set to None — the next run will resolve and
cache."
```

---

## Task 4: `Plugin.canonical_definition_name` + `RunContext.resolved_definition_id`

**Files:**
- Modify: `packages/collect/fulcra_collect/plugin.py`
- Modify or create: `packages/collect/tests/test_plugin.py`

- [ ] **Step 0: Verify branch.**

- [ ] **Step 1: Inspect the existing `RunContext` so the new helper fits**

Read `packages/collect/fulcra_collect/plugin.py` to see the current `RunContext` fields and how `fulcra_token()` (or its equivalent) is exposed. Then read any existing typed-annotation plugin (e.g. one of `fulcra_attention`, `fulcra_media_helpers/collect_plugins.py`'s `lastfm`) to see how the fulcra client is currently constructed from the token. The new helper must match that pattern — it is NOT this task's job to introduce a new client class.

If the existing pattern is unclear or there's no easy way to construct a client from a token without copy-pasting, **report BLOCKED with what you found and what you expected**. Don't guess.

- [ ] **Step 2: Write the failing tests**

`packages/collect/tests/test_plugin.py` (create if it doesn't exist; otherwise append):

```python
"""RunContext.resolved_definition_id tests.

The helper hides the resolver + state caching from plugin code. It is
exercised via a fake fulcra_client and a fresh PluginState — the
resolver itself is tested in fulcra-common."""
from __future__ import annotations

import logging
from datetime import datetime

import pytest

from fulcra_collect.plugin import Plugin, RunContext
from fulcra_collect.state import PluginState


class _FakeClient:
    def __init__(self):
        self.list_calls = 0
        self.create_calls = 0

    def list_definitions(self, *, name):
        self.list_calls += 1
        return []

    def create_definition(self, *, name, **spec):
        self.create_calls += 1
        return {"id": "def-fresh", "name": name, **spec}


def _make_ctx(state, client):
    return RunContext(
        plugin_id="lastfm",
        config={},
        credentials={},
        state=state,
        log=logging.getLogger("test"),
        _emit=lambda evt: None,
        _fulcra_client_factory=lambda: client,
    )


def test_resolved_definition_id_calls_resolver_when_state_empty():
    state = PluginState(plugin_id="lastfm")
    client = _FakeClient()
    ctx = _make_ctx(state, client)
    out = ctx.resolved_definition_id({"annotation_type": "moment"},
                                     canonical_name="lastfm-listens")
    assert out == "def-fresh"
    assert state.definition_id == "def-fresh"
    assert client.create_calls == 1


def test_resolved_definition_id_uses_cache_on_second_call():
    state = PluginState(plugin_id="lastfm", definition_id="cached-id")
    client = _FakeClient()
    ctx = _make_ctx(state, client)
    out = ctx.resolved_definition_id({"annotation_type": "moment"},
                                     canonical_name="lastfm-listens")
    assert out == "cached-id"
    assert client.list_calls == 0   # resolver was NOT called
    assert client.create_calls == 0


def test_canonical_definition_name_is_optional_on_plugin():
    # Plugins without a canonical name (e.g. dayone moments) must
    # still construct cleanly.
    p = Plugin(id="dayone", name="Day One", kind="manual", run=lambda c: None)
    assert p.canonical_definition_name is None


def test_canonical_definition_name_persists_when_set():
    p = Plugin(
        id="lastfm", name="Last.fm", kind="manual",
        run=lambda c: None,
        canonical_definition_name="lastfm-listens",
    )
    assert p.canonical_definition_name == "lastfm-listens"
```

- [ ] **Step 3: Verify tests fail**

Run: `uv run pytest packages/collect/tests/test_plugin.py -v`
Expected: AttributeError on `Plugin.canonical_definition_name` or `RunContext.resolved_definition_id`.

- [ ] **Step 4: Implement the field and the helper**

In `packages/collect/fulcra_collect/plugin.py`:

```python
# In the Plugin dataclass — add at the bottom of the field list:
    canonical_definition_name: str | None = None
```

```python
# In the RunContext dataclass — add a new field and the helper method.
# Field placement: alongside the existing _emit field.

from collections.abc import Callable

@dataclass
class RunContext:
    plugin_id: str
    config: dict
    credentials: dict[str, str]
    state: "object"
    log: logging.Logger
    _emit: Callable[[dict], None] = field(repr=False)
    _fulcra_client_factory: Callable[[], object] | None = field(default=None, repr=False)

    def progress(self, **fields: object) -> None:
        self._emit({"type": "progress", **fields})

    def resolved_definition_id(
        self, expected_spec: dict,
        *, canonical_name: str, force_new: bool = False,
    ) -> str:
        """Return the cached Fulcra definition id for this plugin, or
        call the resolver, cache, and return the freshly resolved id."""
        # Cached path: pure read, no client construction.
        cached = getattr(self.state, "definition_id", None)
        if cached and not force_new:
            return cached

        from fulcra_common.definitions import resolve_definition_id

        if self._fulcra_client_factory is None:
            raise RuntimeError(
                "RunContext has no fulcra_client_factory — the runner must "
                "supply one when the plugin uses resolved_definition_id."
            )
        client = self._fulcra_client_factory()
        new_id = resolve_definition_id(
            canonical_name=canonical_name,
            expected_spec=expected_spec,
            fulcra_client=client,
            force_new=force_new,
        )
        # Cache via setattr so PluginState (or a duck-typed substitute)
        # picks it up uniformly.
        self.state.definition_id = new_id
        return new_id
```

The `_fulcra_client_factory` plumbing lets the worker subprocess pass in
the client construction (which depends on the auth token and HTTP
client config it has set up) without `plugin.py` having to know how to
build one. Existing plugins that don't use `resolved_definition_id`
ignore the field — the optional factory means existing tests and
worker code don't break.

If your inspection in Step 1 surfaced that the worker doesn't have a
clean place to set this factory yet, **stop here and report BLOCKED** —
the runner/worker wiring is a separate concern that should be a
follow-up task rather than smuggled into this one.

- [ ] **Step 5: Verify tests pass**

Run: `uv run pytest packages/collect/tests/test_plugin.py -v`
Expected: 4 passed.

- [ ] **Step 6: Run the whole collect suite for regressions**

Run: `uv run pytest packages/collect/tests/ -q`
Expected: all green; existing tests unaffected by the optional new fields.

- [ ] **Step 7: Post-task branch check.**

- [ ] **Step 8: Commit**

```bash
git add packages/collect/fulcra_collect/plugin.py packages/collect/tests/test_plugin.py
git commit -m "feat(collect): Plugin.canonical_definition_name + RunContext.resolved_definition_id

Plugin-side surface for the multi-machine annotation-definition
resolver. A plugin declares its canonical_definition_name once;
its run(ctx) calls ctx.resolved_definition_id(spec,
canonical_name=...) and gets back the Fulcra def id — cached after
the first resolution on this machine.

The fulcra_client is injected via a factory the worker supplies, so
plugin.py stays untouched by HTTP/auth concerns. Plugins that don't
opt in (dayone moments etc.) keep the existing behaviour unchanged."
```

---

## Task 5: Retrofit `fulcra-attention`

**Files:**
- Modify: `packages/attention/fulcra_attention/` (the file that currently holds `_find_attention_definition` — find it first)
- Update related tests

- [ ] **Step 0: Verify branch.**

- [ ] **Step 1: Find the existing duplicate-avoidance code**

```bash
grep -rn "_find_attention_definition\|ensure_definitions" packages/attention/
```

Read the file(s) returned. Understand:
1. How the existing `_find_attention_definition` queries Fulcra and adopts.
2. Where the attention plugin's `Plugin(...)` is constructed (likely in `packages/attention/fulcra_attention/collect_plugin.py`).
3. How the worker hands the plugin a fulcra client today.

Capture in your report: (a) the canonical name to use for attention's definition (read it out of the existing fix — likely "attention"), (b) the `expected_spec` shape (look at how attention's definition is currently created — what `annotation_type` and `measurement_spec`).

- [ ] **Step 2: Write a regression-guard test**

In `packages/attention/tests/`, add a test that exercises the attention plugin's setup using the resolver path (mocking the resolver to return a known id) and confirms the plugin uses that id for its annotation writes. Specifically:

- Instantiate a `RunContext` with a `_fulcra_client_factory` that returns a fake.
- Run the plugin's setup code (or whatever entry-point creates the definition).
- Assert the plugin reads the id back through the resolver path, not via the old `_find_attention_definition`.

If `_find_attention_definition` is internal to the plugin's setup and not directly callable from a test, monkeypatch it to raise — so the test fails if the old code path is still reached.

- [ ] **Step 3: Verify the regression-guard fails before changes**

Run the attention tests. Expected: PASS in the existing world (old code path is fine) but the new monkeypatch-to-raise hook will be triggered after Step 4.

- [ ] **Step 4: Retrofit**

In the attention plugin's setup code:

1. Add `canonical_definition_name="attention"` to the `Plugin(...)` construction.
2. Replace the body of `_find_attention_definition` with a call to `ctx.resolved_definition_id(expected_spec=ATTENTION_SPEC, canonical_name="attention")`. If `_find_attention_definition` lives at module scope (not on a context), wire it through the plugin's `run(ctx)` so the ctx is available.
3. Remove `_find_attention_definition` entirely if it has no other callers. Don't leave a wrapper.

`ATTENTION_SPEC` is whatever shape the existing definition-creation code uses. Look at the file `git log` for the original `c99f702` / `0ec08f6` commits if you need to confirm the spec shape — but the file itself should already have it.

- [ ] **Step 5: Run tests**

Run: `uv run pytest packages/attention/tests/ -q`
Expected: all attention tests pass, including the regression guard.

- [ ] **Step 6: Post-task branch check.**

- [ ] **Step 7: Commit**

```bash
git add packages/attention/
git commit -m "refactor(attention): use the shared definition resolver

Replaces _find_attention_definition with ctx.resolved_definition_id
— the same multi-machine adopt-by-name behaviour, now sourced from
the generalised fulcra_common resolver. The attention plugin was
the only one with this protection; the generalisation lets the rest
of the typed-annotation plugins inherit it (next two commits do
lastfm and spotify-extended; remaining 10+ in a follow-on plan).

Behaviour-equivalent for the attention plugin; the test that asserted
on _find_attention_definition is replaced by one that asserts on the
resolver path."
```

---

## Task 6: Retrofit `lastfm` plugin

**Files:**
- Modify: `packages/media-helpers/fulcra_media/collect_plugins.py` (the lastfm plugin definition)
- Update lastfm-related tests in `packages/media-helpers/tests/`

- [ ] **Step 0: Verify branch.**

- [ ] **Step 1: Find lastfm's definition-creation code**

```bash
grep -n "LASTFM_PLUGIN\|lastfm" packages/media-helpers/fulcra_media/collect_plugins.py
```

Read the lastfm plugin's `Plugin(...)` construction and its underlying importer's definition-creation. lastfm is a scheduled plugin that writes Moment-style annotations (track plays). The canonical name should be `"lastfm-listens"` (matches the spec). The `expected_spec` is `{"annotation_type": "moment"}` — moments have no measurement_spec.

- [ ] **Step 2: Write the regression-guard test**

In `packages/media-helpers/tests/`, add a test that mocks the resolver to return a known id and confirms the lastfm plugin uses it.

- [ ] **Step 3: Retrofit**

1. Add `canonical_definition_name="lastfm-listens"` to the lastfm `Plugin(...)` construction.
2. In the lastfm importer code (the function `LASTFM_PLUGIN.run` ultimately calls), replace any "find or create definition" step with a call to `ctx.resolved_definition_id({"annotation_type": "moment"}, canonical_name="lastfm-listens")`.
3. Remove dead code from any old "create definition if missing" path that the resolver now subsumes.

- [ ] **Step 4: Run tests**

Run: `uv run pytest packages/media-helpers/tests/ -q -k "lastfm or LASTFM"`
Expected: all green.

- [ ] **Step 5: Post-task branch check.**

- [ ] **Step 6: Commit**

```bash
git add packages/media-helpers/
git commit -m "refactor(lastfm): use the shared definition resolver

Multi-machine coherence for the lastfm-listens annotation: a fresh
install on Mac 2 now adopts Mac 1's existing 'lastfm-listens'
definition instead of creating a duplicate row in Fulcra.

Existing single-machine setups are unaffected — the resolver finds
the user's existing 'lastfm-listens' def by name on first call and
caches the id."
```

---

## Task 7: Retrofit `spotify-extended` plugin

**Files:**
- Modify: `packages/media-helpers/fulcra_media/collect_plugins.py` (spotify-extended plugin)
- Update spotify-extended-related tests

- [ ] **Step 0: Verify branch.**

- [ ] **Step 1: Find spotify-extended's definition-creation code**

```bash
grep -n "spotify-extended\|spotify_extended\|SPOTIFY_EXTENDED" packages/media-helpers/
```

Inspect the plugin and its importer to determine canonical name and expected spec. spotify-extended writes Duration annotations for listening sessions (per the existing repo). Canonical name: `"spotify-extended-listens"` or similar — match what the existing def-creation code uses if it's already named.

- [ ] **Step 2: Write the regression-guard test** (same shape as lastfm's).

- [ ] **Step 3: Retrofit** (same shape as lastfm's).

- [ ] **Step 4: Run tests**

Run: `uv run pytest packages/media-helpers/tests/ -q -k "spotify"`
Expected: all green.

- [ ] **Step 5: Post-task branch check.**

- [ ] **Step 6: Commit**

```bash
git add packages/media-helpers/
git commit -m "refactor(spotify-extended): use the shared definition resolver

Same multi-machine adopt-by-name pattern as lastfm. spotify-extended
writes Duration annotations rather than Moments, so this commit also
exercises the resolver's measurement_spec comparison path on a real
plugin (lastfm and attention being Moment + Duration respectively
covers both modes by Task 7)."
```

---

## Task 8: `fulcra-collect plugin reset-definition` CLI + final verification

**Files:**
- Modify: `packages/collect/fulcra_collect/cli.py`
- Modify: `packages/collect/tests/test_cli.py` (or create)

- [ ] **Step 0: Verify branch.**

- [ ] **Step 1: Inspect the existing CLI structure**

Read `packages/collect/fulcra_collect/cli.py` to see the existing Click command groups (`plugin enable`, `plugin disable`, etc.). The new command lives under whatever sub-group those use.

- [ ] **Step 2: Write the failing test**

```python
def test_reset_definition_clears_cache(collect_home, monkeypatch):
    from click.testing import CliRunner
    from fulcra_collect import cli, state

    # Seed state with a cached definition_id
    st = state.PluginState(plugin_id="lastfm", definition_id="cached-uuid")
    state.save(st)

    runner = CliRunner()
    result = runner.invoke(cli.cli, ["plugin", "reset-definition", "lastfm"])
    assert result.exit_code == 0, result.output

    after = state.load("lastfm")
    assert after.definition_id is None
```

- [ ] **Step 3: Verify it fails**

Run: `uv run pytest packages/collect/tests/test_cli.py -k reset_definition -v`
Expected: FAIL — command doesn't exist.

- [ ] **Step 4: Add the command**

In `cli.py`, add to the existing `plugin` group:

```python
@plugin.command("reset-definition")
@click.argument("plugin_id")
def reset_definition(plugin_id: str) -> None:
    """Clear the cached Fulcra definition id for a plugin so the next
    run re-resolves (and possibly adopts a different definition)."""
    from . import state
    st = state.load(plugin_id)
    st.definition_id = None
    state.save(st)
    click.echo(f"Cleared definition_id cache for {plugin_id!r}.")
```

(If the existing CLI uses a different command-group decorator name, adapt — the structure is the same.)

- [ ] **Step 5: Verify tests pass**

Run: `uv run pytest packages/collect/tests/test_cli.py -v`
Expected: green.

- [ ] **Step 6: Run the full workspace test suite**

```bash
cd /Users/Scanning/Developer/fulcra-tools
uv run pytest -q
```

Expected: every package green — collect, fulcra-common, attention, media-helpers, csv-importer, dayone. Definition resolver work is additive; no regressions.

- [ ] **Step 7: Run `ruff` over the changed packages**

```bash
uv run ruff check packages/fulcra-common packages/collect packages/attention packages/media-helpers
```

Expected: clean.

- [ ] **Step 8: End-to-end discovery sanity**

```bash
uv run --package fulcra-collect python -c "
from fulcra_collect.registry import discover
r = discover()
print(len(r.plugins), 'plugins discovered;', len(r.errors), 'errors')
for pid in sorted(r.plugins):
    p = r.plugins[pid]
    print(' -', pid, '— canonical_definition_name =', repr(p.canonical_definition_name))
"
```

Expected: 17 plugins, 0 errors. Attention, lastfm, spotify-extended show their canonical names; the rest show `None` (will be retrofitted in the follow-on plan).

- [ ] **Step 9: Post-task branch check.**

- [ ] **Step 10: Commit**

```bash
git add packages/collect/fulcra_collect/cli.py packages/collect/tests/test_cli.py
git commit -m "feat(collect): fulcra-collect plugin reset-definition <id>

Clears the cached definition_id on a plugin's PluginState so the next
run re-resolves through fulcra_common.definitions. Useful when a user
wants to migrate a plugin from a stale definition to a fresh one, or
when DefinitionSchemaMismatch fired and they need to force a new
definition with the menubar's force_new toggle (once that ships).

Closes the multi-machine annotation-definition resolver workstream;
three plugins retrofitted (attention, lastfm, spotify-extended).
Remaining typed-annotation plugins (trakt, netflix, deezer,
letterboxd, goodreads, spotify-ifttt, apple-podcasts,
apple-podcasts-timemachine, generic-rss, generic-csv, youtube,
apple-takeout, media-webhook) are queued for a mechanical follow-on
plan that applies the same pattern."
```

---

## How it works (after this plan lands)

1. User installs any retrofitted typed-annotation plugin on Mac A. Plugin's `run(ctx)` calls `ctx.resolved_definition_id(spec, canonical_name="...")`. State is empty; resolver hits Fulcra, finds nothing, creates a definition, caches the id. Plugin writes annotations against that id.
2. User installs the same plugin on Mac B. Same call; state is empty on B; resolver hits Fulcra, finds Mac A's def by name, validates the spec matches, returns the existing id. State on B now caches it. Plugin on B writes against the same id. Fulcra row contains events from both machines.
3. Schema-mismatch case (very rare; user edited the def in Fulcra UI, or plugin's spec drifted): resolver raises `DefinitionSchemaMismatch`; runner records the failure with the existing/expected shapes in the error string. User can `fulcra-collect plugin reset-definition <id>` to drop the cache and retry (or, once the menubar ships, use the force-new toggle to create a disambiguated separate def).

## Self-Review

**Spec coverage:** every section of the spec maps to one or more tasks:
- Goal § (the resolver function) → Tasks 1, 2.
- Plugin contract changes (canonical_definition_name) → Task 4.
- PluginState (definition_id) → Task 3.
- RunContext.resolved_definition_id → Task 4.
- Retrofit list (attention, lastfm, spotify-extended) → Tasks 5, 6, 7.
- CLI reset-definition (open-questions §) → Task 8.
- Deferred 10+ plugins → noted in Task 8's commit message; follow-on plan.
- Out of scope (schema migration, state sync, menubar toggle, full retrofit) → preserved.

**Placeholder scan:** no TBD/TODO; every step ships actual code.

**Type consistency:** `resolve_definition_id` keyword args match between Task 2 (defn), Task 4 (RunContext call), Task 5/6/7 (plugin call). `definition_id` field name consistent across Tasks 3, 4, 8.
