# fulcra-coord

**Shared agent coordination layer using Fulcra Files as a coordination bus.**

Multiple independent agents — local Claude Code sessions, cloud agents, CI jobs, OpenClaw, Codex — coordinate durable work through Fulcra Files without shared memory, direct calls, or a central broker.

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

Requires: Python 3.10+, and a **file-capable** Fulcra CLI (`fulcra-api`).

> **Important:** the public PyPI `fulcra-api` build does **not** ship the `file`
> command group that the coordination bus depends on. If `fulcra-coord doctor`
> reports `File commands: FAIL`, install a file-capable build (the
> `file-management` branch of `fulcradynamics/fulcra-api-python`). See
> [`docs/fulcra-cli-branch.md`](docs/fulcra-cli-branch.md) for the exact command.
> This is the most common fresh-agent setup failure — without it, every bus op
> fails silently.

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
| `tell` | Direct work at another agent: create a `proposed` directive task assigned to them (`tell <assignee> "<title>" [--from <me>] [--next] [--workstream] [--priority]`) |
| `broadcast` | Direct work at **every** agent: create a `proposed` directive with the wildcard assignee `*` (`broadcast "<title>" [--from <me>] [--next] [--workstream] [--priority]`). It lands in every agent's inbox and is acknowledged **per-agent** — one agent's `inbox --ack` clears it for that agent only, so no agent loses or duplicates the directive. Use `tell` for one agent, `broadcast` for all (e.g. "update fulcra-coord when main changes") |
| `assign` | Set or redirect the `assignee` on an existing task (`assign <task-id> <assignee>`) |
| `inbox` | List open directives addressed to you (`--agent`, `--format json`); `--ack <task-id>` marks one seen without claiming it. Stale informational broadcasts (older than `FULCRA_COORD_INBOX_AGE_DAYS`, default 3) are hidden by default and noted as a count; `--all` shows them too. Matching is prefix-aware: a directive addressed to a short id (`claude-code`) reaches the full-id agent (`claude-code:<host>:<repo>`) it prefixes |
| `identity` | Show, set, clear, or migrate this host's declared agent id — the identity handshake reused by every bus op. `identity` shows the resolved id + its source (and hints if a stale legacy global exists); `identity set <agent-id>` persists it; `identity clear` removes it; `identity migrate` copies a legacy global identity into the current repo's entry (`--format json`). **Scoped per working directory** so sibling sessions in different repos no longer clobber each other's identity |
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
| `snapshot` | Write a Fulcra Continuity-compatible checkpoint without changing task state (`--reason`, `--transcript-path`, optional `--next`). Used by compaction/idle hooks for low-noise resume state |
| `done` | Mark done (requires evidence) |
| `abandon` | Mark abandoned |
| `reconcile` | Repair views and resolve pending markers |
| `search` | Search tasks by text |
| `doctor` | Check configuration and connectivity |
| `install-shim` | Install CLI shim to `~/.local/bin/` |
| `install-claude-code` | Install Claude Code lifecycle hooks (global by default) |
| `install-openclaw` | Install OpenClaw Track A artifacts (boot/heartbeat prompts + shutdown/bootstrap hooks); add `--with-plugin` to also materialize the Track B Plugin-SDK plugin |
| `install-codex` | Install Codex lifecycle hooks (SessionStart + PreCompact) into `~/.codex/hooks.json`. No Stop hook by design — Codex end-parking is delegated to the heartbeat |
| `install-heartbeat` | Install a scheduled `reconcile` heartbeat (launchd on macOS, crontab elsewhere) — the safety net that sweeps stale tasks for crashed / end-hook-less agents (`--interval-min N`) |
| `install-listener` | Install a scheduled `notify-inbox` listener (launchd on macOS, crontab elsewhere) — the durable, per-agent way to notice directed work while idle (`--agent`, `--interval-min N`, default 10). See `adapters/claude-code/LISTENER.md` |
| `notify-inbox` | Poll the inbox for an agent; if directives exist, write a surface file the next SessionStart injects and emit a best-effort notification (the call the listener runs each tick). Notify-only |

All hook installers resolve a concretely-callable `fulcra-coord` invocation at install time and bake it into the materialized scripts (absolute on-PATH path, else `<python> -m fulcra_coord`), so hooks work under `uv tool` / source installs, not just `pip`-on-PATH. The committed adapter copies keep a literal placeholder.

## Configuration

