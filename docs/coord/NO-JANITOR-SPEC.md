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
5. **Authority rides the review gate; the platform rings the doorbell.**
   Fleet-wide facts (pins, fences, config) live in ONE review-gated manifest
   in the repo (§3); a dedicated annotation channel plus the `data-updates`
   tick check distributes only the "something changed" signal — never the
   facts themselves, and never by editing files on hosts.

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
- **The channel carries ZERO authority (review rounds 1–2; ruled
  2026-08-14).** A channel record is a WAKE HINT — "go check the manifest" —
  never an order. No state transition ever binds to a record's content, so
  publisher identity and record ordering stop being trust questions: the
  worst a forged, duplicate, or garbage record can cause is one wasted
  manifest fetch. This is the operator's original design read precisely —
  the channel was always the doorbell, not the order. (Appendix A preserves
  why the claim-based-authority alternative was rejected.)
- **Authority lives in ONE review-gated manifest in the repo:**
  `docs/coord/fleet-manifest.json` on the canonical repo's main branch,
  carrying the pin (engine version + FULL 40-hex commit SHA), the fence
  value (§4), and fleet config. It changes only via the normal review-gated
  merge to main — the existing merge process plus the operator's forge
  account control IS the protected publisher boundary: server-authenticated,
  audit-trailed, and already how every other fleet-behavior change ships.
  Ordering is git history on main; rollback is a reviewed revert; the audit
  trail is `git log` on one file.
- **Readers fetch, verify, act.** On a wake hint (and before any write, §4),
  the agent fetches the manifest from canonical origin over authenticated
  HTTPS and reconciles to its content in-session: self-adopt the pin, apply
  the fence and config. Adoption stays content-addressed — the manifest pins
  a full commit SHA, so the installed artifact is immutable by git object
  identity, and a manifest naming an abbreviated SHA, branch, or foreign
  repo is invalid (§3a).
- **Records need no schema trust:** any record of the channel's type
  triggers the same idempotent action — fetch the manifest once this tick.
  Conflicting records converge to identical state on every reader because
  the state never comes from the records.
- **The tick-time catch-up check:** every agent tick/wake runs
  `fulcra data-updates "<since last tick>"` — ONE cheap call, no store
  scan — and looks for the channel's data type in the result. Nonzero →
  fetch the manifest and reconcile to it before processing other work.
  Zero → nothing to catch up on, proceed. The same call's file-change rows
  feed the fold cursors of §2, so the check is free when folds already run.
- **§3a Refuse-on-uncertainty.** If the manifest cannot be fetched from
  canonical origin, fails to parse, or names an invalid pin (abbreviated
  SHA, branch name, foreign repo), the agent KEEPS its current
  pin/fence/config, surfaces the refusal loudly in its fold's degraded line
  and its next claim to coord-boss, and never substitutes a cached or
  third-party copy of the manifest — unfetchable means hold state, not
  downgrade.
- **Bootstrap:** a NEW agent fetches the same manifest from canonical
  origin — bootstrap and steady-state read one artifact. `adopt-latest.sh`
  becomes a transitional mirror and is retired at Phase 2 acceptance.
- **Non-goal:** this channel does not detect datashare joins — `data-updates`
  has no `--user-id` form and covers records + files, not shares or catalogs
  (documented in `MESH-PEER-QUICKSTART.md`; watch `share list-incoming`).

## 4. The version fence

- The manifest carries `fleet_min_version` (an engine version). Agents cache
  the fence from the manifest at tick time.
- **Fence changes ride the merge gate.** Raising the fence is a normal
  reviewed manifest change; lowering it is a reviewed revert — the review
  process is the rollback authorization, and git history is the ordering.
  No channel record can move the fence in either direction (§3: records
  carry zero authority), which is what makes the fence safe: the mechanism
  that distributes it cannot be turned against it.
