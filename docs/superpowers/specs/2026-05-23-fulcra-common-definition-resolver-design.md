# fulcra-common annotation-definition resolver — design

**Date:** 2026-05-23
**Status:** Draft. Pending user review.

## Context

Every typed-annotation plugin in `fulcra-tools` creates its own Fulcra
annotation definition the first time it runs. With one user running the
same plugin on two or more machines, that means *N* machines = *N*
duplicate definitions for the same logical stream. Today only
`fulcra-attention` is protected — it has `_find_attention_definition`
that adopts an existing def by queryable shape. Every other
typed-annotation plugin (lastfm, spotify-extended, trakt, netflix,
deezer, letterboxd, goodreads, apple-podcasts, etc.) fragments
multi-machine data on first install.

This spec generalizes attention's pattern into a `fulcra-common` helper
that every typed-annotation plugin uses. Default: adopt-by-canonical-name
across all machines, so attention from a MacBook and a Mac mini land in
the same Fulcra row. Explicit opt-out: `force_new` for the rare case
where the user actually wants separate streams per machine — for which
the menubar's Preferences > Plugins pane will later expose a toggle.

## Goal

A `resolve_definition_id(canonical_name, expected_spec, *, fulcra_client,
force_new=False, machine_id=None) -> str` function in
`fulcra_common.definitions` that:

1. Queries Fulcra for existing annotation definitions matching
   `canonical_name`.
2. If found and the existing schema matches `expected_spec`, returns
   the existing def's ID.
3. If found but schema mismatches, raises `DefinitionSchemaMismatch`
   with both shapes so the caller can surface "definition X exists but
   with a different schema; create new?".
4. If not found, creates a new definition with the given name + spec,
   returns the new ID.
5. If `force_new=True`, always creates; appends a disambiguator
   (defaults to `platform.node()`) to keep the name unique.

The plugin contract gains one optional field, and the per-plugin state
file gains one optional column to cache the resolution.

## Plugin contract changes

In `fulcra_collect.plugin.Plugin`:

```python
canonical_definition_name: str | None = None
```

`None` means "this plugin manages its own definitions" — current
behaviour, kept for dayone-style per-event moments that don't share a
single definition.

In `fulcra_collect.state.PluginState`:

```python
definition_id: str | None = None
```

Cached after first resolution. JSON-serialised alongside the existing
fields; absent in older state files (backwards compatible — old state
files load with `definition_id=None`).

In `fulcra_collect.plugin.RunContext`:

```python
def resolved_definition_id(self, expected_spec: dict,
                            *, force_new: bool = False) -> str:
    """Returns the cached definition_id if set; else calls
    fulcra_common.definitions.resolve_definition_id, caches the
    result in this plugin's state, and returns it."""
```

This is the surface plugin code calls. The helper hides the resolver +
state plumbing from individual plugins.

## API: fulcra_common.definitions

```python
class DefinitionSchemaMismatch(RuntimeError):
    def __init__(self, name: str, existing: dict, expected: dict) -> None:
        self.name = name
        self.existing = existing
        self.expected = expected
        super().__init__(
            f"Fulcra definition {name!r} exists but its schema does not "
            f"match what the plugin expects; existing={existing}, "
            f"expected={expected}"
        )


def resolve_definition_id(
    *,
    canonical_name: str,
    expected_spec: dict,
    fulcra_client,
    force_new: bool = False,
    machine_id: str | None = None,
) -> str:
    """Find an existing Fulcra definition with `canonical_name`, or create
    one. Returns the definition's id (UUID string).

    `expected_spec` is the shape the plugin expects — for a
    DurationAnnotation it includes `annotation_type` and
    `measurement_spec`; for a MomentAnnotation just `annotation_type`.

    `fulcra_client` exposes `list_definitions(name=...)` and
    `create_definition(name=..., **spec)`.

    If `force_new=True`, a new definition is always created; the name is
    suffixed with `machine_id` (or `platform.node()` if not given) so
    the new and existing defs are distinguishable in Fulcra.

    Raises DefinitionSchemaMismatch when an existing def with the same
    name has a different schema — the caller (typically the menubar) is
    expected to either retry with force_new=True or change the canonical
    name."""
```

The internal `_spec_matches(existing, expected) -> bool` helper compares
`annotation_type` first, then `measurement_spec` if applicable.
MomentAnnotation has no measurement_spec — so the comparison is just
the annotation type. DurationAnnotation has both. The helper is pure;
unit-testable without a live Fulcra.

## Wire location

| File | Change |
|---|---|
| `packages/fulcra-common/fulcra_common/definitions.py` | **New.** Holds `resolve_definition_id`, `DefinitionSchemaMismatch`, `_spec_matches`. |
| `packages/fulcra-common/tests/test_definitions.py` | **New.** Tests against a fake fulcra_client. |
| `packages/collect/fulcra_collect/plugin.py` | Add `canonical_definition_name` to `Plugin`. Add `resolved_definition_id` to `RunContext`. |
| `packages/collect/fulcra_collect/state.py` | Add `definition_id: str | None = None` to `PluginState`. Update load/save. |
| `packages/collect/tests/test_state.py` | Test the new field round-trips and that old state files (no field) load with `None`. |
| `packages/collect/tests/test_plugin.py` | Test `RunContext.resolved_definition_id` calls resolver on first invocation and reads from cache on second. |

The fulcra_client argument is the existing module-level client that
plugins already use to write annotations (see `fulcra_common.wire`).
The resolver doesn't construct a client — it takes one as injection so
tests pass a fake.

