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

Set a Linear credential and either `LINEAR_TEAM_ID` or `--linear-team-id`. Then
use the phases in order:

The credential is resolved from the first of these variables that is set:

| Order | Variable | Notes |
| --- | --- | --- |
| 1 | `LINEAR_PERSONAL_KEY` | personal API key (`lin_api_…`), sent as-is |
| 2 | `LINEAR_PERSONAL_KEY_2` | spare personal key |
| 3 | `COORD_BRIDGE_DEVELOPER_TOKEN` | OAuth app token, acts as the app, not you |
| 4 | `LINEAR_API_KEY` | historical name; kept working, no longer preferred |

`LINEAR_KEY_ENV=<variable name>` overrides the order and uses exactly that one.

The order is not cosmetic. This bridge originally read `LINEAR_API_KEY` and
nothing else; when that one credential stopped authenticating, the projection
went stale for a month while three working credentials sat unused in the same
environment, and the only symptom was `http_status=401`. A 401 now also prints
which variable it used and which others were present, so the next failure is
diagnosable from its own output.

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

## `linear-inbox` — read Ash's board, never touch it

> **STATUS: VERIFIED against the live API, 2026-08-19.** First live read
> rendered 124 issues from team BUS at rc 0. The fail-closed path proved itself
> first and by accident: an expired token produced `UNKNOWN — this is not an
> empty board`, rc 3, exactly as designed — the verb refused to report an empty
> board for an authentication failure. `tests/fixtures/real_linear_issues.json`
> is the stamped capture from that read (100 nodes, payload fields redacted),
> and the field-name contract test runs against it rather than being skipped.
>
> Nine review rounds found seven real defects before it ever met the API. What
> "reviewed" bought was that the first live read worked; what "verified" adds is
> that the shapes it was reasoned about are the shapes Linear actually sends.


`coord-tracker-bridge linear-inbox --linear-team-id <TEAM>` performs one
paginated GraphQL read of a Linear team's issues and prints them as a coord
fold. It is the only verb that runs in the read direction, and it is fenced:

- It builds **no** `BridgeService` — no ledger, no lease, no tracker adapter —
  so there is no write path in scope to reach.
- Its client is wrapped in `ReadOnlyTransport`, which inspects the GraphQL
  document about to be posted and refuses anything that is not a pure query.
  The rail runs on what will execute, not on what the caller intended.
- Node **cardinality is preserved**: the verb walks pages itself rather than
  through `LinearClient.paginate`, which silently filters non-Mapping nodes —
  harmless for a mirror that skips what it cannot project, fatal for a verb
  promising never to render a partial board as a whole one. A `null` in a page
  used to arrive as a clean empty board.
- **Absent may default only when the default ASSERTS NOTHING.** No labels
  asserts nothing; no assignee asserts nothing. But absent pagination metadata
  would be read as "this is the last page" — a claim of completeness, which is
  the one claim this verb exists never to fake. A terminal page must be stated
  (`hasNextPage: false`), never inferred from silence.
- **The invariant: every value read is either ABSENT WITH A DEFAULT or
  VALIDATED WHOLE.** There is no third state, and "present but unusable" is
  never quietly promoted into one of the first two. It holds at four scopes —
  the node list, top-level scalars, optional sub-objects and the fields inside
  them, and the pagination metadata and the fields inside THAT — because it was
  broken at each one in turn across six review rounds. Fixing a scope's shape is
  not the same as fixing its contents: `pageInfo` was corrected once and the
  round that corrected it is what made its internals invisible for four more.
  Watch fallbacks especially: `identifier or id` used to mask a present-but-
  malformed identifier, so a row we could not identify rendered as one we could.
- **Absent has a default; malformed never does — including inside an object.**
  A present-but-hollow `state` or `assignee` is malformed, not absent, so it
  degrades the row rather than rendering as "unknown" or "unassigned". Which
  inner fields are required lives in one table, `_REQUIRED_SUBFIELDS`, pinned by
  a test against the query itself so a field added to the selection cannot end
  up validated by nobody. A sub-object that is missing
  (no labels, no assignee, no state) reads as its natural default. A sub-object
  that is *present and the wrong shape* degrades the row, and one bad row
  degrades the read. Coercing malformed to empty renders a confident answer
  about data we could not read — a row missing labels it never mentions is the
  same lie as an empty board, one level down.
- A failed or partial read is **UNKNOWN and exits 3**, never an empty board.
  A caller scripting this verb must be able to tell "no work" from "could not
  read", and rc 0 during an outage would report the first while meaning the
  second.