- **Below the fence: reads stay legal, writes refuse** — the transport write
  wrapper checks `engine_version >= fleet_min_version` and fails loudly with
  the adopt instruction. An agent that cannot tick (and so cannot know the
  fence moved) is exactly the stale writer the fence exists to stop: its
  next write attempt re-fetches the manifest before writing (one targeted
  fetch, fail-closed to refuse the write if the manifest is unreadable).
- The legacy `coord-reconcile:<host>` identity prefix is **denied writes
  outright** regardless of version — the class is retired (§5), and the
  fence is where the refusal lives.
- Rollout note: the fence's first value is the first manifest-aware pin —
  set only after the fleet is on a manifest-aware engine (Phase 2
  acceptance), so no agent can be fenced out by a mechanism it cannot yet
  see.

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
- **Phase 2 — manifest + channel + fence.** Merge the first manifest,
  register the channel data type, ship the tick check, then set the first
  fence value (§4 rollout note); retire `adopt-latest.sh` on acceptance.
  *Acceptance:* every fleet agent's tick log shows the catch-up check; a
  manifest change reaches all agents within one tick cycle each, signalled
  by a hint record; the zero-authority tests pass live: a forged/garbage
  channel record causes at most one manifest fetch and ZERO state change;
  a manifest naming an abbreviated SHA, branch, or foreign repo is refused
  with state held (§3a); an unfetchable-origin drill holds state and
  surfaces the degraded line; a below-fence write attempt refuses.
- **Phase 3 — janitor retirement.** §5 steps 1–3. *Acceptance:* 14 days
  with zero store writes from any non-session identity (auditable from
  shard `agent`/authorship fields + presence absence), retention batch
  demonstrably running in-session (batch evidence in coord-boss tick
  claims), and folds' degraded-line rate no worse than the Phase-1 baseline.

## 7. What this kills, what it deliberately keeps

Kills: the cron reconciler and its class; out-of-session store writers;
`adopt-latest.sh` as the distribution mechanism (retired at Phase 2; transitional
mirror); the assumption that fold freshness requires a warm daemon.

Keeps: `send_later`/Routine self-wakes (the session wakes ITSELF — the
writer is the woken session, in-session by definition); the wake router as
shipped (unproven in deployment; nothing here depends on it); presence,
lease, and nonce semantics unchanged; addendum-1's cut of the CoordEvent
second ledger stays cut — the directives channel is not a coordination
event mirror, it carries wake hints only, at fleet-directive rates
(a handful of records a week, not per-task traffic — and the facts those
hints point at live in the manifest, not in any record).

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
   should that become a fenced capability in the manifest instead of a
   hardcoded check)?

## Appendix A — why claim-based publisher authority was rejected (review history)

Two review rounds shaped §3, and the reasoning is preserved so it is not
re-litigated:

- **Round 1** proposed typed pin/fence/config records applied directly from
  the channel, with author identity named in the record. Rejected: a claimed
  identity in JSON is not an authorization boundary — any session holding
  the shared account credentials can write any record, so the channel could
  steer the fleet.
- **Round 2** anchored pins in content-addressed full-SHA installs (real,
  and retained in §3) and demoted the publisher allowlist to
  "defense-in-depth until platform-attested authorship (U10)". Rejected on
  two grounds: (1) a full SHA proves object identity, not release approval —
  any object present in the repo could be selected by a forged record; and
  (2) the ordering the spec assigned to `data-updates` does not exist — the
  command returns an aggregate summary, not a per-record server sequence, so
  latest-by-kind could not be established from data readers actually have.
- **Resolution (r3, ruled by coord-boss 2026-08-14):** move ALL authority
  out of the records and into the review-gated repo manifest; the channel
  is a wake hint. Both P1s dissolve — publisher trust becomes the merge
  gate (already server-authenticated), and ordering becomes git history.
  Server-attested record authorship (U10) remains the upgrade path that
  could one day move authority into the channel itself; until then no
  record content is trusted for anything.