## Retrofit in this plan

Three plugins are retrofitted to use the new resolver. The pattern is
identical for each; once these three are green, the same change applies
mechanically to the remaining 10+ plugins (queued as a follow-on plan,
not in this scope).

1. **`fulcra-attention`** — replace `_find_attention_definition` with
   `ctx.resolved_definition_id(expected_spec=ATTENTION_SPEC)`. The
   `canonical_definition_name="attention"` lives on the plugin. The
   existing duplicate-on-machine-2 behaviour was the original motivation
   for this whole spec; this retrofit validates that the new pattern
   reproduces the existing fix.

2. **`lastfm`** — the simplest scheduled plugin in
   `packages/media-helpers/fulcra_media/collect_plugins.py`. Declare
   `canonical_definition_name="lastfm-listens"`, call
   `ctx.resolved_definition_id(...)` once at the top of its run, use
   the returned id for the annotations it writes.

3. **`spotify-extended`** — slightly more complex than lastfm (different
   measurement_spec shape) but the same pattern. Validates the
   resolver works for a second non-trivial plugin and that the
   `_spec_matches` helper isn't accidentally specific to attention/lastfm
   shapes.

## Deferred (follow-on plan)

trakt, netflix, deezer, letterboxd, goodreads, spotify-ifttt,
apple-podcasts, apple-podcasts-timemachine, generic-rss, generic-csv,
youtube, apple-takeout, media-webhook. Each is a mechanical retrofit —
declare a `canonical_definition_name`, switch the def-creation call to
`ctx.resolved_definition_id(...)`. Bundled into one follow-on plan
once this proof-of-concept is approved.

## How it works (end-to-end)

1. User installs `fulcra-attention` plugin on Mac A (the first machine).
2. Plugin's `run(ctx)` calls `ctx.resolved_definition_id(expected_spec=ATTENTION_SPEC)`.
3. State has no cached `definition_id`. RunContext calls
   `fulcra_common.definitions.resolve_definition_id(canonical_name="attention", ...)`.
4. Fulcra query returns no existing def. Resolver creates one,
   returns its id. RunContext caches it in `state.definition_id`.
5. Plugin writes its annotations against that id.
6. User installs `fulcra-attention` on Mac B (second machine).
7. Same call path. State on B has no cached id (fresh machine).
   Resolver queries Fulcra, finds the existing "attention" def from
   Mac A. `_spec_matches` returns True. Returns the existing id.
   RunContext caches it on B.
8. Plugin on B writes annotations against the same id. The Fulcra row
   contains both A's and B's events.

The schema-mismatch case (rare) fires if the user manually edited the
Fulcra definition (e.g. changed annotation_type), or if a plugin's
spec evolved between versions. The menubar surfaces this; for now the
CLI surfaces it as a plain exception with the two shapes printed.

## Error handling

- `DefinitionSchemaMismatch` raised by the resolver propagates up
  through `resolved_definition_id` into the plugin's `run(ctx)`.
  The runner records the run as failed with the exception message,
  the menubar shows "X: definition mismatch" with the option to retry
  with force_new. For v1 (no menubar UI yet), the user can clear
  `state.definition_id` and re-set the canonical name to recover.
- Network errors from the fulcra_client are NOT caught here — the
  resolver lets them propagate so the runner's existing retry/backoff
  policy applies the same way it does to annotation writes.
- The resolver does not currently support migrating an existing def's
  schema (changing measurement_spec on a live def). That is a
  Fulcra-side problem and is out of scope.

## Testing

- `test_definitions.py` covers:
  - Not-found → creates, returns new id.
  - Found + matching spec → returns existing id (no create call).
  - Found + mismatched spec → raises `DefinitionSchemaMismatch`.
  - `force_new=True` → always creates; name carries the machine_id
    suffix.
  - `force_new=True` without machine_id → suffix uses `platform.node()`.
  - `_spec_matches` for Moment (annotation_type only), Duration
    (annotation_type + measurement_spec), and a mixed-shape case.
- `test_state.py` extension: `definition_id` round-trips via save+load,
  and old state files (created before this field existed) load with
  `definition_id=None`.
- `test_plugin.py` extension: `RunContext.resolved_definition_id`
  hits the resolver once, cached on second call.
- Each retrofitted plugin's existing tests stay green. The `attention`
  retrofit may need an additional test that mocks the resolver to
  confirm `_find_attention_definition` is no longer called.

## Out of scope

- Definition schema migration (Fulcra-side).
- Multi-machine state synchronisation (state stays local; multi-machine
  coherence happens at the definition layer only).
- The menubar's "force new definition" toggle and the schema-mismatch
  recovery UI — those land in a small follow-on patch to the menubar
  Preferences spec (`2026-05-22-fulcra-collect-menubar-design.md`).
- Retrofitting the other 10+ typed-annotation plugins (follow-on plan).

## Open questions

- **`machine_id` default**: `platform.node()` is a reasonable default
  but on macOS the hostname can be ugly (`Ash-Mac-mini.local`). For
  the disambiguation suffix, prefer the shorter of `platform.node()`
  or `scutil --get ComputerName`. Recommendation: use
  `platform.node().split(".", 1)[0]`; revisit if the menubar grows a
  per-machine "label" config later.
- **In-memory cache in the daemon**: not adding one. PluginState is the
  cache. If a plugin's state is somehow lost mid-run the resolver fires
  again and that's correct.
- **`fulcra-collect plugin reset-definition <id>` CLI**: yes, useful.
  Adds one tiny Click command that clears `state.definition_id`. Plan
  task includes it.
