# Changelog

All notable changes to **fulcra-coord** — the shared agent-coordination layer
that lets independent agents (Claude Code, Codex, OpenClaw, ChatGPT, CI)
coordinate durable tasks over Fulcra Files as a bus, with no shared memory or
direct calls.

Format loosely follows [Keep a Changelog](https://keepachangelog.com/); released
versions are sourced from `fulcra_coord/__init__.py::__version__`.

---

## [Unreleased] — `ensure-codex-watch`: Codex coordination self-heals on every app start

**Why:** Codex's durable per-agent inbox listener was only ever installed if an
operator manually ran `install-listener`. On a fresh Codex machine that never
happens, so Codex silently never heard directed work on the bus — it had hooks
but no listener, and nobody noticed until directed work went unanswered.

**What:** a new `ensure-codex-watch` command — the single idempotent "make Codex
coordination self-healing" entry point. It composes the already-hardened
installers (`install_codex` + the per-agent `install_listener`), best-effort
`launchctl load`s the listener plist (`--no-load` to skip; macOS/launchd only),
and optionally refreshes presence (`--no-connect`). The Codex SessionStart hook
now backgrounds it on every app start, so a missing listener self-heals with no
operator action. Fully fail-safe: a failed load or connect is warned but the
command still returns 0 (it runs backgrounded at every SessionStart and must
never hard-fail the hook), and it is safe to run repeatedly (the underlying
installers are idempotent; an already-loaded launchd job is a harmless no-op).
The SessionStart hook keeps its `connect --can-review` (Codex is the canonical
review target) and now derives a `codex:*` fallback identity instead of
`claude-code:*` so a pre-handshake box arms the right agent's listener.

---

## [0.9.1] — `remote.list_files` normalizes the real CLI output to clean paths

**Why:** `remote.list_files(prefix)` returned the raw `fulcra file list` display
lines verbatim. The real `fulcra file` CLI formats each line for humans as
`"383B    2026-06-06 01:52AM UTC  claude-code-mac-fulcra-coord.json"` (size +
date + FILENAME-only) — that string is not a path. Every list-based consumer then
fed it straight into `remote.download_json` / `remote.delete`, which silently
returned None / no-op in LIVE: self-heal (`_load_summaries_for_rebuild`'s
vanishing-directive recovery, `_reconcile_presence`), presence prune/reconcile,
retention pruning of dead health/presence records and digest markers,
`search --archived`'s archive index, and the new 0.9.0 health command all read
nothing. The whole test suite stayed green because the fake backend emits clean
full paths, never the real formatted shape — so the bug was invisible to tests
and only surfaced by live-smoking the 0.9.0 health command.

- **`remote.list_files` now normalizes each output line to a clean full remote
  path**, robust to BOTH the real CLI's formatted line and an already-clean path.
  It takes the last whitespace-delimited token as the filename (safe because
  filenames are slug/id-based with no spaces), passes through anything already a
  full path or already prefixed, and otherwise joins the bare filename onto the
  prefix. The real CLI and the fake backend now converge on identical clean paths.
  Best-effort contract preserved (returns `[]` on error). No consumer changed —
  they were all already correct given clean paths.

---

## [0.9.0] — Coordination-system health surface: a silently-failing reconcile becomes visible

**Why:** the operator had rich awareness of *task* state but ZERO awareness of
whether the coordination machinery itself was healthy. A real incident: reconcile
failed on every heartbeat (a `KeyError` on a malformed bus task), views silently
went stale, retention never ran — and nothing surfaced it. It was caught only by
a manual live smoke. This release makes a degraded bus visible.

- **Self-reported per-host health record.** On a SUCCESSFUL reconcile (views
  rebuilt + uploaded with `failures == []`), each host writes
  `health/<slug>.json`. Staleness of that self-report IS the degradation signal
  (the same mechanism as presence liveness). The write is a separate,
  failure-isolated upload placed AFTER reconcile's `if failures: return 1` guard —
  so a failed reconcile leaves its record stale (the contract), and a flaky
  best-effort sub-pass can't suppress a healthy heartbeat.
- **`views.assess_infra_health` (pure).** Judges newest `reconcile_at` per host:
  healthy / degraded (default = heartbeat interval × 3) / outage (default ~3h).
  A host with no record = "not reporting" (informational, never a false alarm).
  Duration / repair-backlog / bus-size are surfaced as metrics, NOT gated.
- **`fulcra-coord health [--format table|json]`** dashboard + a fleet-health fold
  into `doctor` + a one-line infra summary in the twice-daily operator digest
  (the digest scheduler is independent of reconcile, so it reports a broken
  reconcile even on a single-host box — v1's push surface).
- **Retention prunes `health/`** on the dead-presence window, so a decommissioned
  host's records disappear in lockstep with its presence record.
- **Knobs:** `FULCRA_COORD_HEALTH_DEGRADED_SECONDS`,
  `FULCRA_COORD_HEALTH_OUTAGE_SECONDS`.

---

## [0.8.3] — Reconcile + write paths survive imperfect bus data; review sweep is deadline-bounded

**Why:** an adversarial sweep of the 0.8.2 bus surfaced a family of crashes that
all share one root cause — code that bracket-indexes a task/aggregate body for a
field (`id`, `agent`) that an older or imperfect write can omit. Because several
of those accesses sit on the **reconcile** and **write** paths (which run on every
heartbeat tick and every create/update/done/tell), a single malformed body could
take down the whole tick — the same heartbeat-outage class as the just-fixed
`build_search_index` incident. This release makes those paths tolerant of a body
missing a field, and additionally bounds the review-route sweep to the reconcile
deadline so it can no longer starve the retention pass.

- **reconcile no longer crashes on an id-less active expired-claim body (A1).** The
  stale-claim scan in `cmd_reconcile` used `t["id"]` with bracket access; a real
  body with `status=="active"` + an expired `claim.claim_expires_at` but **no
  `id`** raised an uncaught `KeyError` (the inner `try` only guarded `ValueError`
  from `fromisoformat`) — and it ran *before* `build_all_views`/upload, so the
  whole reconcile aborted and every heartbeat tick failed. The loop is now a
  dedicated `_detect_stale_claims(all_tasks, now)` helper that skips id-less
  bodies and parses the expiry via `views._parse_dt` (never lexical).

- **every write command no longer crashes on an id-less cached body (A2).**
  `_load_all_tasks` built `{t["id"]: t for t in cached}` and
  `task_map[t["id"]] = t` with bracket access; an id-less cached body raised
  `KeyError` that propagated uncaught through `_load_summaries_for_rebuild` →
  `_write_task_and_views`, crashing create/update/done/tell/… Both now skip a body
  with no `id` (it has no stable key anyway) and keep the well-formed tasks.

- **`presence` tolerates an agent-less aggregate entry (A3).** `cmd_presence`
  rendered `f"... {a['agent']} [{a['liveness']}] ..."` with bracket access, but
  `build_presence` carries records through verbatim and never injects `agent` — so
  an agent-less aggregate entry crashed the command. It now uses `.get(...)` and
  the well-formed entries still render.

- **the review-route sweep is deadline-bounded (B1).** `_sweep_review_routes`
  loops O(review-directives) with a per-item network fetch + potential full
  view-rebuild write and had **no** time check, so it could run past reconcile's
  ~90s deadline and starve the retention pass (which already gates on the
  deadline). It now threads reconcile's `deadline` through and stops processing
  further directives once the budget (minus a small headroom) is spent, logging
  how many were deferred. Best-effort: deferred directives drain next tick;
  `deadline=None` keeps the old unbounded behavior for direct callers.

- **archive verifies the hot copy before moving (B2).** `_archive_task` would
  upload a phantom archive body + shard for a task that existed **only** in this
  host's stale local cache (deleted remotely by another host, never archived
  here). It now requires a positive stat of the hot `tasks/<id>.json` before the
  move — unless the archive body already exists (the idempotent / crash-recovery
  finish) — and otherwise skips the phantom and evicts it from the local cache so
  it stops being reloaded. Mirrors the reroute sweep's `if fresh is None: continue`
  guard; idempotency and the crash-safe ordering are unchanged.

All best-effort paths still never raise into a reconcile/scheduled tick. Datetime
comparisons go through `views._parse_dt` (never a lexical string compare).

---

## [0.8.2] — Hermetic test cache: tests can no longer pollute the real bus

**Why:** `cache.cache_root()` resolves to `${XDG_CACHE_HOME:-~/.cache}/fulcra-coord`.
Any test that hit a cache-writing path (`write_cached_task`, `_write_task_and_views`,
`cmd_reconcile`, `_sweep_review_routes`, …) *without* first redirecting
`XDG_CACHE_HOME` wrote straight into the **operator's real** `~/.cache/fulcra-coord`.
Most test classes set `XDG_CACHE_HOME` by hand in `setUp`, but several did not —
notably the reviewer-routing sweep tests — and their fixtures (`author:h:r`,
`dead:h:r`, a title-less `TASK-20260604-rev-00000000`) leaked into the real cache.
`reconcile` then read that polluted local cache and **pushed the junk tasks to the
live coordination bus**, where they crashed reconcile. A prior run left 127 stray
tasks in a developer's real `~/.cache` the same way. This is a correctness fix:
the test suite must never touch the operator's cache or bus.

- New `tests/conftest.py` with an **autouse, function-scoped** fixture that points
  `XDG_CACHE_HOME` at a fresh per-test temp dir for **every** test and restores the
  prior value afterward. The cache is now hermetic by default — no test can write to
  the real `~/.cache/fulcra-coord` regardless of whether it remembered to isolate.
  Function scope (not session) so tests never share cache state, matching the
  per-test isolation the careful tests already did manually.
- The same fixture defaults `FULCRA_COORD_BACKEND` to `false` when unset, so a test
  that reaches an unmocked remote file-op can't shell out to the real `fulcra` CLI
  and touch the live account. Tests that inject their own backend (the stateful fake,
  or an explicit `backend=` arg) override this freely — no existing test changed.
- Added `TestCacheIsolationHermetic`: proves `cache_root()` resolves under the temp
  dir (never the real `~/.cache`) and that a cache write from inside a test leaves the
  real home cache byte-for-byte unchanged.
- Existing per-`setUp` `XDG_CACHE_HOME` juggling is left in place — it's now harmless
  (it runs inside the fixture's redirected world) and removing it would be churn.

## [0.8.1] — Reconcile no longer crashes on a malformed task

**Why:** `build_search_index` read `task["id"]` / `task["title"]` with bracket
access. A single real task body missing `title` (or `id`) raised `KeyError`
straight out of the aggregate rebuild — aborting the entire `reconcile`: views
weren't repaired and the new retention pass never ran (its marker was never
written). Caught by a live smoke of 0.8.0 against the real 140-task bus
(`ERROR: 'title'`), which a from-scratch test env never exercises.

- `build_search_index` now uses `.get()` defaults for every field (a malformed
  task surfaces in the search index with empty strings instead of crashing the
  rebuild) — the same render-don't-crash contract `task_summary` got in the
  0.5.6 debug sweep. Regression test added.

## [0.8.0] — Bus Retention / Archival

**Why:** The coordination bus grew without bound. Terminal (done/abandoned)
tasks stayed under `tasks/` forever — bloating `views/summaries.json` (the
aggregate behind the read perf win), growing the `tasks/` listing self-heal
enumerates on every write, and swelling recently-done/search. Digest markers and
dead-agent presence records also accumulated. Reads and reconcile slowly
degraded and the operator surfaces got noisier; nothing removed anything.

**What:**
- Terminal tasks aged past `FULCRA_COORD_RETENTION_DAYS` (default 30) are
  crash-safely MOVED to `archive/tasks/<YYYY-MM>/<id>.json` with an append-only
  per-id cold-index shard `archive/index/<id>.json` (no shared mutable index —
  Files has no CAS). Moving the body out of `tasks/` removes it from the
  aggregate, views, and self-heal automatically — zero read-path filter code.
- `search --archived` (alias `--all`) scans the cold index; default search stays
  hot-only and fast. `restore <id>` moves an archived body back into `tasks/`.
- Spent digest markers (>`FULCRA_COORD_MARKER_RETENTION_DAYS`, default 7) and
  dead-agent presence (>`FULCRA_COORD_PRESENCE_RETENTION_DAYS`, default 30) are
  soft-deleted via `fulcra file delete` (platform-restorable). Both prune gates
  fail SAFE: an undatable marker or presence record is kept, never deleted.
- The pass is folded into `reconcile`, self-throttled to ~once/day via a
  first-host-wins `retention/last-run.json` marker (the digest-marker pattern) —
  no new scheduler. Bounded by `FULCRA_COORD_RETENTION_MAX_PER_RUN` (default 200)
  + a time budget that composes with reconcile's deadline; best-effort
  (never raises into a tick). No data loss by construction (write→verify→delete).

**How tested:** new `tests/test_retention.py` — policy predicates (cutoff
boundaries, non-terminal exclusion), crash-safe move (write→verify→delete order,
crash-mid-move completion, idempotency), append-only shards, `search --archived`
/ `restore`, throttle + cap + time-budget + best-effort, marker/presence prune,
and a VERIFIED automatic-hot-path-exclusion test that archives a terminal task
and asserts it leaves the rebuilt `tasks/` listing and summaries with no filter.

**Fix (local-cache resurrection):** the archive MOVE deleted the remote
`tasks/<id>.json` but left the body in the *local* cache of the host that ran the
archive. Because `_load_all_tasks` (the reconcile load path) seeds its task map
from `cache.list_cached_tasks()` and only ever ADDS remote ids — never removes —
that host's very next `reconcile` rebuilt the archived task straight back into the
authoritative `views/summaries.json`, re-surfacing it across the fleet (and it
stayed an archive candidate forever, re-archived as an idempotent no-op each day).
The hot-path exclusion held for *other* hosts but not the archiving one.
`_archive_task` now evicts the local cache entry (`cache.delete_cached_task`) as
the final step of a verified move. Covered by a unit test (eviction) and an
end-to-end `reconcile` regression test that archives a locally-cached terminal
task and asserts it does not reappear in the rebuilt summaries.

---

## [0.7.0] — Liveness-Aware Reviewer Routing

**Why:** PR-review directives were routed to a FIXED reviewer (canonical, or a
configured #devops fallback) regardless of whether that agent was online. PRs
sat unreviewed in a stale fallback's inbox while a capable reviewer was idle the
whole time, and nothing re-routed a directive once its assignee went dark.

**What:**
- `request-review <pr> --repo <repo>` routes a PR review to a reviewer presence
  says is actually live/idle (capability-based pool: canonical reviewer seed +
  agents that declared `--can-review`), tagging the directive `kind:review` and
  recording a `routed` event. `--dry-run` shows the ranked pool/tiers/winner.
- `connect --can-review` / `--role` declare an agent's capabilities on its
  presence record (default `[]`, backward compatible).
- `reconcile` now sweeps stalled `kind:review` directives: re-routes a never-
  acted review whose assignee fell below liveness floor (P1 15m / P2 30m,
  env-overridable; cap 2; then escalate to the human), and freezes one the
  assignee explicitly accepted, escalating only after a long stall.
- `tell --route-capability R [--floor live|idle]` exposes the underlying
  route-to-live primitive for any directive.
- Escalation (no live reviewer) lands on the human's plate via the existing
  `block --on-user` / needs:human surface. New env knobs:
  `FULCRA_COORD_PRESENCE_GRACE_SECONDS` (1200), `…REVIEW_REROUTE_MINUTES_P1/P2`
  (15/30), `…REVIEW_REROUTE_MAX` (2), `…ACCEPTED_STALL_HOURS` (2).

---

## [0.6.0] — Operator Digest

**Why:** the human surface was pull-only — you saw "what's blocked on me" / "what
is everyone doing" only when you started a session or ran needs-me/agents/resume.
Between sessions you were blind, and the granular per-event annotations were too
fine-grained to read as a glance. The Operator Digest is the push side: a
consolidated, human-paced situational-awareness summary delivered to your Fulcra
timeline twice daily and on demand.

- **`fulcra-coord digest [--window morning|evening] [--format json] [--dry-run]`** —
  builds a four-block digest from existing bus state + presence (blocked on you,
  upcoming, what each agent did since the last window, what's stale) and writes it
  to the timeline. `--dry-run` renders without writing; `--format json` prints the
  structured digest.
- **New "Agent Tasks — Digest" timeline track** — a second moment definition,
  separate from the granular per-event "Agent Tasks" track (which is kept,
  untouched), so digests filter on their own.
- **Any-agent, dedup-guarded** — any machine can run the digest; a first-writer-wins
  marker (`digest/markers/<date>-<window>.json`) collapses concurrent runs to one
  digest per window (a rare same-second double is accepted as harmless, since
  Fulcra Files has no compare-and-swap).
- **`fulcra-coord install-digest`** — schedules the digest twice daily (launchd
  08:00/18:00 on macOS, cron elsewhere). Safe to install on every machine.
- **Per-event annotations now carry work substance** — the note reads
  `[<workstream>/<kind>] <title> — <summary> · next: <action>` instead of just the
  lifecycle category. Note-body only; backward-compatible.

All digest paths are best-effort: a failed read/marker/emit never raises into a
scheduled tick. Datetime comparisons (the `since` window + due ranking) parse
timestamps (consistent with the 0.5.x mixed-precision fix), never lexical compare.
## [0.5.6] — Debug sweep, rounds 2-3

**Why:** a second adversarial pass focused on timestamp precision, malformed task
bodies, human-blocking tags, annotation cache drift, and hook command hygiene.

- **Timestamp precision:** all new coordination timestamps now emit fixed-width
  microseconds, and freshness decisions parse datetimes instead of comparing raw
  strings, so mixed-precision values already on the bus cannot silently drop the
  newer side of a merge or rebuild.
- **Malformed task bodies:** a cached task body with missing display fields now
  surfaces in rebuilt views with empty-string defaults instead of vanishing from
  every materialized view.
- **`needs:human` cleanup:** assigning a human-blocked task away from the human
  strips the stale `needs:human` tag; assigning it back to the human preserves
  the marker.
- **Annotation cache TTL:** cached definition/tag ids expire after 24h by
  default (`FULCRA_COORD_ANNOTATION_CACHE_TTL_SECONDS` override), bounding drift
  after server-side deletes or renames while keeping annotation emission
  best-effort.
- **Hook command hints:** SessionStart resume hints shell-quote the resolved
  `fulcra-coord` command and agent id before printing copy-pasteable commands.
- **Reviewer-caught (codex):** the summaries rebuild path had one remaining raw
  `updated_at` string compare; it now uses the same parsed timestamp key as the
  merge path.

## [0.5.5] — Reconcile performance

**Why:** `reconcile` ran serially — each task's body load and each materialized
view upload happened one round-trip at a time — taking ~96s end to end. That
overran the heartbeat's timeout, so the heartbeat kept dying and presence/views
went stale.

- Per-task body loads and view uploads now run in parallel
  (`ThreadPoolExecutor`), cutting reconcile from **~96s to ~23s** and
  un-breaking the timing-out heartbeat. Partial-failure, timeout, and exit
  semantics are preserved — a failed leg still degrades gracefully rather than
  aborting the whole pass.
- **Deadline fix (reviewer-caught, codex):** each parallel view upload was
  handed the full per-call timeout instead of the *remaining* budget, so an
  upload starting near the limit could still block for the full timeout and
  blow the overall reconcile deadline. Now a global deadline
  (`t0 + timeout`) is computed once and each upload gets `max(1, remaining)`,
  skipping outright once the deadline has passed.

*(0.5.2 was not released standalone — the branch version bumped to 0.5.5 during
the stacked merges below.)*

## [0.5.4] — Debug sweep, round 1

**Why:** a focused audit of the merge / optimistic-concurrency and self-heal
paths turned up 12 confirmed bugs where state could be silently dropped,
misclassified, or mis-timed.

- **Merge / optimistic concurrency:** `_try_merge` now carries forward fields it
  was dropping and unions the `event` / `acked` collections instead of letting
  one side overwrite the other.
- **Summaries aggregate:** the rebuild self-heals a dropped directive rather than
  persisting the loss.
- **needs_human / scheduling gates:** datetime comparisons now parse before
  comparing and coerce naive timestamps to UTC, so `not_before` / `due` gates
  fire on the right boundary instead of throwing or comparing apples to oranges.
- **Inbox auto-aging, presence:** both self-heal correctly under the cases the
  sweep exercised.
- **Annotations HTTP writer:** the tag-id cache and `recorded_at` anchoring were
  corrected so timeline moments land at the right time with the right tags.
- **Stale derived-tag repair (reviewer-caught, codex):** a safe merge that
  combines local status with newer remote fields left behind a stale
  `status:<old>` tag, misclassifying the task in tag-filtered views.
  **`_repair_merged_tags`** rebuilds the standard tags from the merged fields
  while preserving non-standard ones (e.g. `needs:human`).

## [0.5.3] — Per-agent listener

**Why:** the listener's launchd/cron identity was machine-global
(`com.fulcra.coord.listener`), so two agents co-located on one host clobbered
each other's listener job — installing one tore down the other's.

- The listener label / plist / cron-marker now derive from the agent slug
  (`com.fulcra.coord.listener.<slug>`), so each co-located agent gets its own
  job. Install, uninstall, and the cron strip are all agent-scoped.
- A legacy un-slugged plist is **superseded only for the agent it watched**, and
  symmetrically on both install **and** uninstall — a reviewer-caught (codex)
  install/uninstall asymmetry that would otherwise have left an orphaned legacy
  job behind.
- The **heartbeat stays a singleton** — only the listener is per-agent.

## [0.5.1] — Inbox auto-aging

**Why:** informational broadcasts ("X joined the mesh", "identities live") linger
as proposed directives in every agent's inbox forever (broadcasts never get
"done"), piling up and burying real directives at SessionStart.

- The inbox view now hides a **broadcast** (`assignee="*"`) that's still
  `proposed` and older than `FULCRA_COORD_INBOX_AGE_DAYS` (default 3). Pure
  read/view filter — task status, the task file, and the aggregate are untouched
  (nothing is abandoned; a peer on an older CLI still sees it).
- **Directives addressed to a concrete agent are NEVER aged out** regardless of
  age — a real ask stays until acked/done.
- `inbox --all` shows everything; the default prints `(N older broadcast(s)
  hidden — --all to show)`. The SessionStart banner inherits the filter.

## [0.5.0] — Scheduling for the "blocked on you" plate

**Why:** an agent could block a real task on the human that wasn't *actionable
yet* — e.g. "re-auth this 7-day OAuth token" five days before it expires. The
human then saw "⛔ BLOCKED ON YOU" at every session for days for something they
couldn't do. Operator's words: *"every agent keeps telling me about this task but
it's not relevant yet."*

- **`block --on-user "<ask>" [--not-before <when>] [--due <when>]`** — schedule
  when an ask becomes relevant. `<when>` accepts an ISO date/datetime or a
  relative offset (`5d`, `36h`, `10m`).
- **`needs-me`** now shows only **due-now** items, then a compact
  **"Upcoming (next 7d)"** section (`[in 4d] … (due Jun 8)`). `--all` expands
  upcoming inline; `--format json` returns `{human, count, items, upcoming}` with
  `count` counting due-now only.
- **SessionStart banner** counts only due-now items in the `⛔ BLOCKED ON YOU (N)`
  headline and appends a muted `(+N upcoming)` — a future-only plate shows no
  alarm headline at all.
- New optional task fields `not_before` / `due` (carried through `task_summary`,
  so view rebuilds stay equivalent). Backward-compatible: tasks without them
  behave exactly as before.

## [0.4.1] — Self-heal: stop directives vanishing from the bus

**Fixed (critical):** under concurrency, directives could silently disappear from
`inbox` / `status` / `agents` for minutes. The performance work (0.4.0) rebuilt
views from the single `views/summaries.json` aggregate; being one object under
last-writer-wins, a concurrent peer's write could clobber it and **drop a task
another agent had just created** — and because every later write re-read the
clobbered aggregate, the drop persisted until a (90s) `reconcile`.

The per-task files (`tasks/<id>.json`) are the durable, un-clobberable truth. The
write path now enumerates them and re-includes any task whose file exists but the
aggregate dropped — fetching a body only for the rare missing id (≈0 in steady
state). A dropped task now **self-heals on the very next write by any agent**
(seconds), not a reconcile cycle. Add-only and best-effort: a failed/empty
listing can never make the rebuild worse than the aggregate alone.

## [0.4.0] — Situational awareness, performance, annotations, presence

The big release. fulcra-coord's north star became *the human's situational
awareness — above all, "what's blocked on me."*

### Performance
- **Reads `agents` / `needs-me` / `resume` / `status` went from ~56s to <1s**, and
  writes from ~30s to ~7s. Root cause: every read *and* every write re-fetched
  each task body one file at a time. Introduced a `views/summaries.json` aggregate
  (one read replaces N+3 round-trips), rebuilt views from it instead of re-fetching
  bodies, and parallelized view uploads. `task_summary` was enriched
  (`last_touched_by`, `done_at`, `acked_by`) so `build_all_views(summaries)` is
  byte-identical to building from full bodies (guarded by an equivalence test).

### Situational awareness — "what's blocked on me"
- **Human operator handle** (`human [set|clear]`, default `human`, personalizable)
  — the addressable identity tasks are "blocked on ME" against.
- **Blocked-on-you surface**: `block --on-user "<ask>"` assigns a task to the
  human + tags `needs:human`; **`needs-me`** is the human's plate (who's waiting,
  what they need, how long); the **SessionStart banner leads with ⛔ BLOCKED ON
  YOU**. Broadcasts are excluded — only concrete asks surface, so the banner is
  signal, not noise.
- **`resume [--agent X]`** — a pick-up-where-you-left-off briefing (your active
  work, what's blocked on you, what you owe others, what's blocked on the human).
- **needs-user timeline annotation** + a listener that notifies on new
  blocked-on-you items.

### Agent presence
- **`connect` / `workstream` / `presence`** — agents report their current major
  workstream(s) on connect, so `agents` shows what each agent is working on **even
  with no active task**. The SessionStart hook auto-reports presence.

### Annotations (now actually work)
- Lifecycle and needs-user moments write to the operator's **Fulcra timeline via
  the HTTP API** (the path fulcra-collect uses), implemented in pure stdlib —
  resolve tags → resolve/create the "Agent Tasks" moment definition (cached) →
  `POST /ingest/v1/record/batch`. Enable durably with **`annotations on`**
  (persisted), or per-shell `FULCRA_COORD_ANNOTATIONS=http`. `doctor` reports the
  mode + token state.

### Identity & operability
- **Per-cwd identity** (realpath-keyed) so sibling sessions on one machine stop
  clobbering each other's id; legacy global `identity.json` is no longer
  auto-read (`identity migrate` to adopt it).
- **`--version`** flag + **`capabilities`** probe (advertises supported commands)
  + a dynamic version sourced from one place, so `uv tool install --force`
  rebuilds reliably.
- **`doctor` checks for the `file` command group** — the #1 fresh-agent trap
  (public PyPI `fulcra-api` lacks it), with the exact fix.
- `start` no longer requires `--agent` (auto-resolves identity like its siblings);
  onboarding hints for the identity-migrate and start-vs-claim cases.

### Conventions (documented for every agent)
- **One git worktree per session** (a shared checkout clobbers index/HEAD).
- **No direct pushes to `main`** — every change is a PR, reviewed by another agent
  (non-Arc Claude → the Codex reviewer; Arc → arc-code-review), merged by its
  author. Repo homes: `fulcra-tools` only for things that make Fulcra useful to
  others; Fulcra infra → `ashfulcra`, personal → `reversity`.

## [0.1.0] — Initial agent coordination layer

- Durable tasks on Fulcra Files: `start` / `update` / `pause` / `block` / `done` /
  `abandon`, with an optimistic-concurrency write + structured merge.
- Cross-agent **directives**: `tell` / `broadcast` and a per-agent `inbox`.
- **`agents`** status digest, materialized views, `search`, `reconcile`.
- Lifecycle **hooks** (Claude Code SessionStart/PreCompact/SessionEnd; Codex;
  OpenClaw) and a durable **listener** + heartbeat installers.
- Adapters/ONBOARD docs for Claude Code, Codex, OpenClaw, and a ChatGPT facade.
