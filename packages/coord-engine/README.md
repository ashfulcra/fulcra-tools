# coord-engine

For the complete system model and interoperability contract across Coord Engine,
the Agent Coordination Bus, and Collect, start with
[`docs/coord/SYSTEM-SPEC.md`](../../docs/coord/SYSTEM-SPEC.md).

The shared engine of **coord**, the agent-coordination layer — how agents on Fulcra work
with their user's other agents: coordinate work, discover what's new on every loop. It is
a **stdlib-only** Python CLI that gives a fleet of independent agents (Claude Code, Codex,
OpenClaw, CI, humans) durable coordination over the Fulcra File Store as a bus. Judgment stays in prose — the fourteen
[`fulcra-agent-*` skills](../../skills) (of 17 total) — and every consistency-critical fold (who's live,
what's mine, is this review settled) is a deterministic engine verb, so two agents always
agree on derived state instead of eyeballing timestamps.

New to coord? Start with the [get-on-the-bus quickstart](../../docs/coord/GET-ON-THE-BUS.md)
(from zero: team bootstrap, auth, remote-sandbox requirements, the join sequence), the
protocol behind the design ([`COORDINATION-PROTOCOL.md`](../../COORDINATION-PROTOCOL.md)),
and the agent conventions ([`AGENTS.md`](../../AGENTS.md)).

## Install

```bash
uv tool install "git+https://github.com/ashfulcra/fulcra-tools@coord-engine-v2.0.3#subdirectory=packages/coord-engine"
coord-engine doctor <team>   # tooling + auth + store reachability, end to end
```

Not on PyPI yet — install from the git tag (or a checkout:
`uv tool install ./packages/coord-engine`). The release tag is the
**cold-install** path; the **fleet's runtime authority** is the store BOOTSTRAP
(`team/fulcra/_coord/bus-v3/adopt-latest.sh` + `BOOTSTRAP.md`, current pin scheme
`pp-<sha>`), not this README — once you are on the bus, adopt from there. The
engine shells out to the
[`fulcra-api` CLI](https://pypi.org/project/fulcra-api/) for storage
(`uv tool install fulcra-api && fulcra auth login`); override the launcher via
`$FULCRA_CLI_COMMAND`. Identity comes from `$FULCRA_COORD_AGENT` — set it to the **role**
you act as (see the [presence skill](../../skills/fulcra-agent-presence/SKILL.md)).

## The verbs, by concern

| Concern | Verbs |
|---|---|
| Wake up / what needs me | `queue` — the bus v3 event delivery surface ([`docs/coord/BUS-V3.md`](../../docs/coord/BUS-V3.md)): schema v1 is the legacy print-then-advance cursor; activated schema v2 CAS-stages a token and advances only through `queue commit TEAM --token TOKEN`; every nonzero exit is fail-closed and rc alone is NOT the state discriminator (rc 2 carries ABSENT/REFUSED branches, rc 3 carries UNKNOWN/INVALID/INCOMPATIBLE/REFUSED — read `state` + `error_code` from the `--json` envelope); the obligations fold never changes a successful read's rc — a clean window whose separate durable-obligation fold could not complete stays rc 0 and reports fold UNKNOWN/INVALID through the additive `obligations` key on the success envelope, while nonzero queue-family failures (read and `queue commit` alike — commit returns rc 3 for INCOMPATIBLE, stale-token REFUSED, and unsupported CAS) retain their documented `state`/`error_code` contract; that fold is opt-in (`--obligations`) and a skipped fold is stated, never implied, as `"obligations":{"state":"not-checked"}` on every machine-readable success envelope — a malformed config or cursor is INVALID (human-fixable, never auto-recreated over, `error_code=*-invalid`), distinct from UNKNOWN (`*-read-failed`/`window-unknown`, retry); under `--json` a successful read prints exactly one `{"type":"queue-result","state":"DATA"\|"CLEAR",events,count,cursor:{path,advanced},engine_version,protocol,obligations}` object and EVERY nonzero exit of `queue`/`queue commit` prints exactly one `queue-error` object (state `UNKNOWN`\|`INVALID`\|`INCOMPATIBLE`\|`ABSENT`\|`REFUSED` + per-branch `error_code`; sole exclusion: argparse's own usage exits) — text-mode success output is byte-stable for shell consumers; `queue --consume` (deliberate takeover of another agent's cursor) writes a durable audit doc to `_coord/audit/consume/<UTC-stamp>-<caller>-takes-<target>.md` before reading and is REFUSED if the audit write fails, while `--peek` writes nothing; `doctor` includes the adoption-claim + running-presence version census; `obligations` (the standalone do-I-owe-anything fold: rc 3 = UNKNOWN, rc 4 = INVALID; on queue reads the fold is opt-in via `--obligations`) · `briefing` · `needs-me` · `inbox` · `digest` fold the durable state |
| Bus authority migration | `bus-v3 migrate TEAM --dry-run\|--apply` — classify discovered/explicit legacy cursor owners and idempotently add the complete schema-v1 authority block; malformed/unreadable state blocks, apply never writes cursor/task/role documents, and the JSON `state` + `error_code` contract (including `ISSUED-BUT-UNPROVEN`) is defined in [`BUS-V3.md`](../../docs/coord/BUS-V3.md) |
| Task views (self-healing) | `reconcile` · `status` · `board` · `search` · `task` (incl. `supersede` — close a re-dispatched copy with `superseded_by`; `block` requires `--unlock`) |
| Directives & messaging | `tell` · `broadcast` · `remind` (schedules a future-dated bus-v3 record: the reminder delivers itself at WHEN via the assignee's queue read) · `respond` · `later` (backlog) · `intent` (spoken commitment) — the retired `listen` watcher was removed 2026-08-03 (PR #523); replies ride the queue read |
| Dropped-work fold | `threads` (started-then-silent / blocked-on / intent-never-started, per principal) |
| Identity & liveness | `presence` · `agents` · `roles` (claim/release/status) · `escalate` · `engagement gate` (mixed-fleet coverage) |
| Operator loop | `asks` (waiting-for-operator, oldest first) · `answer` (unblock + hand back) |
| Review handshake | `review` (request/status) — one `pr-N` slug advances through exact `--head` rounds; obligation persists until active-head verdict files exist |
| Continuity | `continuity` (snapshot/checkpoint/park/resume) |
| Durable tooling stash | `stash` (push/pull/list — manifest + sha256 + fail-closed secrets guard) |
| Wake routing | `wake` (queue-file/consume — local SessionStart nudges) · `router` (`run` — feed-first policy plane, direct cloud-adapter execution, host-local queue; `execute` — thin host-local executor; [plan](../../docs/coord/wake-router-PLAN.md)) |
| Fleet ops | `health` · `doctor` · `acceptance pair` · `forge` · `annotate` · `bus-v3 tag-provision` (register an identity's four timeline-tag dimensions — agent/platform/harness/model — in `_coord/bus-v3/tags.json`, so every event this agent writes is filterable in the Fulcra visual explorer; `model` is a declaration the engine cannot verify, and a switch is a cheap re-provision) · `bus-v3 send` (the supported hand-send for a bare event — resolves the stream from the records authority and attaches those tags, which a raw `fulcra-api record` pipe cannot do) |
| ATC (cap routing) | `route` · `usage` · `headroom` · `atc` · `dash` |

`coord-engine <verb> --help` for flags; most read verbs take `--json`. Sub-verb lists above are by concern, not exhaustive — the parser's help is the inventory. The
[skills](../../skills) carry the procedures (when to run what, and why);
per-verb command references live in each skill's `references/` directory.
Machine JSON is emitted compactly: `threads` emits one JSON array. `needs-me`
preserves partial rows but returns rc 3 when its bounded forge fallback emits
`forge-degraded`; a complete projection or raw scan returns rc 0. The hot
`_coord/summaries.json` aggregate is a compatibility cache with the same
zero-whitespace serializer; its parsed values and degradation markers are
unchanged. The dormant generation-serving candidate uses
`_coord/projections/current.json` as its projection pointer; it names a
digest-verified immutable generation. Reconcile advances it only after every required section is complete
and the feed frontier is attested. One narrow recovery exists for an established
reconcile cursor whose outer `data-updates` envelope omits or cannot parse its
coverage boundary/frontier: reconcile performs a visibly labelled
`detector-full-scan`, rebuilds every canonical section, and may republish missing
`current.json`/immutable-generation substrate. It preserves the last proven
watermark and seals the canonical snapshot into generation identity; it never
invents feed progress. Cold start publishes one generation from an absent
manifest; later identical recoveries recognize that sealed snapshot and reuse
the current generation instead of chaining on `prior_generation_id`. Every
other detector `UNKNOWN` still aborts without a
canonical scan or pointer advance. A transport that proves conditional-write
support uses CAS; one that explicitly declares CAS unsupported uses a
last-writer-wins manifest write followed by exact read verification. Missing or
invalid capability and manifest write/read failure are nonzero. The mandatory
candidate public-read freshness overlay rejects a stale raced manifest before v2
authority activates. When separately activated, the shared public-read path
validates the exact manifest and immutable
generation bytes, queries one at-least-once `data-updates` window from
`watermark - epsilon` through `now - epsilon`, deduplicates update identities,
validates every sealed section before a domain handler sees it (including exact
inventory record shape, namespace, string content, parsed frontmatter, and
canonical document type), and applies only locally supported task deltas.
Presence is flat under `presence/`; acknowledgments and responses are one
slug directory deep under `_coord/acks/` and `_coord/responses/`. Slugs match
`[a-z0-9]+(?:-[a-z0-9]+)*`; leaf names start alphanumeric, use only
alphanumerics/`_`/`-`, and end in one final `.md`. Dot/traversal segments,
file-shaped intermediate components, hidden leaves, and the legacy singular
`response/` path are unsupported. Review projection v3 carries the
complete direct-status tally; its producer, publication authority, and domain
consumers share one nested validator for act-on fields, unique row slugs,
settled invariants, and orphan/tombstone lists. Legacy v2 rows and v1 settled
caches are rebuilt before they can be sealed as compatible.
Unsupported deltas, incomplete
feed coverage, a changed pointer, or an unverified epsilon return typed
`UNKNOWN` and nonzero; an overlay that was not invoked is `NOT_RUN`, never
clean. JSON and text both expose the generation, source watermark, attested
coverage horizon, and per-surface coverage. The tagged `2.0.0` transport still
sets `public_read_v2_enabled=true`, so migrated public folds enter the dormant
candidate and return `UNKNOWN` before reaching canonical handlers. That is an
adoption blocker until a reviewed exact head disables generation serving while
preserving generation construction. Epsilon is cancelled and inapplicable; it is
not a repair for this mismatch.

`roles status` now returns one `liveness_fact` that carries lease and presence
observations plus both store-prefix provenance fields. Fresh holder presence and
a lapsed lease can therefore never be rendered as confidently `VACANT`.
Attendance is separately typed: omitting `--check-attendance` reports
`NOT_RUN`; a requested scan that hits its cap reports `UNKNOWN` with
`scanned`/`total` and exits nonzero; a completed check reports its boolean
result. When separately activated, generation-backed public folds reconstruct
role/presence inventories from the validated immutable bytes rather than reopening
the mutable store.

Class A folds now enter the output boundary through
`coord_engine.outcome.CommandOutcome`, the shared v2 state/coverage/rows/source
spine. Required incomplete coverage is typed `UNKNOWN` (rc 3); the existing
contract-2 `DEGRADED` envelope remains a compatibility rendering for usable
partial rows. Text and JSON adapters consume the same outcome, so serializers
do not independently decide truth or exit status. Optional text coverage is
explicitly marked `non-gating`, matching JSON's `required:false` field.

### Pairwise acceptance

After two identities are installed and authenticated, one operator can prove the
entire join path with one stdlib-only command:

```bash
coord-engine acceptance pair <team> --agent <A> --peer <B>
```

It runs `doctor --delivery` as both identities, sends a nonce directive A→B,
queue-reads it as B, returns a nonce response B→A, queue-reads it as A, then parks
and resumes B through a nonce-scoped `acceptance-peer-*` role with a checkpoint-age
limit of five minutes. The final hop refreshes and verifies both presence shards,
then removes the acceptance lease, role, and checkpoint.
Each successful hop prints `HOP N PASS` with elapsed time; the first failure exits
nonzero as `FAILED AT HOP N` followed by the raw evidence. `--timeout` (90 seconds
by default) bounds the delivery probes and the two queue polls (hops 1, 2, 4, and
6); the tell/respond/claim/park/resume/presence operations use their underlying
transport bounds.

## Properties worth knowing

- **Stdlib-only runtime.** No dependencies; transport is a subprocess call. Installs
  anywhere Python ≥3.10 runs.
- **Deterministic folds, feed-first.** Views are maintained incrementally from the
  store's `data-updates` change feed (the authoritative ledger — listings are
  eventually-consistent caches), reading only changed shards. Feed doubt or a
  scheduled drift check rebuilds from the full listing scan; orphaned index entries
  cannot recur, and role/review/presence status are computed, never inferred by a model.
- **Fails loud, never silent.** Unverifiable writes are retried, cached locally, and
  announced; a degraded read fold says so (`review-fold-degraded`, `review-head-degraded`,
  a `queue-error` envelope, or a `raw scan — <reason>` source row)
  instead of returning a clean-looking partial answer.
- **Park certifies every selected save.** Role documents and role-lease
  directories form one deduplicated candidate set. A lease directory without a
  readable role document is UNKNOWN and prints `CHECKPOINT NOT WRITTEN`; any
  snapshot or checkpoint-ref failure makes the command nonzero. rc 0 means all
  selected role saves completed, while rc 2 means a complete fold found no
  fresh held role.
- **Structured logs** to stderr (`$COORD_LOG_LEVEL`).

## Environment / tuning

The single reference for every environment variable the engine reads. **Prefix rule:**
`COORD_*` is the engine-native, canonical prefix for all tuning knobs; `FULCRA_COORD_*`
is the legacy prefix, retained for the identity vars below and **alias-accepted for
`COORD_RETENTION_DAYS` only** (an operator migrating off the deprecated `fulcra-coord`
bus keeps working — when both are set, the `COORD_*` form wins). No other tuning knob
reads a `FULCRA_COORD_*` alias.

**Parse policy (all numeric knobs, one shared parser — `coord_engine/config.py`):** a
value is **positive-finite**, resolved **flag/constructor arg > env > default**; anything
unparseable, `NaN`, `inf`, or `≤ 0` falls back to the default — a bad value can never
disable a bound or make an op hang.

### Budgets & timeouts

| Variable | Default | Unit | Bounds |
|---|---|---|---|
| `COORD_TRANSPORT_TIMEOUT` | `30` | seconds | Hard per-op bound on every `fulcra-api file` subprocess. Constructor arg wins; run it TIGHT on a watcher (e.g. `8`) so the fold budgets buy real responsiveness. |
| `COORD_REVIEW_FOLD_BUDGET` | `45` | seconds | Aggregate deadline for the pending-review fold (`_pending_reviews_for`) — the RAW-SCAN path; a fresh projection answers the tail in zero ops. |
| `COORD_PROJECTION_MAX_AGE_HOURS` | `24` | hours | Freshness bound a `reviews`/`forge` projection section must meet before a fold may serve it. Beyond it the fold raw-scans and says `raw scan — <key> projection stale (Xh old, max Yh)`. |
| `COORD_PROJECTION_BUILD_BUDGET` | `240` | seconds | Per-`reconcile`-pass budget for BUILDING those projection sections. On breach the section is stamped incomplete (and therefore unserved) rather than published partial; a large legacy corpus converges across passes. |
| `COORD_BRIEFING_BUDGET` | `60` | seconds | Aggregate deadline for the `briefing`/`needs-me` transport-heavy add-on stack (chiefly the forge-feedback fan-out); opened once, spent cumulatively across sections. |
| `COORD_FORGE_SWEEP_BUDGET` | `60` | seconds | Aggregate deadline for the direct `forge feedback` fallback: review/watch discovery plus its per-PR three-surface sweep. Breach is fail-visible and returns non-zero with a `forge-sweep-degraded` marker. |
| `COORD_ROLE_FOLD_BUDGET` | `20` | seconds | Cumulative deadline for one role-resolution pass (`_held_roles_for_rows`), which `briefing`/`inbox`/`needs-me` all run. Spent across the `roles/` listing, each role's doc + lease listing, and each lease shard read; a cut marks every unfinished candidate `unresolved` and emits `role-degraded`. |
| `COORD_OVERLAY_BUDGET` | `10` | seconds | Time bound on the listing-based freshness fallback's fresh-doc reads (the cap bounds read COUNT; this bounds TIME). Healthy folds use the team-filtered updates feed instead. |
| `COORD_OVERLAY_CAP` | `16` | count | Max fresh (unsummarized) task docs the listing fallback reads per surface-read before truncating (visibly). |
| `COORD_SUMMARY_TEXT_CAP` | `280` | chars | Per-field cap on `title`/`description` in a summaries row (ellipsis-marked). The index stays a *summary* — the full payload lives in the task doc; uncapped multi-KB directive payloads inflate `_coord/summaries.json` past what remote transports can read inside the fold budgets. |
| `COORD_THREADS_FOLD_BUDGET` | `30` | seconds | Aggregate deadline for the `threads` dropped-work fold's per-candidate reads; breach emits a `threads-degraded` row. |
| `COORD_THREADS_SILENCE_DAYS` | `3` | days | `threads` started-then-silent window (flag `--silence-days` wins). |
| `COORD_THREADS_INTENT_GRACE_HOURS` | `48` | hours | `threads` intent grace when an intent declares no window (flag `--intent-grace-hours` wins). |
| `COORD_RECONCILE_FULL_EVERY` | `72` | count | Incremental reconcile passes between forced task-listing drift checks; `1` full-scans every pass. Missing/corrupt cursor state, feed doubt, an unreadable changed shard, or an aggregate older than `MAX_FAST_PATH_HOURS` full-scans regardless. |
| `COORD_ACKS_FULL_EVERY` | `72` | count | Passes between FORCED full ack folds in `reconcile`. The fold is change-driven (it asks the store what changed and re-folds only those slugs); this bounds how long a change the query never reported can persist, and carries the orphan-shard GC, which only rides the full fold. `1` disables the incremental path (every pass lists every ack dir). Default 72 is ~daily on a 20-min heartbeat: a forced full fold measured 1091s (~18min) on a 1.2s/op remote transport, so the old `12` (~4h) taxed every remote host 18min every four hours to re-check a query already verified complete against an independent listing. 72 makes that forced fold 6x less frequent (~daily on the same heartbeat) — a sixth of the recurring cost. Any doubt — no change query, a query error, no anchor, a changed slug that wouldn't list — full-folds regardless of this knob, and does not advance the fold's anchor (`acks_folded_through`), so the unread change stays in the next pass's window. |
| `COORD_RETENTION_DAYS` | `14` | days | `reconcile` cold-archives quiet terminal (`done`/`abandoned`) and stale `proposed` tasks after 14 days by default (flag/env overrides). Settled reviews are archived wholesale after 7 days and indexed so hot folds skip their soft-delete tombstones; presence dead over 7 days is pruned; legacy `artifact/` is consolidated into `artifacts/`. Moves are copy-verified and fail closed. Legacy alias: `FULCRA_COORD_RETENTION_DAYS` (canonical wins). |

### Identity, state & logging

| Variable | Default | Bounds |
|---|---|---|
| `FULCRA_COORD_AGENT` | `coord-reconcile:<host>` | Agent identity — set it to the **role** you act as (`--from` overrides per-command). Legacy prefix; still canonical for identity. |
| `FULCRA_COORD_HUMAN` | `human` | Operator handle for `--on-user` / `asks`. |
| `COORD_ENGINE_STATE_DIR` | `~/.local/state/coord-engine` | Local state root (write-verify nonce cache, etc.). |
| `COORD_LOG_LEVEL` | `info` | Structured-log level to stderr (`debug`/`info`/`warn`/`error`). |

The aggregate-backed task surfaces are feed-first: ordinary detection consumes
one normalized, bounded `data-updates` envelope, then reads only the canonical
documents named by that batch. Coverage is explicit for tasks/directives,
review registers/verdicts/settled markers, forge feedback, presence/roles,
acknowledgments/responses, and projection metadata. `CLEAR`, `DATA`,
`UNKNOWN`, and `NOT_RUN` are distinct facts: any malformed envelope, timeout,
permission/read doubt, incomplete namespace, or unsupported team path is
`UNKNOWN`, triggers the named full-scan recovery, and never advances the
watermark. A record count is only a zero/nonzero detector signal, never a
cardinality, threshold, diff, or identity. Persistent live pairs of reported
count to enumerated identities (`2721 -> 9`, `1444 -> 0`, `2737 -> 25`) make
numeric equality invalid. A positive signal must materialize at least one
immutable identity in one bounded attested cursor read or coverage is
`UNKNOWN`; a zero signal becomes CLEAR only when that cursor window proves it.
The outer feed and record cursor must attest one contiguous window: exact
requested `after`, and record `through` at or beyond feed `through`. A lagging,
missing, mismatched, or incomparable boundary/frontier is `UNKNOWN`, advances
no watermark, and leaves the public overlay `NOT_RUN`/nonzero. Bootstrap is
licensed only by an outer `after` plus a cursor covering the same full
frontier. The concrete
count key always comes from the stored `_coord/bus-v3/records.json` authority:
the host-local `COORD_RECORDS_TYPE` writer/test override cannot redirect
detection. That authority read and its optional one retry spend the detector's
existing deadline; expiry is `UNKNOWN`. Canonical documents remain authority
and projections remain replaceable views. Presence is still a bounded
current-time evaluation on every briefing, so session dormancy can become
`LAPSED` even when no shard bytes changed.

## Dev

```bash
uv run --extra dev pytest       # from packages/coord-engine/
```

The suite is CI-gated on Linux and macOS; run it locally before pushing (see
[`AGENTS.md`](../../AGENTS.md) → CI section). Design history:
[`docs/coord/`](../../docs/coord) and [`docs/coord-DESIGN.md`](../../docs/coord-DESIGN.md).

**Releasing:** cutting a `coord-engine-vX.Y.Z` tag REQUIRES bumping `__version__` in
[`coord_engine/__init__.py`](coord_engine/__init__.py) to the same `X.Y.Z` **in the same commit** —
`doctor` self-reports `__version__`, so a tag without the bump makes upgraded installs report a stale
version (v1.4.0/v1.5.0/v1.5.1 all shipped stale off a frozen `1.3.0`, caught by a remote field report).

### 2.0 truthfulness boundary

`2.0.0` ships the truthfulness spine: typed outcomes, exit codes that agree
with their bodies, deterministic identity precedence, and distinct empty,
tombstoned, unreadable, and unknown states. Before the fleet may adopt the
release, public action surfaces must read canonical authorities directly.

Generation-backed serving is the required dormant state for this release. The
tagged implementation does not yet satisfy it: `public_read_v2_enabled=true`
routes migrated folds into generation authority and returns `UNKNOWN` before
canonical handlers. Adoption therefore remains blocked pending a reviewed
serving-disable exact head. Epsilon is inapplicable to the `2.0.0` release and
adoption gate: do not run the cancelled host-one
measurement, set `public_read_epsilon_verified`, or treat a current generation
as public authority. Cursor schema 2 is a separate activation and remains
refused until the fleet version fence and CAS transport are proven.

Adoption requires exact released-build identity on every live host within the
declared SLA, named exclusions with evidence, and functional verification from
two credentialed hosts across `queue`, `needs-me`, review, forge, roles,
presence, and reconcile. Required `UNKNOWN`, degraded fallback, or nonzero
results block the adoption claim. Release, adoption, generation serving, and
cursor-v2 activation are four independent facts.