**WRITES NEED A BOT ACTOR, NOT JUST A KEY** (Ash, 2026-08-19, binding). The
original setup used an OAuth *bot* token deliberately, so Linear actions are not
attributed to Ash personally. A personal API key is fine for READS — nothing is
attributed — and that is what this verb uses. Any future write plan requires the
refreshed bot-actor OAuth setup first. This is a design constraint, not a
preference: shipping writes on a personal key would silently rewrite the
authorship of every action on the board.

The standing rail on this lane: **zero Linear writes of any kind** — no issue
creation, no state changes, no comments, no label/assignee mutations — until
Ash approves a write plan explicitly. The reason is a near-miss, not caution: an
earlier cutover plan would have pushed ~503 creates into a 55-issue curated
board.

`tools/capture_inbox.py` stamps a real response with its own measured
provenance for the field-name contract test, redacting titles, descriptions,
URLs and assignee names. It has no offline mode: a hand-written fixture
labelled "real" is the defect it exists to prevent.

## `linear-assignments` — route board changes to the fleet, still never write

Phase 1 of the Linear integration design (`_coord/agents/coord-boss/reports/
2026-08-19-linear-integration-design.md`, approved by Ash 2026-08-19).
`coord-tracker-bridge linear-assignments --linear-team-id <TEAM>` reads the
board, works out which cards had their **assignee or state** change since a
durable watermark, and turns each real change into a durable coord directive.
Phases 2 and 3 — the one-time board reconcile and the two-tier projection — are
separately gated on Ash GO'ing a printed plan plus a bot-actor token, and
nothing in this verb anticipates them.

It reaches Linear only through `linear-inbox`'s read path, `ReadOnlyTransport`
and all, and builds no `BridgeService`: **zero Linear writes**, by construction
rather than by intent.

- **It re-reads the whole board rather than filtering server-side.** A second
  query shape would be a second place for a partial board to be reported as a
  whole one, and `fetch_inbox` is the read path that carries the completeness
  contract. The watermark is applied after the rows have been read faithfully.
- **A watermark selects candidates, not changes.** Linear bumps `updatedAt` for
  any edit, so routing on the watermark alone would dispatch a directive every
  time Ash fixes a typo — and the design names noise as a defect in its own
  right. Durable state remembers the `(assignee, state)` pair last observed per
  card; only a pair that actually differs is routed.
- **`updatedAt` is optional in `linear-inbox` and required here**, which is what
  load-bearing means. Neither default asserts nothing: called old, the row is
  silently never delivered; called new, it is delivered on every run forever. So
  a row that cannot be placed in time is UNKNOWN for the whole pass. Same for a
  card whose workflow state was never read — a Linear state may legitimately be
  *named* "Unknown", so `InboxItem.state_present` now carries whether the value
  is a reading or the placeholder.
- **No delivery is ever guessed.** Assignee display names resolve through the
  nickname roster in the coord store. A name that is absent from it, resolves to
  more than one identity, or names an external mesh peer — which the roster
  states is not reachable via `coord-engine tell` — goes to the coordinator for
  triage. A roster that fails to load is **UNKNOWN, not "nobody resolves"**: the
  second files a confident triage verdict on every card in Ash's board on the
  strength of a failed read.
- **Preview is the default.** `--deliver` is both the flag that dispatches and
  the flag that advances the watermark, so a run that shows you the plan can
  never consume it. A **cold start refuses to deliver at all** — with no
  baseline every card reads as a change, which is the ~503-creates shape again
  with the fleet bus as the target — and `--seed` adopts the board as the
  baseline without sending anything. A run that would exceed `--delivery-cap`
  (default 25) refuses whole rather than flooding partway.
- **The watermark may repeat; it may never skip.** Delivery is at-least-once,
  and a repeat is announced *inside the directive* rather than suppressed —
  a silent duplicate is indistinguishable from a second real assignment. On a
  dispatch failure the mark stops at the failing row, so everything that did not
  go out is still owed on the next pass.
- **A dispatch has three outcomes, not two.** `coord-engine tell` can commit the
  directive and then fail to report it, so a raise is not evidence that nothing
  was written — this package's own invariant with the labels swapped, and the
  defect codex-coder found in the first cut. The attempt is written to disk
  *before* the transport runs, and a retry whose fingerprint is still marked
  says **POSSIBLE RE-DELIVERY**: not "new", which under-claims, and not
  "repeat", which over-claims. A confirmed success is what clears the marker.
- Exit codes extend the `linear-inbox` contract by one: **0** succeeded, **3**
  UNKNOWN (proves nothing — never "no assignments changed"), **2** a deliberate
  refusal.

Directives go out via `coord-engine tell`, never a bare bus send: an assignment
that evaporates when a session ends is not an assignment.

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
