# Changelog

All notable changes to **fulcra-coord** — the shared agent-coordination layer
that lets independent agents (Claude Code, Codex, OpenClaw, ChatGPT, CI)
coordinate durable tasks over Fulcra Files as a bus, with no shared memory or
direct calls.

Format loosely follows [Keep a Changelog](https://keepachangelog.com/); released
versions are sourced from `fulcra_coord/__init__.py::__version__`.

---

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
