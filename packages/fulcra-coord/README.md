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
| `snapshot` | Write a Fulcra Continuity-compatible checkpoint without changing task state (`--reason`, `--transcript-path`, optional `--next`). Used by compaction/idle hooks to capture resume state at session boundaries (bounded by `FULCRA_COORD_CONTINUITY_KEEP` retention, not by suppression) |
| `done` | Mark done (requires evidence) |
| `abandon` | Mark abandoned |
| `reconcile` | Repair views and resolve pending markers |
| `search` | Search tasks by text |
| `doctor` | Check configuration and connectivity |
| `install-shim` | Install CLI shim to `~/.local/bin/` |
| `install-claude-code` | Install Claude Code lifecycle hooks (global by default) |
| `install-openclaw` | Install OpenClaw Track A artifacts (boot/heartbeat prompts + shutdown/bootstrap hooks); add `--with-plugin` to also materialize the Track B Plugin-SDK plugin; add `--with-heartbeat --with-listener --agent <id>` to bundle the durable bus-pickup path (reuses `install-heartbeat` + the per-agent `install-listener`) in one command, so a fresh OpenClaw agent hears directed work without a separate step (the OpenClaw analogue of `ensure-codex-watch`) |
| `install-codex` | Install Codex lifecycle hooks (SessionStart + PreCompact) into `~/.codex/hooks.json`. No Stop hook by design — Codex end-parking is delegated to the heartbeat |
| `ensure-codex-watch` | Idempotently (re)arm Codex coordination in one shot — installs Codex hooks, the per-agent inbox listener, best-effort `launchctl load`s it (`--no-load` to skip), optionally refreshes presence (`--no-connect`). Codex SessionStart runs it backgrounded each app start so a missing listener self-heals. Idempotent (`--agent`, `--set-identity`, `--can-review`, `--interval-min N`, `--dry-run`) |
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
| `FULCRA_COORD_BROADCAST_EXPIRY_DAYS` | `14` | A still-`proposed` **broadcast** (`assignee="*"`) whose `created_at` is older than this is transitioned `proposed → abandoned` by the reconcile retention pass, after which cold-archive sweeps it out of the hot path on a later pass — so never-claimed broadcasts stop cluttering `status` instead of living on the bus forever (they already leave the `inbox` at `FULCRA_COORD_INBOX_AGE_DAYS`). Unlike that read filter this **changes status**, but it is recoverable via `fulcra-coord restore`, and — like the inbox filter — it **only expires broadcasts**: a directive addressed to a concrete agent (a real ask) is never expired regardless of age. Clockless broadcasts (missing/unparseable `created_at`) are never expired (fail-safe). Reconcile reports `expired N broadcast(s)` in its Retention line |
| `FULCRA_COORD_NOTIFY_WEBHOOK` | _(unset)_ | Opt-in real-time push endpoint for the listener (Tier 1). When set, `notify-inbox` POSTs a notification to this URL via stdlib `urllib` — the push that reaches the operator's phone regardless of OS or which host fired. Unset → push disabled, native-desktop only. Works with any commodity service (a free / self-hosted ntfy topic, Pushover-style, Slack, Discord) — it is **not** tied to any specific infrastructure |
| `FULCRA_COORD_NOTIFY_FORMAT` | _(auto)_ | Payload shape for the webhook POST: `ntfy\|slack\|discord\|json`. Auto-detected from the URL host (`discord` → Discord JSON, `slack` → Slack JSON, else **ntfy** plain-body, the generic default); set this to override the detection |
| `FULCRA_COORD_NOTIFY_TIMEOUT` | `5` | Seconds before the webhook POST gives up, so a slow/hung push endpoint can't stall a polling tick |
| `FULCRA_COORD_CONTINUITY_KEEP` | `10` | How many of the newest **continuity checkpoint** archives to keep per task. `continuity/<ws>/<agent>/<task>/checkpoints/CHK-*.json` is written immutably on every snapshot (SessionEnd / PreCompact / compaction) and would otherwise grow without bound; the reconcile retention pass keeps the newest N per task and deletes the rest (`latest.json` is never touched — it's the live pointer a resuming agent reads). Floored at `1` so the latest checkpoint is never deleted. Reconcile reports `N continuity` in its Retention line |
| `FULCRA_COORD_READ_SOURCE` | `file` | Where task **bodies** are reconstructed from (per host, reversible). `file` (default) reads the mutable `tasks/<id>.json`, byte-identical to pre-substrate behaviour. `events` folds the task's immutable event log and uses the fold **only** when it's a complete snapshot, falling back to the file on a delta-only / empty / errored fold. Opting a single host into `events` is the validation step for the read cutover; flipping the fleet default is a deliberate operator decision gated on parity (see [Event-sourcing substrate](#event-sourcing-substrate-the-durable-event-log)). Any unrecognised value degrades to `file`, so a typo can never silently flip the read path |
| `FULCRA_COORD_EVENTLOG_KEEP` | `20` | How many of the newest **event-log shards** to keep per LIVE task. Every task mutation appends `events/tasks/<id>/<event_id>.json` forever, so the reconcile retention pass window-prunes each live task below its latest snapshot — keeping that snapshot plus the most recent N events — and GCs the whole shard tree of archived/deleted tasks. A delta-only task (no snapshot ever emitted) is **never** pruned (fail-safe: a delta may carry a unique field never re-set). Floored at `1`. Reconcile reports `N events` in its Retention line |
| `FULCRA_COORD_AGENT` | — | Session-scoped override for your agent id. Identity resolution order is: explicit `--agent` > `FULCRA_COORD_AGENT` > per-cwd persisted identity (`fulcra-coord identity set`) > derived `claude-code:<host>:<repo>` (matching the SessionStart hook) |
| `FULCRA_COORD_HUMAN` | `human` | The human operator's handle — who tasks are "blocked on ME" against (`needs-me`, `block --on-user`). Resolution order: `FULCRA_COORD_HUMAN` > persisted handle (`fulcra-coord human set`) > default `human`. Personalize with `fulcra-coord human set <name>` |
| `FULCRA_COORD_BACKEND` | — | Override backend (testing only) |
| `FULCRA_COORD_ANNOTATIONS` | `off` | Emit lifecycle annotations to the Fulcra **Agent Tasks** timeline track: `off` (default, inert), `http` (alias `api`, **recommended** — writes directly over the Fulcra HTTP API via stdlib `urllib`, needs only a Fulcra token), or `cli` (legacy CLI shell-out). Resolution order: this env var (when set) > the persisted config (`fulcra-coord annotations on`, at `<XDG_CONFIG_HOME>/fulcra-coord/annotations`) > `off`. **Persist it once with `fulcra-coord annotations on`** so every agent emits without exporting this in each shell; set the env var to override a single session. See [docs/annotations.md](docs/annotations.md). |
| `FULCRA_API_BASE` | `https://api.fulcradynamics.com` | Fulcra HTTP API base for the `http` annotation transport. |
| `FULCRA_ACCESS_TOKEN` | _(unset)_ | Bearer token for the `http` annotation transport; when unset the writer falls back to `fulcra auth print-access-token`. |
| `FULCRA_COORD_SESSION_KEY` | — | Generic session pointer key for non-Claude-Code agents (OpenClaw passes its `sessionKey` here); `CLAUDE_CODE_SESSION_ID` takes precedence |
| `FULCRA_OPENCLAW_HOOKS_ROOT` | `~/.openclaw/hooks` | OpenClaw automation-hooks dir for `install-openclaw` |
| `FULCRA_OPENCLAW_PLUGIN_DIR` | `~/.openclaw/plugins/fulcra-coord` | Target dir for the materialized Track B plugin sources (`install-openclaw --with-plugin`); overridable via `--plugin-dir` |

## Roles & review-routing

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

Alongside the mutable `tasks/<id>.json` files, the bus now keeps an immutable,
append-only **event log**. This is a strangler-fig migration: the event log is
written today, validated in parallel, and only takes over reads once parity
proves it's safe. The mutable file stays authoritative until that flip.

**The model.** Every task mutation (`_write_task_and_views`) does two things:
upload the task file as before, *and* — best-effort, default-ON — append one
immutable shard `events/tasks/<id>/<event_id>.json`. The `event_id` is a
time-sortable `<sortable-ts>-<rand>` (the canonical UTC-microsecond instant of
the event plus a random suffix), so every append lands on a *distinct* path —
two concurrent writers for the same task in the same microsecond never collide,
which is what lets a no-CAS, no-lock store stay correct. The dual-write is
best-effort by design: an event-append failure is logged but **never** fails the
task write (Phase 1's job is to validate the dual-write, not to depend on it).

**The fold.** `events.fold_task` is a pure reducer that reduces a task's events
back to a snapshot. It (1) sorts by `(canonical-microsecond-instant, event_id)`
— canonicalizing the timestamp so lexical order equals chronological order, NOT
a raw-string compare which inverts when timestamps differ only in trailing
precision; (2) dedups retries by the compound `(actor, idempotency_key)` pair,
first-in-sort-order wins; and (3) merges by payload type. A **snapshot** payload
(a full task, carrying both `schema` and `id`) *replaces* the accumulated state
wholesale — the latest snapshot wins and stale fields drop. A legacy **delta**
payload (a field subset, Phase-1 events) field-merges last-write-wins. The two
compose in any order. `fold_is_complete` is true once at least one full snapshot
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

A first-class **Directive** record (Phase 3a, schema
`fulcra.coordination.directive.v1`, built/validated by `schema.make_directive` /
`validate_directive` against `DIRECTIVE_SCHEMA`) also lands as an *additive*
schema. It models *communication* (who told whom what) as its own record type
rather than a task-with-assignee. It is **not yet wired into the lifecycle** —
nothing produces or consumes it on the bus today; its dual-write arrives in a
later phase. It's documented here so its presence in the code isn't mistaken for
an active path.

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
| | `schema.py` | task schema + state transitions; the additive Phase-3a `make_directive`/`validate_directive` record (not yet wired in) |
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
  machine-global job is migrated to a per-agent job on the next install.)
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

## Docs

- `docs/protocol.md` — when and how to use coordination
- `docs/auth.md` — auth in local and remote/headless environments
- `docs/continuity-handoff.md` — how `fulcra-coord` and Fulcra Continuity work together for cross-agent, non-GitHub handoff
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
