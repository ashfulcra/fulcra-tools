# fulcra-coord

**Shared agent coordination layer using Fulcra Files as a coordination bus.**

Multiple independent agents — local Claude Code sessions, cloud agents, CI jobs, OpenClaw, Codex — coordinate durable work through Fulcra Files without shared memory, direct calls, or a central broker.

> **Agents:** read [`SKILL.md`](SKILL.md) — the discoverable, scannable guide to *when* and *how* to use coord (quick-reference + the load-bearing rules). This README is the human/reference deep-dive; `SKILL.md` is the agent entry point.

## Value proposition

- **No shared infrastructure required** — Fulcra Files is the only coordination store
- **Works across environments** — local workstations, cloud Claude Code, CI, ephemeral agents
- **No Tailscale, SSH, or workspace access needed** — just the Fulcra CLI + credentials
- **Persistent across sessions** — tasks survive agent restarts and context resets
- **Materialized views** — agents read cheap pre-built summaries, not full task history
- **Optimistic concurrency** — stat-based conflict detection, reconciler repair for partial writes

## Install

```bash
pip install fulcra-coord
# or
uv add fulcra-coord
```

Requires: Python 3.10+, and the Fulcra CLI (`uv tool install fulcra-api`). The
standard build ships the `file` command group the bus runs on
(`fulcra file list|stat|download|upload|delete`) — no special branch or build
needed.

> If `fulcra-coord doctor` reports `File commands: FAIL`, the *resolved* CLI is
> not exposing `file` — usually a stale install (`uv tool install --reinstall
> fulcra-api`) or a `FULCRA_CLI_COMMAND` pointing at a binary without it. See
> [`docs/fulcra-cli-branch.md`](docs/fulcra-cli-branch.md) to verify and
> repoint. Without `file`, every bus op fails silently.

## Quick start

```bash
# 1. Authenticate (device flow)
fulcra-api auth login

# 2. Check setup
fulcra-coord doctor

# 3. See current coordination state
fulcra-coord status

# 4. Start a task
fulcra-coord start "Deploy search service" \
  --workstream devops \
  --agent claude-code \
  --priority P2 \
  --summary "Deploy the new search microservice." \
  --next "Run terraform apply."

# 5. Update progress
fulcra-coord update TASK-... --summary "Terraform done." --next "Run smoke tests."

# 6. Snapshot without changing task state (for compaction / handoff)
fulcra-coord snapshot TASK-... \
  --reason pre-compact \
  --transcript-path /tmp/session.jsonl

# 7. Pause (session ending), optionally writing a Continuity checkpoint
fulcra-coord pause TASK-... \
  --next "Run GET /search?q=test smoke test." \
  --snapshot

# 7b. Resume with latest Continuity checkpoints included
fulcra-coord resume --with-continuity

# 8. Mark done
fulcra-coord done TASK-... \
  --evidence "Smoke tests passed, service live at search.example.com" \
  --verification-level agent-verified
```

## Commands