| Variable | Default | Description |
|---|---|---|
| `FULCRA_COORD_REMOTE_ROOT` | `/coordination` | Coordination root in Fulcra Files |
| `FULCRA_CLI_COMMAND` | `fulcra-api` | CLI command (or `uv tool run fulcra-api`) |
| `FULCRA_COORD_TIMEOUT_SECONDS` | `5` | Read timeout |
| `FULCRA_COORD_RECONCILE_TIMEOUT_SECONDS` | `90` | Reconcile timeout |
| `XDG_CACHE_HOME` | `~/.cache` | Local cache base |
| `XDG_CONFIG_HOME` | `~/.config` | Config base. The persisted identity is scoped **per working directory** at `<XDG_CONFIG_HOME>/fulcra-coord/identities/<cwd-hash>.json` (keyed by the cwd's realpath). A legacy global `identity.json` is **no longer resolved automatically** — it is only surfaced as a migration hint by `identity show` and copied in by `identity migrate`. The human handle lives at `<XDG_CONFIG_HOME>/fulcra-coord/human`. Neither is root-scoped. Pair per-cwd identity with **one git worktree per session** (`git worktree add ../<repo>-<purpose> -b <branch> origin/main`) so concurrent sessions don't share a single index/`HEAD` — see the ONBOARD docs |
| `FULCRA_COORD_STALE_HOURS` | `2` | An `active` task older than this is flagged `stale` and collected into `views/needs-attention.json` |
| `FULCRA_COORD_INBOX_AGE_DAYS` | `3` | A still-`proposed` **broadcast** (`assignee="*"`) older than this drops out of the default `inbox` / SessionStart view — informational fan-out ("X joined the mesh") that has served its purpose. Pure **read filter**: it never changes task status or the task file (a peer on an older CLI still sees it), and **only broadcasts age** — a directive addressed to a concrete agent (a real ask) is never aged out. `inbox --all` shows everything including aged-out broadcasts; the default `inbox` notes how many are hidden |
| `FULCRA_COORD_AGENT` | — | Session-scoped override for your agent id. Identity resolution order is: explicit `--agent` > `FULCRA_COORD_AGENT` > per-cwd persisted identity (`fulcra-coord identity set`) > derived `claude-code:<host>:<repo>` (matching the SessionStart hook) |
| `FULCRA_COORD_HUMAN` | `human` | The human operator's handle — who tasks are "blocked on ME" against (`needs-me`, `block --on-user`). Resolution order: `FULCRA_COORD_HUMAN` > persisted handle (`fulcra-coord human set`) > default `human`. Personalize with `fulcra-coord human set <name>` |
| `FULCRA_COORD_BACKEND` | — | Override backend (testing only) |
| `FULCRA_COORD_ANNOTATIONS` | `off` | Emit lifecycle annotations to the Fulcra **Agent Tasks** timeline track: `off` (default, inert), `http` (alias `api`, **recommended** — writes directly over the Fulcra HTTP API via stdlib `urllib`, needs only a Fulcra token), or `cli` (legacy CLI shell-out). Resolution order: this env var (when set) > the persisted config (`fulcra-coord annotations on`, at `<XDG_CONFIG_HOME>/fulcra-coord/annotations`) > `off`. **Persist it once with `fulcra-coord annotations on`** so every agent emits without exporting this in each shell; set the env var to override a single session. See [docs/annotations.md](docs/annotations.md). |
| `FULCRA_API_BASE` | `https://api.fulcradynamics.com` | Fulcra HTTP API base for the `http` annotation transport. |
| `FULCRA_ACCESS_TOKEN` | _(unset)_ | Bearer token for the `http` annotation transport; when unset the writer falls back to `fulcra auth print-access-token`. |
| `FULCRA_COORD_SESSION_KEY` | — | Generic session pointer key for non-Claude-Code agents (OpenClaw passes its `sessionKey` here); `CLAUDE_CODE_SESSION_ID` takes precedence |
| `FULCRA_OPENCLAW_HOOKS_ROOT` | `~/.openclaw/hooks` | OpenClaw automation-hooks dir for `install-openclaw` |
| `FULCRA_OPENCLAW_PLUGIN_DIR` | `~/.openclaw/plugins/fulcra-coord` | Target dir for the materialized Track B plugin sources (`install-openclaw --with-plugin`); overridable via `--plugin-dir` |

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
  tasks/TASK-*.json         ← individual task files
  digest/
    markers/{date}-{window}.json ← per-window operator-digest dedup marker
                                   (first-writer-wins; any agent, any machine)
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
| | `schema.py` | task schema + state transitions |
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
  sessions. It's notify-only: a scheduled `fulcra-coord notify-inbox` polls the
  inbox and, if there are open directives, writes a surface file the next
  SessionStart injects and emits a desktop notification — it never runs the
  directive. The native Claude Code mechanism is a scheduled remote agent (the
  harness scheduler); `install-listener` is the harness-free launchd/cron
  fallback, and OpenClaw folds `notify-inbox` into its heartbeat. The listener is
  **per-agent**, not per-machine: its launchd label / plist / cron marker are
  derived from the agent's slug, so co-located agents on one machine each get
  their own coexisting job and none clobbers another. (A legacy pre-0.5.3
  machine-global job is migrated to a per-agent job on the next install.) See
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

## Docs

- `docs/protocol.md` — when and how to use coordination
- `docs/auth.md` — auth in local and remote/headless environments
- `docs/fulcra-cli-branch.md` — Fulcra CLI Files support requirement
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
