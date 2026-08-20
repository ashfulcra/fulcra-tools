# Coord Engine v2.0.0 Detection Authority Design

**Status:** Proposed for exact-head review.

**Owner:** codex-coder

**Required reviewer through 2026-08-23:** coord-boss

**Objective:** Replace independent, budget-bounded discovery scans with one
truthful change-detection authority. `data-updates` detects change; folds become
materialized views. Version 2.0.0 is complete only when released, adopted by the
fleet, and verified from at least two credentialed hosts.

## 1. Problem

Coord Engine currently lets several commands independently enumerate remote
state. Each command can exhaust a different budget, observe a different rebuild
instant, and invent its own degraded vocabulary. That has produced every unsafe
combination in the incident matrix:

- a budget cut reported as a complete result with `rc=0`;
- an absent or truncated transport envelope treated as empty;
- body text containing `UNKNOWN` while the process exits zero;
- a renderer claiming a check ran when it was not run;
- concurrent identities on one host contesting themselves because host identity
  outranked the session override;
- empty, tombstoned, and unreadable directories collapsed into one classifier.

Retrying is not a correctness mechanism. Mid-rebuild observations may
self-converge, but every intermediate observation still has to describe itself
truthfully and exit nonzero when any required fact is unknown.

## 2. Architectural Decision

### 2.1 Chosen approach: one change batch, immutable generations

Every reconciliation pass obtains exactly one normalized `ChangeBatch` from the
Fulcra `data-updates` feed. All view builders consume that same batch and the
same prior published generation. A pass writes immutable generation content
first and advances the small current-generation manifest only after every
required section proves complete.

Public reads consume the current generation plus one feed freshness overlay
from the generation watermark to now. If either read is incomplete, malformed,
unbounded, or contradictory, the command returns `UNKNOWN` and exits nonzero.
The overlay makes a stale or out-of-order manifest write fail closed instead of
silently serving an older view.

This approach keeps the file store's lack of compare-and-swap explicit. It does
not pretend that a mutable pointer is atomic. Immutable generations preserve
evidence; the feed watermark proves whether a pointer is current enough to use.

### 2.2 Rejected alternatives

1. **Keep independent scans and enlarge budgets.** This reduces incident
   frequency but preserves conflicting snapshots, per-fold vocabulary, and
   false completeness.
2. **Give every fold its own feed cursor.** This makes individual folds cheaper
   but allows tasks, reviews, roles, and forge state to describe different
   worlds. It also multiplies cursor recovery and coverage logic.
3. **Treat a mutable aggregate as the authority.** The transport has no proven
   CAS, so an older concurrent writer can replace a newer aggregate. A view can
   accelerate reads but cannot own the fact.

## 3. Core Types and States

The engine MUST use typed outcomes internally. Renderers and exit-code selection
consume these types; they do not infer health by searching prose.

```python
class SurfaceState(Enum):
    NOT_RUN = "not-run"
    CLEAR = "clear"
    DATA = "data"
    UNKNOWN = "unknown"

@dataclass(frozen=True)
class Coverage:
    namespace: str
    state: SurfaceState
    observed: int
    expected: int | None
    reason: str | None

@dataclass(frozen=True)
class ChangeBatch:
    cursor_before: str | None
    cursor_after: str
    high_watermark: str
    updates: tuple[NormalizedUpdate, ...]
    coverage: tuple[Coverage, ...]
    state: SurfaceState

@dataclass(frozen=True)
class CommandOutcome:
    state: SurfaceState
    rows: tuple[dict, ...]
    coverage: tuple[Coverage, ...]
    warnings: tuple[str, ...]
```

Rules:

- `NOT_RUN` is distinct from `UNKNOWN`.
- A budget cut, timeout, parse doubt, missing envelope, permission denial, or
  incomplete namespace makes the affected coverage `UNKNOWN`.
- `CLEAR` means the relevant namespace was positively checked and contained no
  matching data.
- A command exits zero only when its required coverage contains no `UNKNOWN`.
- Human and JSON renderers MUST be projections of the same `CommandOutcome`.

## 4. Change Detection

### 4.1 One feed read

`ChangeDetector.poll(team, prior_watermark, deadline) -> ChangeBatch` is the only
ordinary change detector. It performs one bounded `data-updates` query, validates
the response envelope before consuming rows, normalizes paths and lifecycle
timestamps, deduplicates by immutable update identity, and sorts deterministically.

The detector assigns every recognized update to a namespace:

- tasks and directives;
- review registers, verdicts, and settled markers;
- forge mirror and feedback;
- presence and roles;
- acknowledgments and responses;
- projection metadata;
- unknown or unsupported paths.

Coverage describes what was measured, not merely how far a loop advanced.
Budgets cap work and report partial coverage; they never create synthetic domain
rows such as “unreadable review” inside a task list.

### 4.2 Bootstrap and recovery

A full store scan is a named recovery mode, not an implicit fallback that can
claim normal completeness. It runs when there is no trusted watermark, the feed
shape is invalid, or canonical evidence predates retained feed history.

Recovery writes progress under an immutable build id. It may resume, but it MUST
NOT publish the current-generation manifest until every required namespace is
complete. A recovery budget cut returns `UNKNOWN` and nonzero while preserving
progress for the next pass.

## 5. View Building and Publication

Each view builder is pure with respect to discovery:

```text
build(prior_section, change_batch, canonical_reader, deadline)
    -> SectionResult(section, coverage, state)
```