| Command | Description |
|---|---|
| `status` | Show current coordination state (all or filtered) |
| `agents` | Cross-agent digest: what each agent is working on, grouped by owner, stale-marked (`--mine AGENT`, `--format json`) |
| `roles` | Role registry + lease status — the durable identities sessions claim leases on. Bare `roles` lists every registered role with live HELD / VACANT / CONTESTED status (the discovery surface: learn what roles exist instead of guessing session ids); `roles set <name> [--description] [--instructions] [--policy shared\|exclusive] [--sla-hours N] [--maintainer <who>]` upserts a registry record (an update preserves fields you don't pass); `roles claim <name>` / `roles release <name>` manage this agent's own lease (`connect --role` claims automatically). See [Roles & review-routing](#roles--review-routing) |
| `tell` | Direct work at another agent: create a `proposed` directive task assigned to them (`tell <assignee> "<title>" [--from <me>] [--next] [--workstream] [--priority]`) |
| `broadcast` | Direct work at **every** agent: create a `proposed` directive with the wildcard assignee `*` (`broadcast "<title>" [--from <me>] [--next] [--workstream] [--priority]`). It lands in every agent's inbox and is acknowledged **per-agent** — one agent's `inbox --ack` clears it for that agent only, so no agent loses or duplicates the directive. Use `tell` for one agent, `broadcast` for all (e.g. "update fulcra-coord when main changes") |
| `assign` | Set or redirect the `assignee` on an existing task (`assign <task-id> <assignee>`) |
| `inbox` | List open directives addressed to you (`--agent`, `--format json`); `--ack <task-id>` marks one seen without claiming it. Stale informational broadcasts (older than `FULCRA_COORD_INBOX_AGE_DAYS`, default 3) are hidden by default and noted as a count; `--all` shows them too. Matching is prefix-aware: a directive addressed to a short id (`claude-code`) reaches the full-id agent (`claude-code:<host>:<repo>`) it prefixes |
| `identity` | Show, set, clear, or migrate this host's declared agent id — the identity handshake reused by every bus op. `identity` shows the resolved id + its source (and hints if a stale legacy global exists); `identity set <agent-id>` persists it; `identity clear` removes it; `identity migrate` copies a legacy global identity into the current repo's entry (`--format json`). **Scoped per working directory** so sibling sessions in different repos don't clobber each other's identity |
| `human` | Show, set, or clear the human operator's handle — the addressable identity tasks are "blocked on ME" against. Defaults to the neutral `human`; personalize with `human set <name>` (e.g. `human set ash`). `human clear` reverts (`--format json`). Global per machine |
| `annotations` | Enable/disable/inspect the **Agent Tasks** timeline annotations writer. `annotations on` persists `http` to `<XDG_CONFIG_HOME>/fulcra-coord/annotations` so **every agent on the machine emits** without a per-shell `FULCRA_COORD_ANNOTATIONS` export; `annotations off` removes it; bare `annotations`/`annotations status` reports the resolved mode + its source (env/config/default) and whether a token resolves — the token value is never printed (`--format json`). `FULCRA_COORD_ANNOTATIONS` still overrides per shell. Per-event notes now carry work substance — `[<workstream>/<kind>] <title> — <summary> · next: <action>` — so a single moment conveys what the task is and what's next |
| `needs-me` | **What's blocked on YOU** (the human): every open task assigned to / blocked on you across all agents, showing who's waiting, the ask, and how long it's been (`--human <handle>`, `--format json`). The "what's on my plate from my agents" glance. Asks with a future `not_before` (see `block`) are split off into a compact **Upcoming (next 7d)** section instead of the DUE-NOW plate, so a task you can't act on yet doesn't clutter it; `--all` lists each upcoming item inline. JSON returns `{human, count, items, upcoming}` — `count` reflects DUE-NOW only |
| `digest` | Write the **operator digest** — a consolidated twice-daily situational-awareness summary — to the Fulcra timeline on its own **Agent Tasks — Digest** track. Four blocks: blocked-on-you, upcoming, per-agent activity, stale (`--window morning\|evening` sets the lookback + label, omit for on-demand; `--human <handle>`; `--format table\|json`; `--dry-run`). `--dry-run` renders + prints without writing; `--format json` emits the structured digest for tooling. An any-agent dedup guard means it's safe to run from multiple machines — only the first writer per window lands a moment |
| `install-digest` | Install the twice-daily scheduled `digest` jobs (launchd 08:00 + 18:00 on macOS, fixed cron lines elsewhere) — the push side of the operator digest. Safe to install on **every** machine: the any-agent dedup guard collapses concurrent ticks to one digest per window. `--uninstall` to remove, `--dry-run` to print the plan |
| `resume` | Pick-up-where-you-left-off briefing for an agent: your active/waiting work, what's blocked on you, what you owe others, and what's blocked on the human (`--agent`, `--format json`). Add `--with-continuity` to include latest Fulcra Continuity checkpoints for active/waiting tasks. Read-only — run after a restart to reload context |
| `start` | Create a new task |
| `update` | Update summary / next_action / status |
| `block` | Mark as blocked. `--blocked-on "<reason>"` for an agent/external blocker; **`--on-user "<ask>"`** to block on the human — assigns the task to the resolved human handle, tags `needs:human`, and lands it on `needs-me` + the human's next SessionStart. Optional scheduling on an `--on-user` ask: **`--not-before <when>`** gates when it surfaces as DUE-NOW (it stays under `needs-me`'s Upcoming until then), and **`--due <when>`** is the informational deadline (drives upcoming ordering/urgency, does not gate). `<when>` is an ISO date/datetime (`2026-06-08`, `2026-06-08T18:00:00Z`) or a relative offset (`5d`, `36h`, `10m`) |
| `pause` | Set to waiting with a next_action. Add `--snapshot` to write a Fulcra Continuity-compatible checkpoint at the durable pause point without writing snapshots on every task update |
| `snapshot` | Write a Fulcra Continuity-compatible checkpoint without changing task state (`--reason`, `--transcript-path`, optional `--next`). Used by compaction/idle hooks to capture resume state at session boundaries (bounded by `FULCRA_COORD_CONTINUITY_KEEP` retention, not by suppression) |
| `handoff` | Hand work to another agent or `@role` **with its resume state**: opens a `kind=dispatch` loop whose payload carries a continuity `checkpoint_ref` (`handoff --to <agent\|@role> --title "..." [--checkpoint <ref\|file>]`). A local checkpoint JSON file is published to the bus's `continuity/` tree first (the remote path becomes the ref); an opaque ref is forwarded verbatim. The recipient's claim prints the ref + rendered resume brief. See [Continuity](#continuity) |
| `checkpoint` | Read or update a **role's** durable resume point: `checkpoint --role X --ref <ref>` points the role registry's `checkpoint_ref` at a continuity checkpoint (preserving every other field); without `--ref` it shows the current ref + best-effort resume brief. Claiming the role (`roles claim` / `connect --role`) prints the same resume. See [Continuity](#continuity) |
| `park` | Best-effort session-exit checkpoint of every role this session holds: writes a continuity checkpoint per held role via the optional `fulcra-continuity` CLI, publishes it to the bus, and updates each role's `checkpoint_ref`. Silent no-op without the CLI or held roles; **never exits nonzero** — safe for PreCompact/SessionEnd hooks (which call it, backgrounded) |
| `done` | Mark done (requires evidence) |
| `abandon` | Mark abandoned |
| `reconcile` | Repair views and resolve pending markers |
| `search` | Search tasks by text |
| `doctor` | Check configuration and connectivity |
| `install-shim` | Install CLI shim to `~/.local/bin/` |
| `install-claude-code` | Install Claude Code lifecycle hooks (global by default); add `--with-wake` to also seed this agent's host-wake entry in `wake.json` so the listener can spawn a headless session when directed work arrives (see "Host wake" below — review the written command) |
| `install-openclaw` | Install OpenClaw Track A artifacts (boot/heartbeat prompts + shutdown/bootstrap hooks); add `--with-plugin` to also materialize the Track B Plugin-SDK plugin; add `--with-heartbeat --with-listener --agent <id>` to bundle the durable bus-pickup path (reuses `install-heartbeat` + the per-agent `install-listener`) in one command, so a fresh OpenClaw agent hears directed work without a separate step (the OpenClaw analogue of `ensure-codex-watch`) |
| `install-codex` | Install Codex lifecycle hooks (SessionStart + PreCompact) into `~/.codex/hooks.json`. No Stop hook by design — Codex end-parking is delegated to the heartbeat |
| `ensure-codex-watch` | Idempotently (re)arm Codex coordination in one shot — installs Codex hooks, the per-agent inbox listener, best-effort `launchctl load`s it (`--no-load` to skip), optionally refreshes presence (`--no-connect`). Codex SessionStart runs it backgrounded each app start so a missing listener self-heals. Idempotent (`--agent`, `--set-identity`, `--can-review`, `--interval-min N`, `--dry-run`) |
| `install-heartbeat` | Install a scheduled `reconcile` heartbeat (launchd on macOS, crontab elsewhere) — the safety net that sweeps stale tasks for crashed / end-hook-less agents (`--interval-min N`) |
| `install-listener` | Install a scheduled `notify-inbox` listener (launchd on macOS, crontab elsewhere) — the durable, per-agent way to notice directed work while idle (`--agent`, `--interval-min N`, default 10). See `adapters/claude-code/LISTENER.md` |
| `notify-inbox` | Poll the inbox for an agent; if directives exist, write a surface file the next SessionStart injects and emit a best-effort notification (the call the listener runs each tick). With a per-adopter `wake.json` entry it can also **wake** the agent — spawn a configured command, throttled + single-flighted (see "Host wake") |
| `announce-version` | **Maintainer, at each release:** publish this build's version as the canonical manifest (`runtime/version.json`) with verify-after-write. The manifest is a version *pointer* (version + commit + optional `--min-supported`), never code or commands — see [Self-update](#self-update) |

All hook installers resolve a concretely-callable `fulcra-coord` invocation at install time and bake it into the materialized scripts (absolute on-PATH path, else `<python> -m fulcra_coord`), so hooks work under `uv tool` / source installs, not just `pip`-on-PATH. The committed adapter copies keep a literal placeholder.

## Configuration

| Variable | Default | Description |
|---|---|---|
| `FULCRA_COORD_REMOTE_ROOT` | `/coordination` | Coordination root in Fulcra Files |
| `FULCRA_CLI_COMMAND` | `fulcra-api` | CLI command (or `uv tool run fulcra-api`) |
| `FULCRA_COORD_TIMEOUT_SECONDS` | `30` | Read timeout |
| `FULCRA_COORD_RECONCILE_TIMEOUT_SECONDS` | `90` | Reconcile timeout |
| `FULCRA_COORD_UPLOAD_RETRY` | `1` | Reconcile's parallel view-upload pool retries a failed view ONCE after a 0.5–2s jitter sleep — only when there is real deadline headroom (jitter + 1s per-upload floor + 2s slack), so the deadline stays a hard ceiling. Absorbs backend burst throttling / transient 5xx that otherwise fails a rotating subset of views every tick. `0` disables (single attempt); a second failure is final and keeps the unchanged markers-preserved/exit-1 path |
| `FULCRA_COORD_WRITE_RETRY` | `1` | The **single-write** sibling of the row above: the authoritative task-body upload in every mutating command (`tell` / `later` / `done` / `update` / …) retries ONCE after a 0.5–2s jitter sleep on a failed **or raising** upload — absorbs the backend write-throttling that silently dropped single writes (2026-06-10 evidence: four sender-believed-delivered losses in one evening). `0` disables (single attempt); a second failure keeps the unchanged cached-locally path (warn + local cache + reconcile self-heal) |
| `FULCRA_COORD_WRITE_VERIFY` | `1` | Verify-after-write for the same task-body upload: the post-write version-tracking `stat` doubles as delivery confirmation (no extra round-trip on the fast path). When the just-uploaded body is NOT visible on the bus despite a success-shaped upload, the write is re-tried once more (jittered) and, if still unverifiable, an unmissable `DELIVERY NOT CONFIRMED: <task-id>` warning is printed and the op marker is kept `needs_reconcile` for the standard reconcile self-heal — the exit code never flips (the body is cached locally). `0` disables verification (for backends with unreliable `stat`) without losing the upload retry |
| `XDG_CACHE_HOME` | `~/.cache` | Local cache base |
| `XDG_CONFIG_HOME` | `~/.config` | Config base. The persisted identity is scoped **per working directory** at `<XDG_CONFIG_HOME>/fulcra-coord/identities/<cwd-hash>.json` (keyed by the cwd's realpath). A legacy global `identity.json` is **no longer resolved automatically** — it is only surfaced as a migration hint by `identity show` and copied in by `identity migrate`. The human handle lives at `<XDG_CONFIG_HOME>/fulcra-coord/human`. Neither is root-scoped. Pair per-cwd identity with **one git worktree per session** (`git worktree add ../<repo>-<purpose> -b <branch> origin/main`) so concurrent sessions don't share a single index/`HEAD` — see the ONBOARD docs |
| `FULCRA_COORD_STALE_HOURS` | `2` | An `active` task older than this is flagged `stale` and collected into `views/needs-attention.json` |
| `FULCRA_COORD_VIEW_STALE_MIN` | `20` | Staleness ceiling (minutes) for the materialized **read views** (`views/summaries.json`, `views/presence.json`). Views refresh only when a write/reconcile successfully **uploads** them, so under backend throttling they can lag the durable `tasks/` / `presence/` files by hours while reads keep trusting them — inboxes look empty, a live reviewer looks dead. When a view's `generated_at` is older than this, reads **fall back to listing the durable files directly** (slower — one listing + per-file fetches — but complete) and print a `WARN` so the degradation is visible. A view with no `generated_at` (written by an older CLI) is trusted as before; if the direct listing also fails, the stale view is used with a louder warn (degraded, never blind). `0` disables the guard |
| `FULCRA_COORD_INBOX_AGE_DAYS` | `3` | A still-`proposed` **broadcast** (`assignee="*"`) older than this drops out of the default `inbox` / SessionStart view — informational fan-out ("X joined the mesh") that has served its purpose. Pure **read filter**: it never changes task status or the task file (a peer on an older CLI still sees it), and **only broadcasts age** — a directive addressed to a concrete agent (a real ask) is never aged out. `inbox --all` shows everything including aged-out broadcasts; the default `inbox` notes how many are hidden |
| `FULCRA_COORD_BROADCAST_EXPIRY_DAYS` | `14` | A still-`proposed` **broadcast** (`assignee="*"`) whose `created_at` is older than this is transitioned `proposed → abandoned` by the reconcile retention pass, after which cold-archive sweeps it out of the hot path on a later pass — so never-claimed broadcasts stop cluttering `status` instead of living on the bus forever (they already leave the `inbox` at `FULCRA_COORD_INBOX_AGE_DAYS`). Unlike that read filter this **changes status**, but it is recoverable via `fulcra-coord restore`, and — like the inbox filter — it **only expires broadcasts**: a directive addressed to a concrete agent (a real ask) is never expired regardless of age. Clockless broadcasts (missing/unparseable `created_at`) are never expired (fail-safe). Reconcile reports `expired N broadcast(s)` in its Retention line |
| `FULCRA_COORD_NOTIFY_WEBHOOK` | _(unset)_ | Opt-in real-time push endpoint for the listener (Tier 1). When set, `notify-inbox` POSTs a notification to this URL via stdlib `urllib` — the push that reaches the operator's phone regardless of OS or which host fired. Unset → push disabled, native-desktop only. Works with any commodity service (a free / self-hosted ntfy topic, Pushover-style, Slack, Discord) — it is **not** tied to any specific infrastructure |
| `FULCRA_COORD_NOTIFY_FORMAT` | _(auto)_ | Payload shape for the webhook POST: `ntfy\|slack\|discord\|json`. Auto-detected from the URL host (`discord` → Discord JSON, `slack` → Slack JSON, else **ntfy** plain-body, the generic default); set this to override the detection |
| `FULCRA_COORD_NOTIFY_TIMEOUT` | `5` | Seconds before the webhook POST gives up, so a slow/hung push endpoint can't stall a polling tick |
| `FULCRA_COORD_CONTINUITY_KEEP` | `10` | How many of the newest **continuity checkpoint** archives to keep per task. `continuity/<ws>/<agent>/<task>/checkpoints/CHK-*.json` is written immutably on every snapshot (SessionEnd / PreCompact / compaction) and would otherwise grow without bound; the reconcile retention pass keeps the newest N per task and deletes the rest (`latest.json` is never touched — it's the live pointer a resuming agent reads). Floored at `1` so the latest checkpoint is never deleted. Reconcile reports `N continuity` in its Retention line |
| `FULCRA_COORD_READ_SOURCE` | `file` | Where task **bodies** are reconstructed from (per host, reversible). `file` (default) reads the mutable `tasks/<id>.json`, byte-identical to pre-substrate behaviour. `events` folds the task's immutable event log and uses the fold **only** when it's a complete snapshot, falling back to the file on a delta-only / empty / errored fold. Opting a single host into `events` is the validation step for the read cutover; flipping the fleet default is a deliberate operator decision gated on parity (see [Event-sourcing substrate](#event-sourcing-substrate-the-durable-event-log)). Any unrecognised value degrades to `file`, so a typo can never silently flip the read path |
| `FULCRA_COORD_EVENTLOG_KEEP` | `20` | How many of the newest **event-log shards** to keep per LIVE task. Every task mutation appends `events/tasks/<id>/<event_id>.json` forever, so the reconcile retention pass window-prunes each live task below its latest snapshot — keeping that snapshot plus the most recent N events — and GCs the whole shard tree of archived/deleted tasks. A delta-only task (no snapshot ever emitted) is **never** pruned (fail-safe: a delta may carry a unique field never re-set). Floored at `1`. Reconcile reports `N events` in its Retention line |
| `FULCRA_COORD_PARITY_SAMPLE` | `50` | Tasks the reconcile event-parity pass probes per tick (a rotating window with a persisted cursor, so the full bus is covered every ~⌈N/sample⌉ ticks). Probing everything every tick was the bulk of a measured 3,105-subprocess reconcile; drift is a slow-moving report-only signal, so sampling loses nothing but latency. `<= 0` disables sampling (probe everything) |
| `FULCRA_COORD_RETENTION_DAYS` | `30` | Age past which a terminal (done/abandoned) task leaves the hot path for the cold archive. A month of finished work stays instantly visible in `recently-done`/`search` before it cold-stores |
| `FULCRA_COORD_RETENTION_MAX_PER_RUN` | `200` | Per-tick archive cap: a huge first backlog drains over several passes instead of blowing reconcile's deadline |
| `FULCRA_COORD_MARKER_RETENTION_DAYS` | `7` | How long spent digest dedup markers linger before the retention pass deletes them — regenerable guards with no history value |
| `FULCRA_COORD_PRESENCE_RETENTION_DAYS` | `30` | How long a dead presence record lingers before the retention pass takes it. Presence is a live snapshot, not history; a record untouched this long is a long-departed agent |
| `FULCRA_COORD_PRESENCE_GRACE_SECONDS` | `1200` | Wall-clock grace (seconds) past the idle→stale cutoff before review routing treats an agent as below the liveness floor. One missed heartbeat or a laptop sleep/wake must not drop a reviewer; an absolute duration (not a tick count) so it evaluates identically on every machine |
| `FULCRA_COORD_REVIEW_REROUTE_MINUTES_P1` | `15` | How long a never-acted P1 review may sit on a reviewer that has since gone below the liveness floor before the reconcile sweep reroutes it to a live candidate |
| `FULCRA_COORD_REVIEW_REROUTE_MINUTES_P2` | `30` | The same reroute gate for P2/P3 reviews — less urgent, longer leash |
| `FULCRA_COORD_REVIEW_REROUTE_MAX` | `2` | Total route attempts (the initial route + reroutes) before the sweep stops cycling reviewers and escalates the review to the human |
| `FULCRA_COORD_ACCEPTED_STALL_HOURS` | `2` | Hours an accepted-then-silent review may stall before the sweep escalates it to the human. An accepted review is never rerouted — work isn't yanked out from under a reviewer mid-flight; the human gets nudged instead |
| `FULCRA_COORD_HEALTH_DEGRADED_SECONDS` | heartbeat interval ×3 (`3600`) | Age of a host's newest reconcile past which the fleet-health dashboard marks it `degraded`. The default ties to the heartbeat interval rather than bare wall-clock so one slow or skipped tick can't flap a host |
| `FULCRA_COORD_HEALTH_OUTAGE_SECONDS` | `10800` | Age (~3h) past which a silent host reads `outage` on the fleet-health dashboard |
| `FULCRA_COORD_OPSLOG_MAX_BYTES` | `1000000` | Size ceiling for the local ops-log segment before it rotates to `.1`. Floored at 4096 so a tiny override can't rotate on every append; `<= 0` disables rotation entirely (unbounded, opt-in) |
| `FULCRA_COORD_AGENT` | — | Session-scoped override for your agent id. Identity resolution order is: explicit `--agent` > `FULCRA_COORD_AGENT` > per-cwd persisted identity (`fulcra-coord identity set`) > derived `claude-code:<host>:<repo>` (matching the SessionStart hook) |
| `FULCRA_COORD_HUMAN` | `human` | The human operator's handle — who tasks are "blocked on ME" against (`needs-me`, `block --on-user`). Resolution order: `FULCRA_COORD_HUMAN` > persisted handle (`fulcra-coord human set`) > default `human`. Personalize with `fulcra-coord human set <name>` |
| `FULCRA_COORD_BACKEND` | — | Override backend (testing only) |
| `FULCRA_COORD_ANNOTATIONS` | `off` | Emit lifecycle annotations to the Fulcra **Agent Tasks** timeline track: `off` (default, inert), `http` (alias `api`, **recommended** — writes directly over the Fulcra HTTP API via stdlib `urllib`, needs only a Fulcra token), or `cli` (legacy CLI shell-out). Resolution order: this env var (when set) > the persisted config (`fulcra-coord annotations on`, at `<XDG_CONFIG_HOME>/fulcra-coord/annotations`) > `off`. **Persist it once with `fulcra-coord annotations on`** so every agent emits without exporting this in each shell; set the env var to override a single session. See [docs/annotations.md](docs/annotations.md). |
| `FULCRA_API_BASE` | `https://api.fulcradynamics.com` | Fulcra HTTP API base for the `http` annotation transport. |
| `FULCRA_ACCESS_TOKEN` | _(unset)_ | Bearer token for the `http` annotation transport; when unset the writer falls back to `fulcra auth print-access-token`. |
| `FULCRA_COORD_ANNOTATION_CACHE_TTL_SECONDS` | `86400` | TTL on the locally cached annotation definition/tag ids. A fresh entry is a zero-HTTP hit; an expired one re-resolves (and re-stamps), so any drift heals within a day instead of being trusted forever |
| `FULCRA_COORD_SELF_UPDATE` | `1` (on) | **Version self-incorporation** — default ON. Every `connect` (session start) and every throttled `notify-inbox` tick compares the installed version against the canonical manifest at `runtime/version.json` and, when behind, runs the locally configured update (see [Self-update](#self-update)). Set `0` to opt this host out |
| `FULCRA_COORD_SELF_UPDATE_INTERVAL_H` | `6` | Listener-tick throttle: at most one self-update check per this many hours (mtime marker in the cache dir). `connect` is never throttled — a fresh session always checks |
| `FULCRA_COORD_SESSION_KEY` | — | Generic session pointer key for non-Claude-Code agents (OpenClaw passes its `sessionKey` here); `CLAUDE_CODE_SESSION_ID` takes precedence |
| `FULCRA_OPENCLAW_HOOKS_ROOT` | `~/.openclaw/hooks` | OpenClaw automation-hooks dir for `install-openclaw` |
| `FULCRA_OPENCLAW_PLUGIN_DIR` | `~/.openclaw/plugins/fulcra-coord` | Target dir for the materialized Track B plugin sources (`install-openclaw --with-plugin`); overridable via `--plugin-dir` |

## Roles & review-routing

**Roles are the durable identity; sessions are leases.** Agent sessions are
ephemeral — they die, sleep, and get respawned, and any identity pinned to a
session id drifts with it. A **role** (`reviewer`, `deployer`, `backlog-groomer`
— whatever your fleet needs; none ship in core, all roles are *your* registry
data) is the thing that persists. Register one with
`fulcra-coord roles set <name> --description '...' --instructions '...'`:

- **`standing_instructions` onboard fresh sessions.** The registry record
  carries the job — runbooks, conventions, where the role's state lives — so
  any new session that claims the role knows what to do without being told.
- **Leases ride presence.** `connect --role <name>` (or `roles claim <name>`)
  writes a per-agent lease on the role; the lease stays fresh exactly as long
  as the holder's presence heartbeat does. There is no extra keep-alive to
  run — when a session dies, its presence goes stale and its leases lapse with
  it, and the role reads **VACANT** on `board` / in the health record.
- **Vacancy routes to the role's `maintainer`.** Give a role `--sla-hours N`
  and `--maintainer <who>` (an agent id, an `@role`, or your human handle):
  if it sits vacant past the SLA, an escalation directive lands in the
  maintainer's inbox — once per day, not per reconcile tick. This generalizes
  "agent X is dark" into the thing that actually matters: "function X is
  unstaffed".
- **`--policy exclusive` keeps single-holder roles honest.** Two fresh leases
  on an exclusive role render **CONTESTED** (visible, never silently
  double-held); a stale lease is simply claimable. The default `shared` policy
  fans out to every fresh holder.
- **`checkpoint_ref` is the role's resume point.** A session claiming a role
  (`roles claim` / `connect --role`) prints the role's checkpoint ref and —
  when the optional `fulcra-continuity` CLI is installed — the rendered
  resume brief. Set it with `checkpoint --role X --ref <ref>`, or let the
  session-exit `park` hook maintain it automatically. See
  [Continuity](#continuity).

`fulcra-coord roles` is also the **discovery** surface: senders list the
registry to learn what roles exist instead of guessing session ids, and
`@role` directives (below) resolve against the live lease-holders.

**Declaring what an agent can do.** An agent advertises its capabilities at
connect time: `fulcra-coord connect --role <role>` records `<role>` (repeatable)
in the agent's presence `capabilities`, and roles are arbitrary — `review`,
`deploy`, `triage`, whatever your fleet uses. `connect --can-review` is sugar for
`--role review`. Capabilities are part of the same presence record the bus
already keeps, so they carry liveness with them.

**How review requests are routed.** `fulcra-coord request-review <artifact>
[--repo <repo>]` builds a preference-ordered candidate pool — the configured
reviewer **seed** (optional, see below) followed by every live/idle agent that
declared the `review` capability — and assigns the directive to the first agent
that is currently live or idle. If no candidate is live, the request escalates to
the human via `block --on-user`, so a review never silently vanishes. A
reconcile-time sweep reroutes a never-acted review off a reviewer that has since
gone dark, and escalates to the human once the reroute cap is hit. `<artifact>`
is an opaque ref — a PR#, MR#, branch, commit SHA, URL, or patch id — so routing
is forge-agnostic; `--repo` is optional.

**Configuring a preferred-reviewer seed (optional).** The pool is purely
capability-driven by default — you do **not** need any config to use review
routing. To bias routing toward specific reviewers, drop a
`review-routing.json` at `${XDG_CONFIG_HOME:-~/.config}/fulcra-coord/` with a
top-level `seed` (and optional `author_overrides` keyed by an author-id prefix);
see [`review-routing.example.json`](review-routing.example.json) for the shape.
The env var `FULCRA_COORD_REVIEW_SEED` (comma-separated agent ids) overrides the
file's top-level seed for a single session. The seed is a preference/tie-break
only — a live `review`-capable agent still gets the work, and an empty seed
degrades to pure capability routing. Put **your** fleet's reviewer ids in this
config; never hard-code them in source.

**Role-addressed directives.** A directive may also target an `@<role>` audience
instead of a concrete agent id. Role audiences resolve at inbox/read time against
each agent's declared capabilities, so every live holder of that role sees the
ask and agents without the role do not. Use this for post-deploy fleet aliases
such as `@reviewer`; until agents declare the matching role, older concrete-id
routing continues to behave as before.

## Coordination loops

**Any ask that crosses an agent/session boundary is a directive-with-a-lifecycle.**
A review request, a dispatch to another agent, a question, an idea being routed —
these are not separate record families; they are one loop record whose `kind`
field selects a per-kind state machine from a registry (`loops.KINDS`). Adding a
work-type means registering a kind, not adding a schema or module. Records
written before loops existed read as the legacy `tell` kind, so a mixed fleet
keeps working untouched.

| Kind | Lifecycle | Expects response | SLA default |
|---|---|---|---|
| `tell` | `sent → acked → closed` | no (legacy/FYI default) | — |
| `review` | `requested → acked → in_review → responded → closed` | yes | 24h |
| `dispatch` | `assigned → accepted\|declined → in_progress → delivered → closed` | yes | 72h |
| `idea` | `captured → maturing → viable → routed → active → done\|dropped` | no (a pipeline, not an ask) | — |
| `question` / `signoff` | `asked → answered → closed` | yes | 48h |

Lifecycles are permissive where it matters: a `review` can jump straight from
`requested` to `responded` (no forced ack dance), a `declined` dispatch can be
reassigned, and every machine guarantees a terminal state is reachable from
every state — a loop can never be stranded. `review`/`dispatch`/`idea` are wired
end to end; `question`/`signoff` are registered now and built when first needed.
A record-level SLA overrides the kind default; `idea` and `tell` have no SLA and
are never flagged overdue.

**Backlog (`later`).** When something should happen *eventually* — a "do later"
item handed to an agent mid-session — capture it on the bus, not in session
memory:

```bash
fulcra-coord later "Try sharding the search index" -s "came up during the retention work"
```

This creates a `kind=idea` loop in the `captured` state, addressed to the
`@backlog` role audience. Because `@backlog` is a role nobody holds by default,
the item is durable and board-visible (the ideas pipeline counts it; `search`
finds it) without landing in anyone's inbox — and a future backlog-groomer
agent can `connect --role backlog` to receive the whole backlog at once. Ideas
expect no response, so they never clutter the open-loop ledger. When an item's
time comes, route it with the ordinary `assign TASK-ID <agent>`; the loop's
state folds from `captured` to `routed`.

**Dispatch asks (`tell --expects-response`).** A plain `tell` is an FYI: the
recipient acks it and life goes on. Add `--expects-response` when you are
*asking for work back*:

```bash
fulcra-coord tell that-agent "Port the digest emitter" --expects-response
```

This opens a `kind=dispatch` loop (`assigned`, SLA-tracked at the registry
default) that stays open — visible in your `board`'s awaiting-others column,
flagged `⚠ overdue` past its SLA — until the recipient closes it on the bus
with `respond <loop-id> --outcome … --evidence …`. Broadcasts deliberately
cannot carry the flag: a fan-out FYI must not open a loop per agent.

**The closed-loop guarantee.** A loop that expects a response stays **OPEN until
a response lands on the bus** — nothing else closes it:

```bash
fulcra-coord respond <loop-id> --outcome approve --evidence "PR #42 verified"
```

(`review-done` is the review-specific close primitive; `respond` is the generic
return leg for everything else.) Each response is an append-only shard under the
directive's `responses/` sub-log, so concurrent responders — a `@role` fan-out
answering at once — never clobber each other. The loop snapshot's `outcome` and
`state` are a best-effort **cache** of the sub-log fold, never the truth. And to
state it plainly: a forge comment, a pushed commit, or any out-of-band message
**never** closes a loop. The verdict that exists only as a PR comment is, from
the bus's point of view, a verdict that never happened — that is the exact
failure mode this design exists to kill. Closing a loop is one command from any
state; no lifecycle dance is ever required to land an outcome.

**Visibility.** `status` warns when coordination loops are overdue or awaiting
your response. Each reconcile tick records a `loop_health` block (open / overdue
/ awaiting-me counts) in the per-host health record. The inbox listener appends
the overdue count to its notification (`… · 2 overdue`), so a lapsed loop rides
the alert the operator already sees.

**The board.** `fulcra-coord board` is the operator's glance view of every open
loop, in four sections: loops awaiting you, your own unanswered asks, open
non-idea loops by kind, and the ideas pipeline by state. Your unanswered asks
carry trailing flags — `⚠ overdue` for a loop past its SLA with no answer,
`◈ out-of-band` for a loop whose answer exists off the bus (see the forge
mirror below). `--format json` prints the raw board projection for scripting.

```bash
fulcra-coord board

  Awaiting me (1)
    DIR-20260608-...  [review]  Review the retention sweep
  Awaiting others (2)
    DIR-20260607-...  [dispatch]  Port the digest emitter ⚠ overdue
    DIR-20260609-...  [review]  Review PR #42 ◈ out-of-band
```

**The forge mirror.** `fulcra-coord forge-mirror --once` is the **one**
sanctioned forge poller — core is fitness-pinned to never import it. It sweeps
open review loops and mirrors verdict-shaped GitHub signals (a merge, a review
state, a verdict comment) into the loop's evidence sub-log, force-marked
`source: forge-mirror`. Mirrored evidence **never** closes a loop: it flags the
loop `◈ out-of-band` on the board so the requester closes it explicitly
(`respond`), citing the evidence. The mirror makes a slipped discipline
visible; the bus response remains the only thing that closes anything.

**Self-healing listener.** The response leg is only useful if someone is
listening for it, so `connect` idempotently re-arms the per-agent `notify-inbox`
job whenever it finds it missing — a dead listener heals on the next session
instead of staying silently dead. It is best-effort and quiet (never blocks
connect); opt out with `FULCRA_COORD_ENSURE_LISTENER=0` where the host scheduler
is managed elsewhere.

**Rule of the road.** Do the work wherever it lives — a forge, a doc, a sandbox.
But the *signal* — the verdict, the result, the answer — returns on the bus.
That single discipline is what makes cross-agent coordination visible,
auditable, and loss-proof: the requester consumes the bus, never polls the
platform, and the overdue detector catches any loop where the discipline
slipped.

## Continuity

**A checkpoint is only useful if it travels with the work.** Fulcra Continuity
(the standalone `fulcra-continuity` package) does the hard part — structured
checkpoints (objective, decisions, artifacts, open questions, next actions)
plus resume-brief rendering. What coord adds is **bus visibility and
delivery**: a checkpoint becomes a payload *ref* on the coordination
primitives you already use, instead of a side-tree only retention knows
about. Coord stores **refs, never bodies** — the checkpoint schema stays
owned by `fulcra-continuity` (coord never imports it; when installed, its CLI
is invoked as a subprocess to render briefs), and refs are opaque strings
coord forwards verbatim.

**Handoff — a dispatch loop carrying a resume point.** When one session hands
work to another agent (or a `@role`), send the state along:

```bash
fulcra-continuity checkpoint --task-id TASK-123 --title "..." --objective "..." \
  --next "Audit parser inputs" --out /tmp/handoff.json
fulcra-coord handoff --to @arc-maintainer --title "Continue the parser audit" \
  --checkpoint /tmp/handoff.json
```

`handoff` opens an ordinary `kind=dispatch` loop (SLA-tracked, closed only by
`respond`) whose payload carries `checkpoint_ref`. A **local checkpoint file
is published to the bus first** — the `fulcra-continuity` CLI writes local
paths, which are useless on another host, so coord uploads the JSON to its
`continuity/` tree and carries the immutable remote archive path as the ref
(if that publish fails, the checkpoint rides inline in the payload as a
fallback). Already-remote/opaque refs pass through untouched. The recipient
sees `checkpoint:` on the directive in `inbox`, and **claiming the task**
(`update <id> --status active`) prints the ref plus — when `fulcra-continuity`
is installed — the rendered resume brief. Closing the loop = the work
continued.

**Role checkpoints — where the role left off, surviving session death.** The
role registry's `checkpoint_ref` is the role's durable resume point:

```bash
fulcra-coord checkpoint --role arc-maintainer --ref <ref>   # set
fulcra-coord checkpoint --role arc-maintainer               # show ref + brief
```

Any session that later claims the role — `roles claim arc-maintainer` or
`connect --role arc-maintainer` — gets the ref + best-effort resume brief
printed at claim time. This is the respawn backbone: spawn session → claim
role → resume brief → work → checkpoint on park.

**Park — checkpoint on the way out.** `fulcra-coord park` checkpoints every
role the session holds (via the optional `fulcra-continuity` CLI), publishes
each checkpoint to the bus, and points the role's `checkpoint_ref` at it. The
Claude Code PreCompact/SessionEnd hooks call it backgrounded, so the
"session died mid-work" gap closes without ever blocking a session exit:
missing CLI, no held roles, or any bus failure are silent no-ops and `park`
never exits nonzero. Checkpoint archives are GC-bounded by the existing
`FULCRA_COORD_CONTINUITY_KEEP` retention.

Everything here degrades gracefully: without `fulcra-continuity` installed
you still get refs stored, forwarded, and printed — only the rendered brief
requires the CLI. Nothing in core hard-depends on the continuity package.

## Remote layout

```
/coordination/
  index.json                ← global compact index with counts
  views/
    active.json             ← all active/waiting/blocked
    next.json               ← proposed + waiting
    recently-done.json      ← last 7 days of done/abandoned
    search-index.json       ← searchable records
    needs-attention.json    ← active tasks gone stale (possibly forgotten)
    inbox/{agent-slug}.json ← open directives addressed to each assignee
  workstreams/{ws}.json     ← per-workstream active view
  agents/{agent}.json       ← per-agent active view
  tasks/TASK-*.json         ← individual task files (mutable, authoritative)
  events/
    tasks/{task_id}/{event_id}.json ← immutable, append-only event shards
                                       (one path per event; never overwritten)
  digest/
    markers/{date}-{window}.json ← per-window operator-digest dedup marker
                                   (first-writer-wins; any agent, any machine)
  roles/
    {name}.json               ← role registry record (description, standing
                                 instructions, policy, sla_hours, maintainer)
    {name}/leases/{agent-slug}.json ← one lease file PER HOLDER (a re-claim
                                       refreshes only the claimer's own file;
                                       freshness = the holder's presence)
    {name}/escalations/{date}.json  ← daily vacancy-escalation dedup marker
                                       (first-writer-wins, like digest markers)
```

`index.json`'s `counts.inbox` folds a per-assignee directive count so a hook can see "you have N directives" without loading every inbox view.

## How it works

1. Agent calls `fulcra-coord start` / `update` / `done`
2. Helper stats the remote task file (optimistic concurrency)
3. Applies the change locally, uploads the task file
4. Rebuilds all views from the full cached task set
5. Uploads views to Fulcra Files
6. Writes an operation marker — if view uploads fail, flags `needs_reconcile`
7. `fulcra-coord reconcile` repairs partial writes

Read commands use local cache when fresh. Full remote sync happens on `status` and `reconcile`.

## Event-sourcing substrate (the durable event log)

Alongside the mutable `tasks/<id>.json` files, the bus keeps an immutable,
append-only **event log**. The cutover is deliberately incremental: the event
log is written on every mutation, validated in parallel, and only takes over
reads once parity proves it's safe. The mutable file stays authoritative until
that flip.

**The model.** Every task mutation (`_write_task_and_views`) does two things:
upload the task file as before, *and* — best-effort, default-ON — append one
immutable shard `events/tasks/<id>/<event_id>.json`. The `event_id` is a
time-sortable `<sortable-ts>-<rand>` (the canonical UTC-microsecond instant of
the event plus a random suffix), so every append lands on a *distinct* path —
two concurrent writers for the same task in the same microsecond never collide,
which is what lets a no-CAS, no-lock store stay correct. The dual-write is
best-effort by design: an event-append failure is logged but **never** fails the
task write (the mutable file is authoritative; the parity pass audits misses).

**The fold.** `events.fold_task` is a pure reducer that reduces a task's events
back to a snapshot. It (1) sorts by `(canonical-microsecond-instant, event_id)`
— canonicalizing the timestamp so lexical order equals chronological order, NOT
a raw-string compare which inverts when timestamps differ only in trailing
precision; (2) dedups retries by the compound `(actor, idempotency_key)` pair,
first-in-sort-order wins; and (3) merges by payload type. A **snapshot** payload
(a full task, carrying both `schema` and `id`) *replaces* the accumulated state
wholesale — the latest snapshot wins and stale fields drop. A legacy **delta**
payload (a field subset, written by older CLIs) field-merges last-write-wins.
The two compose in any order. `fold_is_complete` is true once at least one full snapshot
has been applied — that's the signal the fold reconstructed a trustworthy,
schema-complete task rather than a partial delta-only stream.

> **Gotcha:** the `event_id` is derived from `at` but is **not** recomputable —
> two `make_event` calls with the same `at` produce different ids (random
> suffix). The reducer stores the id on creation; it never re-derives it.

**The read cutover.** `FULCRA_COORD_READ_SOURCE` (`file` default | `events`)
governs where task *bodies* are read from, per host and reversible by unsetting
it. Under `events`, `_cache_remote_task` folds the event log and uses the fold
**only** when `fold_is_complete`; on a delta-only / empty / errored fold it falls
through to the mutable file. So opting a host into `events` is incompleteness-
and-error-safe by construction. Default is `file`: this changes what a read
*returns*, so a host must explicitly opt in — that's the validation step.
Flipping the fleet default is a deliberate operator decision gated on parity,
**not** automatic.

> **Gotcha:** even under `events`, the write-path concurrency baseline always
> stats the *file* (`tasks/<id>.json`), never the fold. The cutover changes
> reads only; the optimistic-concurrency stat stays file-sourced, or the next
> write would lose its baseline.

**The parity safety net.** Reconcile's `_event_parity_check` folds each task's
events and compares the result against its mutable file, recording
`event_parity: {checked, drift, drift_task_ids, ack_drift, ack_drift_task_ids,
tasks_total, tasks_with_events, folds_complete}` in the per-host health record.
`drift` counts tasks where the fold disagrees with the file; `ack_drift`
separately counts folds missing a durable ack that the `summaries` view has (a
delta-only or truncated-event-log task can lose an `inbox_ack` the aggregate
still holds). The three coverage counts are the liveness signal: `tasks_total`
is every task file on the bus, `tasks_with_events` is how many had an event log
to compare (identical to `checked`), and `folds_complete` is how many produced a
trustworthy full-snapshot fold. The check is **report-only** — the mutable file
stays authoritative, nothing is rewritten.

The green light for the flip is **not** bare `drift == 0`. `drift == 0` is
satisfiable two ways: the fold faithfully reconstructs every task, OR the fold
folded nothing / there are no events on the bus, so there was simply nothing to
disagree with. The strengthened gate is `drift == 0` **AND** `folds_complete > 0`
**AND** `tasks_with_events` ≈ `tasks_total` (high coverage). A host that folded
nothing, or a bus with no events, can no longer read green just because there was
nothing to compare.

**Retention.** `FULCRA_COORD_EVENTLOG_KEEP` (default 20) bounds per-task shard
growth: the reconcile retention pass keeps each live task's latest snapshot plus
the most recent K events and prunes everything strictly older; a delta-only task
is never pruned (fail-safe), and an archived/deleted task's whole shard tree is
GC'd. Reconcile reports the count.

**Honest status.** The read cutover is still being hardened — mixed-fleet write
soundness (a fleet where some hosts read from `events` and others from `file`) is
in-flight. The flip is gated on sustained zero drift and is fully reversible per
host. Until then the mutable file is the source of truth and `events` is opt-in.

A first-class **Directive** record (schema `fulcra.coordination.directive.v1`,
built/validated by `schema.make_directive` / `validate_directive`) rides the
same dual-write pattern: every directive-creating command (`tell` / `broadcast`
/ `assign` / `request-review` / `review-done`) additively mirrors its task into
a `directives/<id>.json` loop record, best-effort. It models *communication*
(who told whom what) as its own record type rather than a task-with-assignee.
The task record stays authoritative for task state; the loop records are what
the coordination-state readers consume — `board`, the digest, `review-done`,
and reconcile's health and directive-parity passes (see [Coordination
loops](#coordination-loops)).

## Module architecture

The package is layered: leaf utilities at the bottom, feature subsystems above
them, and `cli.py` at the top as the reconcile-orchestration core plus a thin
re-export layer that aggregates every command for the dispatcher (`entry.py`).
Each feature module depends only on lower layers and **never imports `cli`**, so
there are no import cycles.

| layer | module | responsibility |
|-------|--------|----------------|
| leaf utils | `output.py` | stdout/stderr formatting (`print_json`/`err`/`warn`/`info`) |
| | `timeutil.py` | the UTC/microsecond/`Z` bus-timestamp convention |
| | `textfmt.py` | human relative-time formatters (`age`/`until`/`due`) |
| core | `__init__.py` | `remote_root`, `task_file_path`, `env_float`/`env_int` |
| | `remote.py` | Fulcra Files I/O (upload/download/list/`list_json`/stat/delete) |
| | `events.py` | pure event envelope (`make_event`/`event_id`) + the `fold_task` reducer / `fold_is_complete` (no I/O) |
| | `eventlog.py` | append-only event-shard I/O (`append_event`/`read_events`) over the immutable `events/tasks/<id>/<event_id>.json` paths |
| | `schema.py` | task schema + state transitions; the directive/loop record (`make_directive`/`validate_directive`) every directive-creating command dual-writes |
| | `views.py` | materialized-view generation + pure judgments |
| | `io.py` | task load/cache layer (parallel fetch, summaries fast-path, self-heal) |
| | `writepipe.py` | the single write path: optimistic-concurrency upload + merge + view fan-out |
| subsystems | `retention.py` | cold-archive + cold-index + prune + `search`/`restore` |
| | `presence.py` | per-agent presence + reconcile rebuild |
| | `routing_ops.py` | liveness-aware reviewer routing + reroute sweep |
| | `digest.py` | operator digest (push) + fleet-health dashboard (pull) |
| | `lifecycle.py` | mutation commands and durable checkpoints (start/update/block/pause/snapshot/done/abandon/tell/broadcast/assign) |
| | `query.py` | read commands (status/agents/needs-me/resume) |
| | `inbox.py` | directive inbox + blocked-on-you notification |
| | `installers.py` | hook + scheduler installers |
| | `doctor.py` | `capabilities` + `doctor` diagnostics |
| | `config.py` | local config commands (identity/human/annotations/session-task) |
| top | `cli.py` | the reconcile tick (`cmd_reconcile` + health-record write + stale-claim detection) and the re-export aggregation surface |
| entry | `entry.py` | argparse + the `COMMAND_MAP` dispatcher |

Commands are re-exported from `cli` under their historical names so the dispatch
table and the test patch surface (`fulcra_coord.cli.<name>`) keep resolving — the
extraction is behavior-preserving end to end.

## Adapters

- `adapters/claude-code/CLAUDE.md` — paste into project CLAUDE.md files
- `adapters/codex/AGENTS.md` — for Codex Paperclip agents
- `adapters/openclaw/SKILL.md` — OpenClaw skill-style integration
- `adapters/generic-cloud-agent.md` — for ephemeral cloud/CI agents
- `fulcra-coord install-claude-code` — wires SessionStart/PreCompact/SessionEnd
  hooks so every Claude Code session auto-surfaces in-flight work and checkpoints.
  Already-running sessions: see `adapters/claude-code/ONBOARD.md`.
- `fulcra-coord install-codex` — wires Codex SessionStart + PreCompact hooks
  (reusing the Claude Code hook bodies — Codex hooks receive the same
  `session_id`/`transcript_path`/`cwd` stdin shape; PreCompact keys the session
  pointer on `FULCRA_COORD_SESSION_KEY`) into `~/.codex/hooks.json` via an
  idempotent surgical JSON merge. No Stop hook: Codex `Stop` fires every turn and
  would thrash the task, so end-parking is delegated to the heartbeat. Codex
  Desktop active threads do not get live text injected by launchd; if the
  operator wants broadcasts to appear in an already-open thread, add a Codex app
  heartbeat/automation that polls `fulcra-coord inbox --agent <id>` for that
  thread.
- `fulcra-coord install-heartbeat` — installs a scheduled `fulcra-coord reconcile`
  (launchd LaunchAgent on macOS, a managed crontab line elsewhere). The reconciler
  is the coordination safety net: it sweeps `active` tasks left dangling by crashed
  agents or end-hook-less surfaces (ChatGPT, Codex) and rebuilds
  `views/needs-attention.json` on a cadence (default every 20 min).
- `fulcra-coord install-listener` — the durable inbox listener. The
  coordination suite surfaces directed work (`tell` / `assign`) the instant a
  session opens (the SessionStart hook's "📥 Directives for you" section); the
  listener is how an *idle* agent notices a directive that arrives between
  sessions. By default it's notify-only: a scheduled `fulcra-coord notify-inbox`
  polls the inbox and, if there are open directives, writes a surface file the
  next SessionStart injects and emits a desktop notification — it never runs
  the directive itself. With an opt-in per-adopter `wake.json` entry it can
  additionally **wake the agent runtime** (spawn your configured command,
  throttled and single-flighted) so pending work gets processed with nobody at
  the keyboard — see "Host wake" below. The native Claude Code mechanism is a scheduled remote agent (the
  harness scheduler); `install-listener` is the harness-free launchd/cron
  fallback, and OpenClaw folds `notify-inbox` into its heartbeat. The listener is
  **per-agent**, not per-machine: its launchd label / plist / cron marker are
  derived from the agent's slug, so co-located agents on one machine each get
  their own coexisting job and none clobbers another. (A legacy machine-global
  job from an older install is migrated to a per-agent job on the next install.)
  Notification delivery is layered and best-effort: **Tier 0** is the SessionStart
  inbox-surface file (guaranteed, zero-config, no network — directed work always
  reaches the operator on the next session start, every OS); **Tier 1** is opt-in
  real-time push — if `FULCRA_COORD_NOTIFY_WEBHOOK` is set, a stdlib-`urllib` POST
  to that URL is what reaches the operator's phone, and a small adapter shapes the
  payload from the URL host (`discord` / `slack`, else **ntfy** plain-body) so it
  works with any commodity push service rather than depending on specific infra;
  **Tier 2** is a best-effort native desktop ping (macOS `osascript`, Linux
  `notify-send`, else a stderr line), a no-config local bonus never relied upon.
  The inbox notification is deduped via a seen-set, so it fires once per **new**
  directive instead of re-alerting every tick. See
  `adapters/claude-code/LISTENER.md`.
- `fulcra-coord install-digest` — the push side of the **operator digest**.
  Where `install-heartbeat` / `install-listener` are *interval*-scheduled
  ("every N min"), the digest is *calendar*-scheduled: two jobs, `digest
  --window morning` at 08:00 and `digest --window evening` at 18:00, local
  (launchd `StartCalendarInterval` on macOS, fixed `M H * * *` cron lines
  elsewhere). The digest itself is a single consolidated situational-awareness
  summary written to the Fulcra timeline on its **own** track — `Agent Tasks —
  Digest`, separate from and independent of the granular per-event `Agent
  Tasks` track (which is unchanged) — so the human-paced twice-daily moments
  filter apart from the per-event lifecycle stream. It folds four blocks:
  **blocked-on-you**, **upcoming**, **per-agent activity**, and **stale**.
  Unlike the per-agent listener, `install-digest` is safe to install on
  **every** machine: an any-agent **dedup guard** claims a per-window marker at
  `<remote_root>/digest/markers/<YYYY-MM-DD>-<window>.json` (first writer wins;
  every machine targets the same UTC-date-keyed path), so concurrent ticks
  collapse to exactly one digest per window. Like the rest of the digest path
  it's best-effort end to end — a failed marker claim or emit is logged and the
  tick still exits 0, so it never blocks a scheduled run.
- `fulcra-coord install-openclaw` — Track A of the OpenClaw integration.
  Materializes `BOOT.md` / `HEARTBEAT.md` (agent-driven prompts that run
  `fulcra-coord status` at gateway boot and on heartbeats) plus three file-based
  automation hooks into `~/.openclaw/hooks/`: a `session:compact:before` handler
  that ALWAYS checkpoints the session's active task before OpenClaw summarizes
  history (the file-based analog of the Claude Code `PreCompact` hook —
  `session:compact:before` IS a file-based automation event, so this guarantee
  ships in Track A, no plugin required); a `gateway:shutdown` handler that parks
  the session's active task as `waiting`; and an `agent:bootstrap` handler that
  folds surfaced in-flight work into the session's `MEMORY.md` bootstrap slot via
  the mutable `event.context.bootstrapFiles` array. The three `handler.ts`
  templates are written to the real OpenClaw automation-hook API (verified
  against `docs.openclaw.ai/automation/hooks` and the `openclaw/openclaw` source
  — event shape, the `WorkspaceBootstrapFile` object type, and recognized
  bootstrap basenames); they still can't be *run* in this repo, so the installer
  is what's unit-tested.
- `fulcra-coord install-openclaw --with-plugin` — Track B: the OpenClaw
  Plugin-SDK plugin (`adapters/openclaw/plugin/`). It registers the in-process
  `session_start` / `before_compaction` / `session_end` lifecycle hooks the
  file-based surface can't reach. Track B's real differentiator is deterministic
  per-session start/end (there is no file-based `session:start`, and
  `session:end` is plugin-only); its `before_compaction` is the plugin-side
  equivalent of the compaction checkpoint Track A already ships file-based via
  `session:compact:before`. The TypeScript is validated against the OpenClaw
  Plugin-SDK *source* (`github.com/openclaw/openclaw`, `docs.openclaw.ai/plugins/hooks`),
  not a live runtime. `--with-plugin` only *materializes* the plugin sources
  (default `~/.openclaw/plugins/fulcra-coord/`); building + registering needs
  `npm`/`tsc` + `openclaw plugins install .`, which the CLI can't do — see
  `adapters/openclaw/plugin/README.md` for the steps.

### Host wake (wake.json)

The listener can now **wake your agent runtime, not just notify you**. Without
it, `notify-inbox` detects directed work while an agent is idle but can only
write a surface file and ping a webhook/desktop — delivery that still depends
on *you* opening the next session. With a wake entry configured, a tick that
finds pending work **spawns a command of your choosing** (typically "start a
headless agent session, process the inbox, exit") with nobody at the keyboard —
directives and review verdicts get handled while you're doing other things.
Sessions stay disposable; the bus, the checkpoints, and the host wake carry the
continuity.

The mechanism is platform-neutral core (a pinned invariant — no agent-runtime
command strings ship in `fulcra_coord/`); **what** gets spawned is entirely
your policy in `${XDG_CONFIG_HOME:-~/.config}/fulcra-coord/wake.json`:

```json
{
  "<agent-id-or-prefix>": {
    "cmd": ["claude", "-p", "BUS WAKE: you are <agent>. Process your fulcra-coord inbox… then exit."],
    "cwd": "/path/to/the/worktree",
    "min_interval_min": 15,
    "max_runtime_s": 900,
    "enabled": true
  }
}
```

Keys are agent ids or prefixes — **longest prefix wins**, so `"claude-code:"`
covers every host/repo instance and a longer key overrides for one host. With
no file (or a malformed one, or `"enabled": false`) the listener degrades to
exactly the notify-only behavior — the loader is fail-safe like
`review-routing.json`. Per-platform examples live in **your** wake.json; the
repo ships `wake.example.json` with claude-code / codex / openclaw entries
**clearly marked as examples** to copy from. `fulcra-coord install-claude-code
--with-wake` seeds the claude-code entry for you (and prints a loud review
note).

Mechanics and safety rails:

- The spawn is **detached** (`start_new_session`) so it can never block a
  polling tick; stdout/stderr append to the listener logs dir as
  `wake-<agent-slug>.log`. The spawned process receives `FULCRA_COORD_AGENT`
  (whose inbox fired) and `FULCRA_COORD_WAKE_PENDING` (the pending count) in
  its env — everything else it reads from the bus.
- Set `cwd` to the worktree/project directory the runtime should start in.
  `install-claude-code --with-wake` seeds it to the directory where you ran the
  installer, so a woken session sees the right `AGENTS.md`, MCP/plugin config,
  and local tooling. A missing `cwd` preserves legacy hand-written configs; an
  invalid `cwd` disables that wake instead of launching in the wrong context.
- **Throttle**: at most one wake per `min_interval_min` (default 15), tracked
  by a per-agent marker in the local cache. A failed spawn does not arm the
  throttle, so the next tick retries.
- **Single-flight**: a per-agent pidfile skips the spawn while a previous wake
  is still running; stale pidfiles are ignored.
- **Runaway protection is the spawned command's job**: `max_runtime_s`
  documents your runtime budget, but fulcra-coord deliberately runs no process
  manager — cap runtime with the spawned CLI's own timeout flags.
- The command runs **unattended with the host's default permissions** — review
  your wake.json entry before relying on it, and pause any entry with
  `"enabled": false`.

## Self-update

**Default ON** (operator call, 2026-06-10 — "i'm not going to go around and
wake the entire fleet for each incremental upgrade"); opt a host out with
`FULCRA_COORD_SELF_UPDATE=0`. The maintainer publishes a version manifest at
each release (`fulcra-coord announce-version` → `runtime/version.json`), and
every host checks it at the two places it already passes through:

- **`connect` (session start)** — unthrottled, runs **before** the presence
  write so the roster reflects post-update state. A successful update logs
  `updated to X — takes effect next invocation` and continues; there is no
  mid-session re-exec — the *next* session/wake runs the new code.
- **`notify-inbox` (the durable listener tick)** — throttled to one check per
  `FULCRA_COORD_SELF_UPDATE_INTERVAL_H` (default 6h). This is what keeps an
  operator-absent host current.

**The pointer rule (non-negotiable safety boundary).** The bus carries a
version *pointer*, never a code payload: the manifest is
`{schema, package_version, release_commit, min_supported, published_at}` and
its validator **rejects any extra key**, so a tampered manifest cannot smuggle
an instruction. The update command itself comes from **local config only** —
nothing read off the bus ever reaches an exec boundary.

**Configuring what "update" means** (`${XDG_CONFIG_HOME:-~/.config}/fulcra-coord/`):

```jsonc
// update.json — the built-in safe default: name your canonical checkout.
// fulcra-coord then runs, with argv built in code from this path:
//   git -C <checkout> pull --ff-only
//   uv tool install --reinstall --force <checkout>/packages/fulcra-coord
{ "checkout": "/path/to/fulcra-tools" }

// update-cmd.json — full override for non-standard installs (wins over update.json)
{ "cmd": ["/path/to/my-updater", "--flag"], "cwd": "/optional/dir" }
```

There is deliberately **no default checkout path** — the package cannot know
where your canonical clone lives, and guessing wrong would `git pull` someone
else's directory.

**Visible degradation, never breakage.** A host that is behind but cannot
update (no config, or the update failed) warns once, writes a local stale
marker, and its presence summary carries **`(vX behind canonical Y)`** on the
roster — staleness is visible to the operator instead of silently rotting.
Every failure path is best-effort: a self-update problem can never fail a
session boot or a polling tick. Update output is appended to
`<cache-dir>/self-update.log`; each step is bounded at 300s.

## Docs

- `docs/protocol.md` — when and how to use coordination
- `docs/auth.md` — auth in local and remote/headless environments
- `docs/continuity-handoff.md` — how `fulcra-coord` and Fulcra Continuity work together for cross-agent, non-GitHub handoff
- `docs/fulcra-cli-branch.md` — Fulcra CLI `file` support: verify + `FULCRA_CLI_COMMAND` repointing (the old special-branch workaround is obsolete)
- `docs/schema.md` — full task and view schema reference
- `docs/annotations.md` — Agent Tasks lifecycle annotation track (enable flag, tags, deferred-write caveat)
- `docs/other-side-claude-code-test-plan.md` — cross-environment Claude Code verification plan

## Running tests

```bash
pytest tests/ -v
```

No live Fulcra account required — tests use a fake backend.

### Pre-push hook (local CI gate)

A shared `pre-push` hook runs the fulcra-coord suite before any push that
changes fulcra-coord, so a red suite is caught locally. (GitHub Actions' macOS
job is path-filtered to macOS-specific changes only — see `.github/workflows/macos.yml`
— so a pure fulcra-coord change otherwise has no automated test gate.) The hook
is version-controlled in `.githooks/`, but `core.hooksPath` is per-clone config,
so **enable it once in each clone**:

```bash
git config core.hooksPath .githooks
```

It only runs when `packages/fulcra-coord/` changed; bypass a single push with
`git push --no-verify`. Requires `uv` on PATH.

## Live smoke test

```bash
export FULCRA_COORD_REMOTE_ROOT=/coordination-smoke
FULCRA_COORD_LIVE_SMOKE=1 python scripts/live_smoke.py
```

Requires a live Fulcra account with credentials.

## Example

```bash
python examples/multi_agent_example.py
```

Demonstrates two independent agents coordinating through an in-memory fake backend.

## License

MIT
