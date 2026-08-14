# No-janitor coord — specification

**STATUS: DRAFT for cross-model review.** Authored 2026-08-13 under P1 operator
order. Gate: normal dual-green cross-model review; workstream ratification by
coord-boss.

**Authorization and trigger.** Operator, 2026-08-13, on discovering a cron
reconciler running on a personal laptop (the MBP): *"The fact that that exists
is an utter failure. Is that script an architecture requirement for the current
coord to work? If so the current coord is trash. What if I wanted to use it
with only cloud agents. Why can't you use a combination of annotations /
metrics, files and the data updates command to make this work right without
something outside the sessions doing cleanup."* The operator killed the MBP
daemon the same day. A second operator-specified mechanism is folded in as a
requirement, not an option: *"why don't you just have an extra channel for
fleet directives like version updates. Data updates against it on every tick
checks if there is something to catch up on."*

**Amends:** extends `wake-router-ADDENDUM-1-event-substrate.md` §2 (feed-first
principles, adopted here wholesale) from the read path to the whole
architecture. Does not touch ATC or the wake router.

## 0. Ruling on the architecture question

The cron was **not** a correctness requirement. Every fold the engine serves
(`queue`, `needs-me`, `briefing`, `board`, `status`) computes in-session from
canonical shards, with the warm `summaries.json` index and settled index as
derived caches; a cloud-only fleet works today, degraded-but-correct, exactly
as the fail-closed doctrine intends. What the cron actually was: a warm-cache
daemon keeping derived views fresh so in-session reads stayed fast — plus a
retention sweeper. Those are real duties; **hosting them outside sessions is
the failure.** Out-of-session writers are unaccountable (no directive trail,
no presence, no version discipline — the class of stale writer the nonce
forensics and the prefs incidents keep re-finding), and leaning on their warm
projections is why folds report `degraded` whenever the daemon lapses. This
spec re-homes the duties and retires the host class.

## 1. Normative principles

1. **No out-of-session writers.** Every write to the coordination store
   happens inside an accountable agent session — one with an identity, a
   presence beat, and a directive trail. The janitor CLASS is retired, not
   just the MBP instance: no cron, launchd job, CI schedule, or resident
   daemon may hold `FULCRA_COORD_AGENT` and write.