The canonical reader may fetch documents named by the batch. It may not list a
namespace to discover additional work during an ordinary pass. A missing named
document is classified using update lifecycle evidence:

- a proven delete/tombstone is absence;
- a positively empty directory is empty;
- an unreadable or ambiguous path is `UNKNOWN`;
- an unrecognized shape is `UNKNOWN`, never a domain row.

Publication is two phase:

1. Write and read-verify
   `_coord/projections/generations/<generation-id>.json`. The generation id is a
   digest of the prior generation id, feed watermark, normalized update digest,
   schema version, and engine version.
2. Write and read-verify `_coord/projections/current.json` containing only the
   generation id, source watermark, schemas, engine version, and content digest.

The manifest advances only if tasks, reviews, forge, roles, presence,
acknowledgments, and responses all prove their required coverage. Incomplete
section results remain build progress and are never published as current.

Concurrent writers converge when they consume identical inputs because the
generation id and bytes are deterministic. Writers with different watermarks may
race on the manifest; every reader therefore performs the freshness overlay in
section 6 and rejects a manifest whose watermark does not cover observed
canonical changes.

## 6. Public Read Contract

Every public fold follows one path:

1. read and validate the current manifest;
2. read and digest-verify its immutable generation;
3. obtain one bounded `data-updates` overlay after the generation watermark;
4. either apply the supported delta or return `UNKNOWN` with a recovery action;
5. render `CommandOutcome` and derive the exit status from its typed coverage.

Commands MUST NOT return a clean projection when the overlay was not run. JSON
includes `state`, `coverage`, `generation`, and `watermark`. Text output names the
same unknown surfaces without turning diagnostics into work rows.

The invariant is mechanical: if JSON or text says any required surface is
`UNKNOWN`, the exit code is nonzero. Tests assert the structured outcome and the
process exit together.

## 7. Identity and Ownership

Identity precedence is:

```text
explicit command argument > FULCRA_COORD_AGENT > persisted workspace identity
> sanitized host fallback
```

The host is attribution, not session identity. Two agents on one host therefore
cannot contest a role merely because the hostname is shared. Every generation
records builder agent, host, engine version, and session correlation id without
placing secrets in artifacts.

## 8. Compatibility and Versioning

This architecture is a major version because projection schemas, output
envelopes, and truthfulness guarantees change together.

- package and engine version: `2.0.0`;
- existing canonical task, review, verdict, role, presence, response, and forge
  documents remain readable and authoritative;
- v1 projection aggregates remain readable only as bootstrap input and are never
  advertised as v2 current generations;
- mixed-fleet writers may continue writing canonical documents;
- v1 readers MUST NOT be pointed at v2-only projection authority until the fleet
  pin proves the minimum reader version;
- cursor schema 2 remains disabled without proven CAS.

`AGENTS.md`, the coord-engine README, output-contract documentation,
`adopt-latest.sh`, and fleet-pin instructions are ship-gate artifacts.

## 9. Implementation Units

Each unit gets a separate exact-head review request naming the invariant it
enforces. Through 2026-08-23, coord-boss is the required reviewer and
codex-reviewer is optional.

1. **Outcome spine:** typed states, coverage, rc-matches-body, and renderer parity.
2. **Identity and classifier:** env-over-host precedence; empty, tombstone, and
   unreadable are distinct.
3. **Change detector:** one normalized feed batch with explicit namespace
   coverage and recovery signaling.
4. **Generation builder:** deterministic sections, immutable generation writes,
   publication fence, resumable recovery progress.
5. **Read overlay:** generation validation, freshness feed check, supported delta
   application, fail-closed unknown.
6. **Migration and release:** v1 bootstrap, version bump, docs, pin, installer,
   adoption, and fleet verification.

The already reviewed projection-publication fence and queue truthfulness changes
are inputs to these units. They are not evidence that v2 is complete.

## 10. Acceptance Matrix

The suite MUST pin every previously observed failure flavor:

| Case | Required result |
|---|---|
| Feed budget expires after partial rows | coverage `UNKNOWN`, no publish, nonzero |
| Transport returns no envelope | `UNKNOWN`, prior cursor preserved, nonzero |
| Body contains an unknown required surface | nonzero in JSON and text modes |
| Surface was not invoked | `NOT_RUN`, never “checked” or “clear” |
| Concurrent same-host sessions use different env identities | no self-contest |
| Empty directory | positively empty, no diagnostic work row |
| Tombstoned document | absent only with lifecycle evidence |
| Unreadable directory or document | `UNKNOWN`, nonzero |
| Rebuild stops after immutable generation write | old manifest remains current |
| Older builder overwrites manifest | freshness overlay rejects stale watermark |
| Two builders consume identical batch | identical generation id and bytes |
| Projection row contradicts canonical changed doc | overlay applies delta or returns `UNKNOWN` |
| Renderer truncates rows | coverage and exit status remain truthful |

Focused tests cover each row. The full coord-engine suite must remain green.

## 11. Release and Fleet Completion

“Done” requires all of the following:

1. every implementation unit approved at its exact head and merged;
2. coord-engine `2.0.0` released;
3. fleet pin and `adopt-latest.sh` moved to `2.0.0`;
4. every live host reports adoption of the exact release;
5. at least two credentialed hosts run queue, needs-me, review, forge, roles,
   presence, and reconcile verification;
6. the acceptance matrix passes against both synthetic failure fixtures and the
   live store;
7. any live `UNKNOWN` produces nonzero status and no silent-clear wake.

Only then may the parked usage-visibility research resume unless Ash explicitly
resequences it sooner.
