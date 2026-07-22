# Wake Router + Engagement Model — stage-2 implementation plan

**Parent spec:** [`wake-router-SPEC.md`](wake-router-SPEC.md) (merged `8211776`, Part C resolution
`5d44b68`). **Owner:** Tycho (`coord-boss`). **Gate:** dual-green (codex-reviewer + owner) on this
plan before any task below is dispatched; then per-task dual-green during execution.

## 1. Work breakdown (task DAG)

Tasks are sized for one PR each, red-first tests throughout, ATC untouched. Assignee is the
*planned* implementer; actual dispatch happens on the bus after this plan gates.

| # | Task | Depends on | Assignee (planned) |
|---|---|---|---|
| W1 | **Engagement schema, inert.** `presence beat` gains `--engagement resident\|session\|occasional` + `--until <ts>`; shard carries `engagement:{mode,until}`; all folds PARSE it, none acts on it yet. Legacy shards (no field) read as `resident` (today's behavior — no semantic change at this step). | — | coord-maintainer |
| W1.5 | **Activity-implies-liveness write-path refresh.** Every engine bus write (tell/respond/task/review/reconcile verbs) refreshes the actor's presence beat, throttled to once per beat interval per process (in-memory memo: N writes in one interval ⇒ exactly ONE beat write — pinned by test); a refresh failure NEVER fails the successful write (stderr note only). This is the spec §Part-B consumption requirement with both constraints; red-first tests include the throttle pin and the failure-isolation pin. | W1 | coord-maintainer |
| W2 | **Engagement-aware liveness fold.** `presence show`/`health`/vacancy consult engagement: `session` past `until` renders **PARKED** (distinct from stale/dead); the fold treats recent write-path activity (W1.5) as liveness proof; CONCUR conditions enforced in output (stale-nudge visible, exact-id matching, dormancy ⊥ staleness). Semantics changes to vacancy/escalation land **behind the mixed-fleet gate** (§3). | W1, W1.5 | coord-maintainer |
| W3 | **Zero-token lapse sweep.** `coord-engine engagement sweep <team>` (host tick, model-free): session agents past TTL are marked **LAPSED** (visible marker; idempotent; never fires on `resident`/`occasional`) and their wake policy drops to the reduced check-in cadence (default 6h) **indefinitely — the sweep never parks and never releases roles** (operator decision 2026-07-22: park is explicit-only, via a park directive or the agent's own continuity park). Vacancy/escalation reads LAPSED as explained absence. | W2 | coord-maintainer |
| W4 | **Router core.** `coord-engine router run <team>` (or `--once`): cursor-based store scan (**explicitly NOT the `listen` fold** — the router must be structurally immune to the 2026-07-22 listen-starvation class), policy evaluation (busy-aware deferral, P1/directed-only interrupt gating, per-agent debounce), durable state under `team/<team>/_coord/router/` (`cursor.json`, `queue/`, `dead-letter/`, `delivered.json`) per the spec's relay contract. Pure stdlib; config file format defined here. | — (parallel with W1) | Fabio |
| W5 | **Adapter integration + cloud-reachable execution.** Adapter invocation contracts for all six allowlisted adapters, and the decision plane EXECUTES exactly the cloud-reachable ones (`managed-agents-message`, `routine-align`); host-local adapters (`codex-exec-resume`, `openclaw-post`, `macos-notify`, `queued-wake-file`) are **enqueued with `executor: <host id>` and never executed here** — W5.5 is their sole executor. Bounded retry → dead-letter; fail-visible `wake: unroutable` lane; never spawns sessions. One component executes each adapter class, by construction. | W4 | Fabio |
| W5.5 | **Thin host executor.** A policy-free, model-free poller on each resident host with host-local adapters: reads `_coord/router/queue/` entries whose `executor` matches its own id, executes via the local adapter script, records the outcome (delivered ⇒ idempotency-keyed delivery-record shard; exhausted ⇒ dead-letter transition, which it owns as claim-holder). It is the SOLE executor for host-local adapters; it executes only what the decision plane resolved (`executor` field), claims by id-match + `claimed_at`, and is **safe under at-least-once retry because W5's adapter content rule is enforced** (wake payloads are keyed check-your-queue nudges — duplicates converge; see §2 delivery guarantee). **W5.5 acceptance explicitly includes the duplicate-delivery/content-safety test:** execute the same queue entry twice, assert the payload carries no per-event command and the observable effect converges to one bus check. No policy, no config authority, ~small enough to read in one sitting; a dead executor leaves wakes visibly queued. | W4, W5 | coord-maintainer |
| W6 | **Proposed-adapter legs.** Queued wake file + SessionStart-hook consumption (local/desktop lane); cloud routine-alignment leg (align queued work to the agent's self-armed Routine cadence — no exact-session wake exists, none invented). | W4 | codex-coder |
| W7 | **Shadow mode + acceptance (deterministic, measured against live delivery).** Router runs read-only ≥48h logging a decision (`interrupt`/`batch`/`defer`/`debounce`/`unroutable`) for every item in the comparison population = directed items (task/tell/review with a concrete assignee or held role) appearing in the store during the window. **Live-delivery evidence source: a model-free delivery probe, instrumented for the shadow window** — the existing listener/wake success paths (listener-tick / the fleet loop / adapters) write a tiny evidence shard to the router-owned namespace `_coord/router/shadow-evidence/<agent>-<hash>.json` `{key, agent, delivered_at, path: listener|adapter|watchdog}` at the moment delivery succeeds (a shard write, zero model tokens; instrumentation ships as part of W7 and is removable after acceptance). Ack/respond shards are NOT delivery evidence — the engine writes them only on explicit `inbox --ack`/`respond`, so they timestamp later agent processing; they remain in the report as a separate end-to-end consumption metric. Correlation on the idempotency key (`source-shard-id:agent`), window = one poll interval + 5 min; requires W8 landed so the baseline is not the deaf-listener degenerate case. Outcome classes per item: `matched` (router interrupt ≤ probe delivery time + window), `policy-divergent` (router chose batch/defer/debounce where the probe shows prompt live delivery — EXPECTED, counted, itemized), `lagged` (router interrupt later than probe delivery by > window), `missed` (probe delivery evidence exists, router had no decision), `phantom` (router wake decision with no store item). **Zero-tolerance: `missed` and `phantom`; PASS additionally requires zero `lagged` at p95 (p95 interrupt-decision latency ≤ poll interval + 2 min vs probe delivery times), 100% of the population classified. The report ALSO enforces the duty-cycle gate from §2.5: uptime ≥ 95%, max gap ≤ 90 min — cloud hosting's honest cost, gated not just reported; latency bounds reference the fixed 60s poll constant.** The report is machine-generated from shadow log + probe evidence shards (+ ack/respond as the separate consumption metric) so two reviewers mechanically reach the same verdict. | W4, W8 (W5 for enablement) | Fabio, reviewed by coord-boss |
| W8 | **`listen` head-of-line fix** (P1, escalated 2026-07-22, two-agent confirmation). Same budget treatment #441 gave briefing/needs-me: directed-to-caller items scan first under a dedicated budget; degraded output stays fail-visible. Ships independently of the router — the fleet's listeners are deaf without it. | — (parallel, URGENT) | codex-coder |
| W9 | **Fleet bot-account PAT custody.** Blocked on Ash creating the dedicated fleet machine account (FulcraBot is reserved for Fulcra-side repos, not this fleet — operator decision 2026-07-22; interim = operator credentials, status quo). Then: fine-grained per-repo PAT, router/env-config custody, rotation cadence documented, attribution conventions in AGENTS.md. | — (parallel; blocked on account creation) | coord-boss + Ash |
| W10 | **Drawdown + re-arm.** Per-agent listener cadence reduction to safety-net (only for agents with a working adapter — unroutable agents keep full cadence), escalate sweeps re-armed fleet-wide (the Phase-2 disarm ends here). Requires: W2 shipped + mixed-fleet gate satisfied + W7 acceptance + **W8 landed (cadence must never draw down onto a known-deaf listen path)** + **W3 landed (TTL role release precedes escalate re-arm)** + **for every agent whose working adapter is host-local: W5.5 deployed on that host and evidenced by its delivery records** (no drawdown onto an undeployed execution plane). | W2, W3, W5, W5.5, W7, W8 | coord-maintainer + coord-boss |

Automation-skill doctrine update ("a listener loop must never die on degradation") rides with W8's
PR — same surface, one review.

## 2. Schema diffs (normative for W1–W4)

- **Presence shard:** `engagement: {mode: resident|session|occasional, until: <iso8601|null>}`;
  absent field ⇒ `resident`. `until` required iff `mode: session`.
- **Cursor + durable dedup** (`_coord/router/cursor.json`): `{watermark: <iso8601Z>,
  processed: {<idempotency-key>: <first-seen iso8601Z>, …}}`. The watermark is the store-listing
  mtime high-water mark (monotonic: never written backwards); `processed` is the durable
  idempotency ledger — key = `<source-shard-id>:<agent>` — retained for ≥ retention-window of the
  source shards, so a rollback/failover replay of any already-processed shard is a no-op by
  ledger lookup, not by timestamp guesswork. **The scan predicate is tie-safe by construction:**
  store mtimes are minute-granular, so equal-mtime shards are the common case, and a strict
  `mtime > watermark` scan would skip forever any same-minute shard that landed after checkpoint.
  Each scan therefore rescans **inclusively** (`mtime >= watermark`) and relies on the processed
  ledger to suppress already-handled keys — an unprocessed equal-mtime shard is picked up on the
  next pass, a processed one is a ledger no-op. Ledger retention must consequently always cover
  the watermark minute (it does — retention-window ≫ 1 minute). W4's tests pin the equal-mtime
  case explicitly: two shards same mtime, one processed pre-checkpoint, the other must surface on
  the next scan. Checkpoint write is atomic (write-temp + overwrite) after each batch; on
  missing/corrupt cursor the router restarts in observe-only and reports.
- **Router config** (`_coord/router/config.json`): per-agent
  `{priority_floor: "P1"|"P2"|"P3", debounce_min: <int, minutes>, adapter: <enum — exactly one of
  the §W5/W6 allowlist: "codex-exec-resume"|"openclaw-post"|"managed-agents-message"|
  "macos-notify"|"queued-wake-file"|"routine-align">, adapter_args: <per-adapter allowlisted keys
  ONLY — e.g. codex-exec-resume: {thread_id}; openclaw-post: {endpoint_name} (resolved from local
  adapter config, never a raw URL from the store); managed-agents-message: {session_ref};
  free-form keys are a config validation error>, active_hours?: {window: "HH:MM-HH:MM",
  tz: <IANA name>}}`; absent agent ⇒ observe-only (shadow) — enablement is explicit per agent,
  never default. Per the relay contract, no config field may carry commands, permission modes, or
  session keys; adapters resolve secrets host-side.
- **Wake-queue entry** (`_coord/router/queue/<agent>-<hash>.json`): `{agent, reason, source_shard,
  priority, queued_at, not_before, adapter: <config enum>, executor: <logical executor id —
  "decision-plane" or a host id from the config's executor allowlist>}` (all timestamps iso8601
  UTC) — idempotency key as in cursor. **The decision plane resolves `adapter`/`executor` from
  config at enqueue time** — that is the trusted routing source; executors never choose. An
  executor claims exactly the entries whose `executor` matches its own id and stamps `claimed_at`.
  **Delivery guarantee, stated honestly: the store has no atomic claim/CAS, so side effects are
  AT-LEAST-ONCE** — a crash after the adapter fires but before the delivery record lands, or two
  processes under one executor id, can re-execute. The system is safe under that guarantee by
  ADAPTER CONTENT DESIGN, which W5 must enforce and test: every wake payload is a
  "check your queue/bus" nudge carrying the idempotency key and NO per-event command or payload —
  so N deliveries of the same wake converge to one bus check (semantically idempotent effect;
  duplicate notifications are accepted noise). Discipline, not proof, bounds duplicates: exactly
  one executor process per executor id (local pidfile), and executors skip entries with a foreign
  fresh `claimed_at` (< 10 min) — a stale claim is retryable, which is at-least-once again, which
  is safe by the content rule. Delivery records are bookkeeping (idempotency-keyed,
  self-overwriting), never the safety mechanism.
- **Dead-letter** (`_coord/router/dead-letter/<idempotency-key>.json`): queue entry +
  `{attempts, last_error, gave_up_at}`. **Owned by whichever executor holds the claim** when
  bounded retries exhaust; keyed by the idempotency key, so a duplicate transition under
  concurrent retry is a self-overwrite no-op.
- **Delivery records** (`_coord/router/delivered/<idempotency-key>.json`): one shard per
  successful execution, written by the executing component only — idempotency-keyed, so the
  single-writer-per-key rule holds by construction across hosts and the decision plane.
  `_coord/router/delivered.json` is a **decision-plane-owned folded view** regenerated
  deterministically from the delivery-record shards (per-agent `{last_delivered_at, count,
  last_source_shard}`) — sole writer: the decision plane; no executor ever touches it. Dedup
  authority stays with the cursor ledger, never these files.

## 2.5 Hosting model (operator-decided 2026-07-22): cloud-first, hardware-optional

**The decision plane is cloud-hosted; resident hardware is never mandatory.** Ash's rule,
generalized: preferring cloud Claude/Codex sessions as hosts means adopting this system is never
blocked on anyone's hardware — a new fleet must be standable-up entirely from a phone (spin up
cloud sessions; env-config secrets; setup-script process re-arm; self-armed Routines). Concretely:

- **Decision plane (W4)** runs in a cloud session environment — designated: the
  `coord-fable-worker` environment (already carrying the setup-script + Routine + env-config
  stack). **The parent spec's hosting sentence and namespace-writer model are updated in this same PR**
  (operator decision, Ash 2026-07-22) so exactly one authoritative contract exists — spec and plan
  gate together on this head; every other spec contract stands. **Cadence, defined:** the router's poll interval is a
  FIXED constant `60s` while the process is alive (the acceptance latency bounds reference this
  constant — it is not tunable per report); the process runs continuously while the container
  lives, the setup script re-arms it at container creation, and the hourly Routine is the re-arm
  floor after reclaim. Duty-cycled liveness is accepted, *measured*, and **gated**: W7 acceptance
  additionally requires decision-plane uptime ≥ 95% over the 48h window AND max single gap ≤ 90
  min (Routine floor + margin) — an hour-scale outage pattern fails the gate rather than hiding
  in an averaged report. The tie-safe cursor makes gaps lossless — a down router costs latency,
  never events, and the gate bounds how much latency the fleet accepts.
- **Execution plane** is wherever an adapter physically lives: host-local adapters
  (codex-exec-resume, macos-notify, openclaw-post) are fired by a **thin executor** (W5.5) on
  their resident host — policy-free, so a flaky desktop only delays its own adapters' wakes,
  visibly, in the durable queue. Cloud-reachable adapters fire from the decision plane directly.
- **Genericization rule for every task in this plan:** no mandatory component may require
  resident hardware; anything host-local must be an optional accelerator with a queue-visible
  degradation mode. (This is also the posture that ports to community adopters — BUS-83.)

## 3. Mixed-fleet gate (CONCUR condition 4, operationalized)

No vacancy/escalation semantic change activates until every **live** roster agent either (a) beats
with an `engagement` field, or (b) appears in an explicit defaults map
(`_coord/router/engagement-defaults.json`, operator-approved) assigning its mode. The gate is a
deterministic check (`engagement gate <team>` prints COVERED/UNCOVERED per agent); W10 cites its
output as evidence. Stale/dead legacy shards cannot block the gate (they're pruned by retention or
defaulted) — only live-and-uncovered blocks.

## 4. Rollout order & reversibility

W8 immediately (deaf listeners are the live incident) — and it now hard-gates W10. W1→W1.5→W2→W3
as the engine track; W4→W5/W6→W7 as the router track, in parallel. W9 anytime. W10 last, gated
three ways. Every step reversible: inert schema (W1) is a no-op to ignore; the sweep (W3) and
router (W4+) are processes you stop; drawdown (W10) is a cadence setting restored by re-running
the installer. Shadow-mode divergence (W7) is the single go/no-go artifact for the only step that
changes delivery behavior.

## 5. Out of scope (unchanged from spec)

Webhook receiver implementation, cross-account federation (BUS-78), tracker-bridge changes, ATC
anything, replacing listeners entirely (safety-net cadence is permanent).

## 6. Open confirms (non-blocking, ride with this plan's review)

1. ~~Router host~~ — **RESOLVED 2026-07-22 (Ash): cloud-first.** Decision plane in the
   `coord-fable-worker` environment; host-local adapters via the W5.5 thin executor. See §2.5.
2. ~~Session TTL default~~ — **RESOLVED 2026-07-22 (Ash): `join + 8h`**, encoded in W1 as the
   default `until` when `--engagement session` omits it. Paired semantics (same decision): expiry
   ⇒ LAPSED indefinite reduced-cadence check-in, park explicit-only — see W3 and spec Part B.