2. **Cloud-only is the baseline, not a degraded mode.** The fleet must be
   fully functional — folds fresh, retention running, versions distributed —
   with zero host-local machinery. Anything host-local is an optimization
   that must be killable at any moment without correctness loss (the
   operator's test: "what if I wanted to use it with only cloud agents").
3. **Folds are in-session, event-driven, and incremental.** Addendum-1 §2
   applies unchanged: the `data-updates` feed is the ledger, listings are a
   cache, feed-first fail-closed, shards canonical, derived views rebuilt
   never trusted, no second ledger. This spec extends the consequence: since
   any session can cheaply compute "what changed since my cursor," **the
   session that reads is the session that folds** — there is nothing left
   for a janitor to pre-compute except warmth, and warmth is what read-repair
   provides (§2).
4. **Version fence.** A writer whose engine version is below the fleet
   minimum refuses to write (§4). This converts "stale out-of-session
   writer" from an incident class into a refused operation.
5. **Distribution rides the platform, not host config.** Fleet-wide facts
   (pins, fences, config) are typed records on a dedicated annotation
   channel (§3), caught up on every tick via `data-updates` — never
   distributed by editing files on hosts.

## 2. Mechanism: in-session folds with bounded read-repair

- **Cursor-delta folds.** Each agent keeps a per-team feed cursor (already
  the queue/fold pattern). On any fold-consuming verb, the engine reads
  `data-updates` since the cursor, overlays the delta onto the derived view,
  and serves the result. Fallback on any doubt: full scan, fail-closed —
  byte-identical to today's behavior.
- **Read-repair replaces pre-warming.** When a fold finds a derived view
  stale (index older than feed watermark, unsummarized docs, settled index
  missing entries), the reading session repairs it **within a bounded
  budget** (docs-per-repair cap; the 129-doc deafening incident is the
  sizing anti-pattern) and persists the repaired view for the next reader.
  Unrepaired remainder is carried, loudly, in the fold's degraded line —
  never silently dropped. Successive readers converge the backlog.
- **Retention becomes a bounded in-session duty.** The `reconcile`
  retention sweep (archive quiet terminal tasks, settled orphan reviews)
  runs as a capped batch inside a designated role's normal wake —
  coord-boss's tick is the natural home; any session MAY run a batch, the
  role guarantees liveness. Same code path, session-hosted, budget-capped,
  presence-attributed.
- **Health shards** are written by sessions about themselves (wake
  closeout), not by a daemon about the world.

## 3. Mechanism: the fleet-directives channel (operator-specified, REQUIRED)

- **One dedicated annotation data type** (working name `FleetDirective`,
  a `MomentAnnotation/<uuid>`) registered in `_coord/bus-v3/records.json`
  beside the bus channel — resolved from the registry, never hardcoded.
- **Typed records, `v:1` JSON in `note`:** `kind: pin` (engine pin + rc,
  replaces `adopt-latest.sh` as the distribution signal), `kind: fence`
  (fleet minimum version, §4), `kind: config` (fleet-wide settings). Records
  are append-only; every record names its author identity and the
  authorizing directive slug.
- **Authority model (review round 1, P1).** A claimed identity in JSON is
  not an authorization boundary — any session holding the account's
  credentials can write any record, so the channel by itself must not be
  trusted to steer the fleet. Trust is anchored in what IS server-enforced
  today, and every verification failure refuses (§3a):
  - *Account boundary (server-enforced):* readers accept records only from
    the fleet account's own registered channel (`records.json`), never from
    a shared/foreign catalog. Writing it at all requires the account's
    credentials — the same trust root as the store the fleet already runs
    on. The channel adds no NEW writer surface; it inherits the existing
    one.
  - *Content-addressed pins (server-enforced by the forge):* a `pin` record
    carries the FULL 40-hex commit SHA of the canonical repo. Adoption
    installs `git+<canonical-repo>@<sha>` — git object identity is the
    artifact hash, so a pin can only ever point at code that exists in the
    repo, and publishing runnable code requires repo push access: a second,
    independently server-authenticated boundary (the forge's, not
    Fulcra's). The channel conveys WHICH commit; the repo remains the root
    of trust for code. Records carrying abbreviated SHAs, branch names, or
    foreign repo URLs are invalid and refused.
  - *Publisher allowlist (claim-based today, attested later):* only the
    named release role (coord-boss, or a successor named in a `config`
    record that itself passed verification) publishes pin/fence/rollback
    records. Readers check the author claim AND that the named authorizing
    directive row exists on the bus. This is defense-in-depth, not a
    boundary, and the spec says so: until the platform attests record
    authorship server-side (upstream register **U10** — the same primitive
    the mesh needs), the allowlist is honest-majority hardening on top of
    the two real boundaries above. When U10 lands, the allowlist check
    upgrades from claim to attestation with no protocol change.
- **Deterministic ordering.** Latest-by-kind is decided by the server-side
  change ledger (`data-updates` processing order), with record id as the
  lexicographic tiebreak for equal timestamps — never by client-supplied
  time fields, which are forgeable. Two conflicting same-instant records
  therefore resolve identically on every reader; a reader that cannot
  establish the order refuses (§3a).
- **The tick-time catch-up check:** every agent tick/wake runs
  `fulcra data-updates "<since last tick>"` — ONE cheap call, no store
  scan — and looks for the channel's data type in the result. Nonzero →
  read the directive records → act in-session (self-adopt the pin, update
  the fence, apply config) before processing other work. Zero → nothing to
  catch up on, proceed. The same call's file-change rows feed the fold
  cursors of §2, so the check is free when folds already run.
- **§3a Refuse-on-uncertainty.** If publisher verification, directive
  cross-check, ordering, or SHA validity is uncertain for the latest record
  of a kind, the agent KEEPS its current pin/fence/config, surfaces the
  refusal loudly in its fold's degraded line and its next claim to
  coord-boss, and does not fall back to an older record of that kind
  (an attacker must not be able to force a downgrade by appending garbage).
- **Bootstrap:** a NEW agent still reads `adopt-latest.sh` once to get an
  engine; from first tick onward the channel is authoritative and the file
  is a mirror the pin-record author updates for bootstrap only.
- **Non-goal:** this channel does not detect datashare joins — `data-updates`
  has no `--user-id` form and covers records + files, not shares or catalogs
  (documented in `MESH-PEER-QUICKSTART.md`; watch `share list-incoming`).

## 4. The version fence

- A `kind: fence` record carries `fleet_min_version` (an engine version) and
  the authorizing directive. Agents cache the latest fence at tick time.
- **The fence is monotonic.** A record whose `fleet_min_version` is LOWER
  than the cached fence is ignored and reported, never applied — a later
  record must not be able to lower the minimum and re-admit stale writers.
  Lowering requires an explicit `kind: rollback` record that (a) names the
  fence value it lowers to and the operator-authorized directive ordering
  it, and (b) is mirrored by a repo commit updating the bootstrap mirror —
  so a rollback needs BOTH the account credentials and repo push access,
  the same two boundaries as a pin (§3). Absent either leg, readers keep
  the higher fence. Note the review's structural point stands and is
  answered by scope: the fence never defends against a compromised channel
  (the channel's own §3 boundaries do that); it defends against stale
  writers, and monotonicity keeps the channel from being turned against
  that purpose.
- **Below the fence: reads stay legal, writes refuse** — the transport write
  wrapper checks `engine_version >= fleet_min_version` and fails loudly with
  the adopt instruction. An agent that cannot tick (and so cannot know the
  fence moved) is exactly the stale writer the fence exists to stop: its
  next write attempt re-reads the fence record before writing (one targeted
  read, fail-closed to refuse if unreadable).
- The legacy `coord-reconcile:<host>` identity prefix is **denied writes
  outright** regardless of version — the class is retired (§5), and the
  fence is where the refusal lives.
- Rollout note: the fence distributes over the channel it gates. The first
  fence record is therefore set only after the fleet is on a channel-aware
  pin (Phase 2 acceptance), and the fence's first value is that same pin —
  no agent can be fenced out by a mechanism it cannot yet see.

## 5. Retirement of the `coord-reconcile:*` class

Known members: the MBP cron reconciler — **killed by the operator
2026-08-13**; no `coord-reconcile:<host>` identity currently beats presence.
Steps, in order:

1. Purge cron/daemon install instructions from `packages/coord-engine/README.md`
   and any host docs; the `reconcile` verb remains as a **manually invoked,
   in-session** maintenance command (bounded batches, §2) and its
   `--retention-days` semantics are unchanged.
2. Engine: refuse `FULCRA_COORD_AGENT=coord-reconcile:*` on write paths
   (fence rule, §4).
3. Re-home duties per §2 (read-repair + role-hosted retention batch +
   session-owned health shards). Delete the daemon-only code paths once the
   duties demonstrably run in-session (Phase 3 acceptance).

## 6. Phases and acceptance (machine-checkable)

- **Phase 1 — converge the parked event-read cutover.** The read-mode flip
  coord-maintainer soaked is this spec's §2 read path; adopt it as the first
  execution step. *Acceptance:* folds run feed-first on the flipped mode for
  7 days with zero fail-closed fallbacks attributable to the flip (degraded
  lines quote the feed cursor, not listing staleness). Operator flips per
  the standing parked row — this spec does not bypass that gate.
- **Phase 2 — channel + fence.** Register the data type, ship the tick
  check, publish the first pin record, then the first fence record (§4
  rollout note). *Acceptance:* every fleet agent's tick log shows the
  catch-up check; a test pin record reaches all agents within one tick
  cycle each; a below-fence write attempt refuses in a live test; AND the
  authority tests pass live: a record with a non-allowlisted claimed
  author is refused; a pin carrying an abbreviated SHA, branch name, or
  SHA absent from the canonical repo is refused; a fence-lowering record
  without the two-leg rollback is ignored and reported; two same-instant
  conflicting records resolve identically on two independent readers.
- **Phase 3 — janitor retirement.** §5 steps 1–3. *Acceptance:* 14 days
  with zero store writes from any non-session identity (auditable from
  shard `agent`/authorship fields + presence absence), retention batch
  demonstrably running in-session (batch evidence in coord-boss tick
  claims), and folds' degraded-line rate no worse than the Phase-1 baseline.

## 7. What this kills, what it deliberately keeps

Kills: the cron reconciler and its class; out-of-session store writers;
`adopt-latest.sh` as the distribution mechanism (demoted to bootstrap
mirror); the assumption that fold freshness requires a warm daemon.

Keeps: `send_later`/Routine self-wakes (the session wakes ITSELF — the
writer is the woken session, in-session by definition); the wake router as
shipped (unproven in deployment; nothing here depends on it); presence,
lease, and nonce semantics unchanged; addendum-1's cut of the CoordEvent
second ledger stays cut — the directives channel is not a coordination
event mirror, it carries fleet-scope facts only, at fleet-directive rates
(a handful of records a week, not per-task traffic).

## 8. Open questions for review

1. Retention-actor liveness: is coord-boss's tick a sufficient guarantee, or
   should the duty rotate (any wake runs a batch when the last batch is
   older than N hours)?
2. Feed retention horizon vs cold-start: addendum-1's revisit trigger
   (derived snapshots must cover any fold horizon the feed cannot) — does
   Phase 1 need a measured answer before Phase 3 deletes the full-scan
   warm path, or does fail-closed full scan cover cold-start forever?
3. Fence granularity: engine version only, or per-capability fences (e.g.
   cursor-v2 activation is already forbidden by a version warning today —
   should that become the first fence record instead of a hardcoded check)?
