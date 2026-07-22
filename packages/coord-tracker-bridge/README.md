# coord-tracker-bridge

`coord-tracker-bridge` mirrors coord work into an external tracker without
making that tracker authoritative. Its provider-neutral core defines normalized
source snapshots, a complete-identity state ledger, a versioned projection
policy, and a deterministic diff plan. Phase 2 adds a `coord-engine --json`
source adapter, a Linear GraphQL adapter, and explicit operator-controlled run
phases. Phase 3 adds a lower-fidelity, read-only `teams` source that reads only
typed task documents under `team/<team>/task/` and never depends on derived
coord-engine views.

The package fixes the unsafe shortcuts in the original Linear probe:

- identity is `(provider, namespace, item_id)`, never a title marker or short
  suffix;
- snapshots distinguish complete, unsupported, and degraded capabilities;
- destructive closes are suppressed only for the incomplete capability scope;
- policies declare field ownership and a bounded managed-label taxonomy;
- planning is diff-before-mutate and deterministic, so adapters can retry a
  partially executed run until it converges.
- issues, labels, projects, per-issue labels, comments, and inbound events are
  paginated; rate-limit retries use bounded exponential backoff;
- a singleton lease covers each `(source, tracker, policy)` run;
- full source identity is stored in the ledger and provider metadata, so a
  create that succeeds just before a ledger-write crash is rediscovered;
- policy v2 is an explicit lane allowlist: omission means exclusion, and the
  bundled operator surface is only `active`, `blocked`, derived `backlog`,
  `asks`, and `threads-missed`;
- the one-time `adopt-markers` phase migrates v0.25 `[bus:xxxxxxxx]` issues to
  full provider metadata plus the ledger before ordinary sync can duplicate
  them.

## Core contract

Source adapters produce a `Snapshot` containing `items`, `complete`,
`diagnostics`, `capabilities`, and `observed_at`. Tracker adapters normalize
managed records. `build_plan()` compares those inputs with `BridgeLedger` and a
versioned `Policy`, returning semantic create/update/reopen/close changes for an
adapter to execute.

```python
from coord_tracker_bridge import BridgeLedger, build_plan, load_policy

plan = build_plan(snapshot, tracker_records, BridgeLedger.load("state.json"), load_policy())
```

The policy bundled at `coord_tracker_bridge/policies/default-v2.json` is an
explicit allowlist. A lane absent from `included_lanes` is excluded; there is
no fallback that projects its raw status. The bundled surface contains only
`active`, `blocked`, `backlog`, `asks`, and `threads-missed`. Engine task rows
in `proposed` or `waiting` with `assignee: @backlog` derive to `backlog`;
ordinary proposed/waiting rows remain excluded. Asks and dropped-thread rows
derive to `asks` and `threads-missed`. A managed item that positively moves
outside the allowlist is closed. Command intake and expectation evaluation
remain disabled and out of scope.

The engine source accepts both one JSON document and JSONL output from
`coord-engine --json` folds; `threads` currently uses JSONL. Valid JSONL rows
survive an interleaved prose degraded-marker line, while the line text is
bounded into diagnostics and the affected capability remains degraded, so the
partial read cannot authorize absence-based closes. Embedded degraded
markers fail the affected capability closed and diagnostics name their exact
JSON path, marker type, and reason instead of emitting an anonymous “degraded
row.” Schema-invalid rows likewise degrade their capability—even when other
rows are usable—so a partial enumeration can never authorize closes. Ordinary
engine folds are bounded at 180 seconds. Fleet health is a
known slower aggregate and has its own configurable adapter bound, 360 seconds
by default (`EngineSourceAdapter(..., health_timeout=...)`). Its JSON view is
an object; each entry in `hosts` becomes a health record keyed by the stable
`host` value, while an invalid hosts collection degrades health fail-closed.

## Run phases

Set `LINEAR_API_KEY` and either `LINEAR_TEAM_ID` or `--linear-team-id`. Then use
the phases in order:

```bash
coord-tracker-bridge plan --coord-team fulcra
coord-tracker-bridge adopt-markers --dry-run --coord-team fulcra
coord-tracker-bridge adopt-markers --coord-team fulcra
coord-tracker-bridge apply-resources --coord-team fulcra
coord-tracker-bridge sync --coord-team fulcra
```

Run `adopt-markers --dry-run` first and inspect every provider/source mapping.
The preview reads Linear and coord source state but writes neither Linear nor
the ledger. It exercises the full adoption resolver, including archived task
lookups; those lookups are batched concurrently because remote archives can be
slow.

**`adopt-markers` without `--dry-run` is MUTATING.** It strips title markers,
writes provider metadata, and persists ledger entries. Run it once before the
first package-managed sync only after the dry-run mapping is approved and only
when the Linear team contains v0.25 title markers. The authoritative mapping
is the bridge-owned description footer ``bus slug: `<full-slug>` ``; the title
marker is only a consistency cross-check against the slug's final eight
characters, which are not necessarily hexadecimal. Every marked issue must
contain exactly
one footer naming exactly one source row, and every full slug must be unique.
Rows excluded from the hot projection are eligible for identity adoption;
terminal task slugs are resolved by an exact archived search, then the normal
completeness-gated plan closes them. Missing footers, marker mismatches, unknown
or ambiguous archived lookups, collisions, or identity conflicts abort before
mutations. Each successful issue update strips the title marker, writes full
source identity and capability metadata, then atomically persists the ledger
entry. A crash between the provider update and ledger write converges on retry
from provider metadata. Re-run `plan` afterward; for a workspace not yet cut
over, hold cutover until the plan's create set matches the approved projection
surface. (The `fulcra` team's cutover completed 2026-07-21 — first live sync
applied 59 changes — so this hold applies only to onboarding a NEW
workspace/team, not to routine syncs.)

Use `--source teams` to read the strict base-teams convention directly. The
teams source requires `type: Task`, an explicit stable `id`, a title, a valid
status, and typed tags in every task document. `index.md` and `log.md` are
ignored as derived artifacts. Any ambiguous listing, read, parse, duplicate ID,
or unexpected entry degrades the task capability and suppresses absence-based
closes. Asks, threads, health, due dates, expectations, and command intake are
reported as `UNSUPPORTED`; they are never represented as clean empty results.
Colliding stable IDs remove every colliding record from the snapshot, and task
downloads run concurrently under one whole-snapshot deadline (30 seconds by
default); an incomplete batch degrades tasks instead of authorizing mutations
from a partial enumeration.

- `plan` is read-only and shows projection changes plus missing bounded
  taxonomy resources.
- `adopt-markers --dry-run` previews the complete legacy identity mapping and
  performs no provider or ledger writes.
- `adopt-markers` is the explicit **mutating** one-time migration for legacy
  Linear issues; ordinary `sync` never infers identity from a title.
- `apply-resources` is the only phase that creates labels or projects.
- `sync` refuses a non-empty resource plan; it never silently creates resources.
  It also refuses an overlapping run holding the same source/tracker/policy
  lease.

State defaults to `~/.local/state/coord-tracker-bridge`. Secrets are environment
references only. GraphQL failures never log variables or source content.

## Test

```bash
uv run --package coord-tracker-bridge --extra dev --no-editable pytest packages/coord-tracker-bridge/tests -q
```
