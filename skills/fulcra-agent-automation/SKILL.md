---
name: fulcra-agent-automation
description: "Keep a fulcra-agent-teams space healthy unattended: schedule coord-engine reconcile on a heartbeat so the index/views stay healed, and resume structured continuity on cron/heartbeat wake-ups."
homepage: "https://github.com/ashfulcra/fulcra-tools"
license: "MIT"
user-invocable: true
metadata: { "openclaw": { "emoji": "⏱️" } }
---

# Fulcra Agent Automation

Ties the coord skills together for **unattended** operation. `fulcra-agent-reconcile` heals a team's
index/views, but someone has to run it; this skill **schedules** it, and makes wake-ups
(cron/heartbeat) **resume structured continuity** first. Scheduling is a single, platform-specific action
(not a fold), so this skill is prose + one bundled install script — no engine logic.

**Require consent:** always ask the user before creating any scheduled job or background automation.

## Where to start — the re-entrancy probes

Before installing anything, probe what this host already runs. Enter at the **first probe that
fails** (per the repo's skill-quality pattern, `docs/skill-quality-pattern.md`):

| Probe (run in order) | Command | Passes when | If it fails, enter at |
|---|---|---|---|
| Engine usable? | `coord-engine doctor <team>` | exits 0 and last line is `doctor: healthy` | fix engine/auth first (see fulcra-agent-reconcile) — do NOT install jobs on a broken engine |
| Heartbeat installed? | `ls ~/Library/LaunchAgents/com.fulcra.coord-engine.heartbeat.<team>.plist` (macOS) or `crontab -l \| grep coord-engine.heartbeat.<team>` (Linux) | file/line exists | §1 (install the heartbeat) |
| Heartbeat loaded? | `launchctl list \| grep coord-engine.heartbeat.<team>` (macOS only) | a line appears | reinstall via §1 (plist exists but is not loaded — a reboot-era failure mode) |
| Listener installed + loaded? | `ls ~/Library/LaunchAgents/com.fulcra.coord-engine.listener.<team>.*.plist` and `launchctl list \| grep coord-engine.listener.<team>.` | file matches AND a launchctl line appears — then confirm the plist's `ProgramArguments` names the intended `<agent>` (the suffix is sanitized-agent+checksum; a wildcard can match a DIFFERENT agent's listener; never grep the raw id — colons sanitize to dashes) | §2 (install the listener) |
| Views actually fresh? | `coord-engine health <team>` | this host's row shows a recent `last reconcile` | jobs exist but are not ticking — check `log show` / cron mail, then reinstall |
| Claude Code hooks installed? | `ls ~/.claude/fulcra-agent-hooks/` | 3 scripts | §3 (install-claude-code.sh) |
| Codex watch coord-first? | `grep -l "coord watch" ~/.codex/hooks.json` | file matches | §3 (install_codex_watch.py) |
| OpenClaw block present? | `grep "fulcra-agent:begin" <workspace>/HEARTBEAT.md` | one match | §3 (install_openclaw.py) |

All probes pass → nothing to install; re-running any installer is safe anyway (reinstall replaces the
job, never duplicates — CI-tested for both the launchd and cron paths in
`packages/coord-engine/tests/test_installers.py`). Re-running after this upgrade also MIGRATES any
coord2-era artifacts (old `fulcra-coord2-hooks/` dir, `coord2-watch-<agent>` automation, `coord2 watch`
hooks marker, `fulcra-coord2:begin` fence) to the new names in place — old artifacts removed, zero
orphans (host-simulation-tested in `packages/coord-engine/tests/test_adapter_installers.py`).

## 1. Heartbeat — keep the views healed
Schedule `coord-engine reconcile <team>` on a timer so the index/aggregate never drift, even when no
agent is actively working.

**Bundled installer (macOS launchd / Linux cron):**
```bash
# every 20 minutes (default); creates a LaunchAgent on macOS or a crontab line on Linux
./scripts/install-heartbeat.sh <team> [interval-minutes]
./scripts/install-heartbeat.sh --uninstall <team>
```
It runs `coord-engine reconcile <team>`; needs `coord-engine` + an authenticated `fulcra-api` on PATH.

**OpenClaw / other runtimes:** add a line to your `HEARTBEAT.md` (or a native cron job, per
`fulcra-agent-teams`' automation section) that runs `coord-engine reconcile <team>` on your
chosen cadence. Prefer a longer interval or an external loop over waking the model every tick.

### Projection — task transitions onto your Fulcra timeline (model-free)
The heartbeat can project each task transition (create / pickup / update / complete) onto your Fulcra
timeline as an Agent-Tasks annotation, mechanically, spending no model tokens — and it annotates
transitions made by *any* agent or harness, not just this host. Reconcile already computes the
transitions; projection folds them onto the timeline right after.

Opt in **per team** (default is `off`):
```bash
coord-engine annotate resolution <team> transitions   # turn projection on for this team
coord-engine annotate resolution <team> off           # turn it back off
coord-engine annotate status <team>                    # resolution level + cursor position
```
The level is stored on the bus, so every host's heartbeat reads the same setting. With projection on,
`install-heartbeat.sh` runs `coord-engine annotate project <team>` immediately after each `reconcile` —
it consumes the structured `pending.json` transitions reconcile just wrote (the `log.md` bullets can't
feed the fold — they carry no task_id/kind/ts), emits one annotation per new transition (deterministic
id + cursor, so a re-run or mid-run crash never double-writes), and advances the cursor. Off or absent
⇒ the step no-ops. The heartbeat chain then finishes with `coord-engine digest <team> --store
--emit-timeline`, which keeps the operator's twice-daily digest alive on both surfaces (bus copy +
the 'Agent Tasks — Digest' timeline track) — see the health skill for its semantics.

Multi-host is safe: the typed ingest endpoint **upserts on an explicit record id** (live-verified
2026-07-14 — a same-id re-POST returns 201 and the record count stays 1), and every projected
annotation carries a deterministic id, so two hosts racing `reconcile && annotate project` in the
same window converge on the same records instead of duplicating them. The cursor still matters — it
is what keeps quiet ticks cheap (no re-POSTs) — but it is an efficiency guard, not the only thing
between you and duplicates. Run projection from every heartbeat host.

`resolution` is a **level axis, not a boolean**: `{off, transitions}` are live today; finer levels (tool
calls, I/O, …) are additive later without a config-shape change. Any other value is rejected.

**Projection is the successor to the in-process `fulcra-coord annotations` writer.** Both emit
Agent-Tasks moments for the same transition to a no-dedup endpoint, so running both double-writes the
timeline. Enabling projection therefore requires the legacy annotations writer stay off — which the
standing rule ([`AGENTS.md` → Fulcra platform surface](../../AGENTS.md)) already mandates on every host.
Projection is the sanctioned replacement; do not switch the legacy writer back on to get timeline
annotations — turn projection on instead.

## 2. Listener — await new directives, responses, and verdicts (the reply leg)
Every agent that sends an ask (`tell`/`broadcast`/`remind`/`review request`) should **arm a listener**:
the send verbs now print the exact `listen` line to run for replies. `listen` is the engine-owned await
leg — one implementation of the diff/notify logic that the launchd tick, live sessions, Codex, and
headless all delegate to. Each tick id-diffs three sources against a per-agent state file: **new inbox
directives** for the agent — including directives routed to a **role you hold a fresh lease on** (the
same fold `inbox`/`briefing` show), excluding unscheduled self-authored rows (self-tells and your own
broadcasts do not wake you; `remind` yourself does, at WHEN); **responses to directives you own** (the return of
`respond`); and **new verdicts on reviews you requested** (the await leg of `review request`, including
the terminal `SETTLED <slug>` line when a review closes). It also surfaces **orphan review dirs** (a
`<slug>/` verdicts dir with no `<slug>.md` doc) as a one-time `ORPHAN` event — visibility only, repair
stays a maintainer action. One line per new item — `DIRECTIVE`/`RESPONSE`/`VERDICT`/`SETTLED`/`ORPHAN`,
or one JSON object per line under `--json`. Quiet ticks print nothing. A transport failure prints
`LISTEN DEGRADED: <what>` to stderr **once per source per streak** across five independent sources —
`inbox`, `responses`, `orphans`, `verdicts`, and `roles` (an unreadable role-lease listing while
resolving role-routed directives; independent so a chronic role failure can't mask a fresh inbox
outage). A degraded read never advances state, so the pending event re-surfaces on recovery.

`listen` is the reply-leg watcher; the load-bearing **wake** read is the composite `briefing` path.
When a scheduled tick's `briefing`/`inbox` degrades, quiet is NOT clear — apply the raw-bus fallback
in §3 (**Degraded briefing → fail-closed raw-bus fallback**): raw-list + direct-read the unacked
directives before reporting, never conclude "no work" off a degraded read.

```
coord-engine listen <team> --agent <agent> [--interval N=60] [--once] [--json] [--verbose]
```
`--once` runs exactly one tick and exits **0** on a clean tick (including "nothing new") or **3** when
the tick itself captured degradation (transport failure, unreadable source) — so a scheduler treats no
output as "nothing new", while a monitoring wrapper can distinguish a degraded tick from a quiet one
without parsing stderr. Long-running mode loops at `--interval` seconds and exits cleanly on SIGINT.

### Per-platform — pick the leg that matches how the agent runs
- **launchd / cron (unattended host):** the bundled installer — unchanged UX. The scheduled job's tick
  delegates to `listen --once` and keeps the notification + consent-gated wake around it.
  ```bash
  ./scripts/install-listener.sh <team> <agent> [active-minutes]            # adaptive: default 1m/30m tail/30m idle
  ./scripts/install-listener.sh <team> <agent> 1 --tail-minutes 30 --idle-minutes 60
  ./scripts/install-listener.sh <team> <agent> 1 --wake-cmd "…headless…"    # + consent-gated wake
  ./scripts/install-listener.sh <team> <agent> 10 --fixed                  # legacy fixed cadence
  ./scripts/install-listener.sh --uninstall <team> <agent>
  ```
  On NEW events it posts a macOS notification (or a log line) and, only if you consented to `--wake-cmd`
  at install, runs your wake command. A healthy quiet tick emits nothing by default
  (`COORD_LISTENER_VERBOSE=1` restores diagnostic quiet lines). Listener stderr is never discarded:
  a newly emitted `LISTEN DEGRADED` diagnostic is forwarded and also wakes the consented adapter so
  the session can run the targeted fallback. Wake adapters receive fixed advisory environment fields
  (`COORD_LISTENER_TEAM`, `COORD_LISTENER_AGENT`, `COORD_LISTENER_DEGRADED`,
  `COORD_LISTENER_RETRY`, `COORD_LISTENER_EVENT_REFS`) plus the legacy advisory
  `COORD_LISTENER_OUTPUT`, and must still fetch the authoritative briefing.
  `COORD_LISTENER_EVENT_REFS` contains only validated `KIND:canonical-slug` pairs;
  adapters may use it for targeted orientation but must never evaluate it as code.
  Raw titles, outcomes, authors, and bodies remain excluded from bundled wake
  prompts. `--yes` skips BOTH the schedule prompt and the wake-command
  acknowledgement — only use it when that consent was already given. By default the scheduler ticks
  every active minute, but the model-free tick uses a local due-time gate: affirmative events keep
  the work tail hot, healthy quiet polling backs off to `--idle-minutes`, and degradation follows a
  separate exponential retry backoff capped at that idle cadence. A failed wake is durably retried on
  later ticks even though `listen` has already advanced its event cursor; wake
  delivery has its own exponential backoff capped at the idle cadence, so a
  persistently unavailable harness cannot spawn a model attempt every hot minute. The idle
  interval is the maximum added pickup latency until Fulcra exposes push; `--fixed` preserves the old
  behavior. `COORD_LISTENER_FORCE=1` bypasses only the due gate, while
  `COORD_LISTENER_MARK_ACTIVE=1` also restarts the hot tail for trusted lifecycle adapters. Hardened like the heartbeat:
  validated inputs, pinned `PATH`/`HOME` (scheduled jobs source no profile — the parent project's wake
  silently 401'd on exactly this), `plutil` lint, install-time self-test.
- **Claude Code live session:** THE one command is `coord-engine listen` wrapped in the harness's
  background monitor — no hand-rolled watcher. The monitor surfaces each event line
  (`DIRECTIVE`/`RESPONSE`/`VERDICT`/`SETTLED`/`ORPHAN`) into the session as it arrives, closing the
  live-session gap where an agent waiting on a reply used to poll by hand:
  ```bash
  coord-engine listen <team> --agent <agent> --interval 60
  ```
- **Codex:** one automation-prompt line, ticked once per automation run:
  ```bash
  coord-engine listen <team> --agent <agent> --once
  ```
- **headless / any foreground process:** run the loop in the foreground and stream its output:
  ```bash
  coord-engine listen <team> --agent <agent>
  ```

For push-capable harnesses and the fleet security contract, see
[`docs/coord/EVENT-DRIVEN-WAKE.md`](../../docs/coord/EVENT-DRIVEN-WAKE.md). OpenClaw can use the
bundled fixed adapter at `scripts/wake/openclaw.sh`; Codex Desktop and Claude Code UI sessions retain
the documented safety nets appropriate to their harness. Codex's stable exact-thread adapter is
`scripts/wake/codex.sh`; it uses `codex exec resume` without bypassing approvals or sandboxing:
```bash
COORD_CODEX_THREAD_ID=<thread-id> COORD_CODEX_CWD=<repo> \
  ./scripts/install-listener.sh <team> <agent> 1 \
  --wake-cmd "COORD_CODEX_THREAD_ID=<thread-id> COORD_CODEX_CWD=<repo> /absolute/path/to/scripts/wake/codex.sh"
```

**Single-flight — one watcher identity per agent.** Run exactly one listener per `<agent>` on a host:
the per-agent state file is not a concurrency lock, so two listeners for the same agent (or a canonical
listener running alongside a legacy alias listener for the same identity) race the state and double-fire
or drop events. Serialize ticks (`--once` on a scheduler, or a single long-running `--interval` loop —
never both), coalesce overlapping schedules onto one cadence rather than stacking timers, and retire any
legacy alias listener before arming the canonical one (the scheduling-overlap P1). Distinct agents get
distinct state files and run independently; the constraint is per-identity.

## 3. Harness adapters — lifecycle wiring
The listener (§2) delivers inbox notifications, but the **lifecycle contract** — resume-on-wake,
snapshot-on-change, park-before-context-loss — is owned by a per-harness adapter that hooks the
platform's own session events. The contract itself (rules 1–4) lives in
[`fulcra-agent-continuity` §The lifecycle contract](../fulcra-agent-continuity/SKILL.md); the adapters
below automate it. Each keys everything on a distinct `coord` marker. Retired
first-generation entries are left inert unless an installer's documented
migration path explicitly recognizes them. All installers are idempotent
(reinstall replaces, never duplicates) and ship an `--uninstall` inverse.

**Tick doctrine (shared by every adapter).** Every adapter keys off the same canonical, briefing-led
tick — though claude-code's hook renders only steps 1–2 (`continuity resume` + `briefing`) as session
context, leaving the verdict-before-ack duty steps to the review skill's reviewer procedure:
`continuity resume` → `briefing` (THE entry fold — identity, role inboxes, needs-me incl. pending
reviews) → for each review request, **slug-exact verdict-before-ack** (write the verdict file, verify
`review status` clears you from `pending_required`, only then ack — never ack bare or against a different
slug) → handle other work → `continuity snapshot` → `usage log` (ATC, when accounts are declared) →
`continuity park` before session end → **report last**: the human-visible summary is the tick's final
output, composed after every command above. Text followed by more tool activity may never render —
"sent" is not "delivered" — so anything that MUST reach a recipient (human or agent) goes on the bus
as a durable artifact (ask, review doc, snapshot), never only in session text. PR/forge feedback
arrives via `briefing` (forge mirror sweeps all three GitHub surfaces) — never hand-roll `gh` polls
in a watch prompt. **These
hooks/prompts/blocks are rendered artifacts, not live references:** after upgrading `fulcra-tools`,
**RE-RUN YOUR ADAPTER INSTALLER** to regenerate them — an un-regenerated hook keeps emitting the
doctrine it was rendered under.

**Degraded briefing → fail-closed raw-bus fallback (doctrine, not optional).** A wake read that
degrades is *absence of a complete answer, never proof of "all clear"* — and the fallback covers
**every** degraded section, not just reviews. A watcher that acts only on what a clean-looking fold
returned can silently drop a live unacked directive.

- **Directives / inbox — the general case.** Every aggregate-backed read (`briefing`, `inbox`,
  `needs-me`, `status`, `board`, `search`) folds the summaries index through the public-read failure
  contract ([`AGENTS.md` → The public-read failure contract](../../AGENTS.md)): when the index/listing
  is UNKNOWN it emits the shared `read-degraded` marker (or `inbox`'s named `inbox-degraded` type) —
  carried in the `--json` result and as a stderr notice — instead of a clean-empty. On ANY such marker
  (or a `briefing` that reports a failed resume / stalled section), the watcher MUST NOT conclude "no
  work": it **raw-lists and direct-reads the unacked directives** — enumerate `team/<team>/task/`
  (`fulcra-api file list`), read each `intent:`/`assignee`-shaped doc naming this agent or a role it
  holds, and act on anything open-and-unacked. Only report a genuinely clear inbox when a *non-degraded*
  read returns empty; a degraded read is reported **degraded**, never "no directives."
- **Reviews — the specific case.** The `briefing`/`needs-me` pending-review fold is wall-clock bounded
  (`COORD_REVIEW_FOLD_BUDGET`, default 45s): on a slow transport it stops early and emits a
  `review-fold-degraded` row (`{scanned, total}`, plus `skipped` when a slug's doc or verdict read
  failed) rather than a clean-looking partial. On that row, fall back to a per-slug `review status`
  sweep over the `review/` listing for the unscanned and skipped remainder, and clear those verdicts
  before acking.

Codex's repaired watch prompt already does the review sweep; the **directive raw-bus fallback is the
same discipline for the inbox side** — the installer-generated watcher (§2) and every adapter tick
(below) run the composite engine path (`briefing` first) and honor both markers before reporting.

**Claude Code / Cowork** — settings.json hooks.
```bash
./scripts/claude-code/install-claude-code.sh <team> <agent>
./scripts/claude-code/install-claude-code.sh --uninstall <team> <agent>
```
Writes three scripts to `~/.claude/fulcra-agent-hooks/` and merges their command paths into
`~/.claude/settings.json`: **SessionStart** → bounded `continuity resume` + inbox brief injected as
context; **PreCompact** and **SessionEnd** → backgrounded `continuity park`. It touches only its own
command paths; a coord2-era `fulcra-coord2-hooks` install is migrated to the new
dir in place. Pre-coord first-generation hooks are not managed by this installer
and should be removed separately. Cowork uses the same core
and the same settings.json, so the same installer covers it.

**Codex** — `hooks.json` merge + app-thread automation.
```bash
python3 scripts/codex/install_codex_watch.py <team> <agent> [--codex-dir DIR] [--thread-id ID] [--interval-minutes N] [--uninstall] [--dry-run]
```
Merges SessionStart (matcher `startup|resume|clear|compact`) + PreCompact entries into
`~/.codex/hooks.json` — same entry shape as Claude Code — and seeds a coord-first app-thread automation
under `~/.codex/automations/coord-watch-<agent>/` whose prompt embeds contract rules 1–3 and ticks the
inbox. The default safety-net cadence is 30 minutes (override with `--interval-minutes`), replacing the
old 5-minute model-backed poll. For event-driven wake, pair its exact thread id with
`scripts/wake/codex.sh` through the consent-gated listener command above. The adapter uses the stable
`codex exec resume <SESSION_ID>` interface, never passes
`--dangerously-bypass-approvals-and-sandbox`, and never places raw bus event text in the prompt; the
resumed agent fetches authoritative briefing state. Deployment precondition: on the first real host, verify the SessionStart hook actually fires
before relying on hook-based automation seeding — pass `--thread-id` for the deterministic path if you
already know the watch thread.

**OpenClaw** — managed prose block.
```bash
python3 scripts/openclaw/install_openclaw.py <team> <agent> [--workspace DIR] [--uninstall] [--dry-run]
```
Merges a `fulcra-agent`-fenced block (`<!-- fulcra-agent:begin … -->` / `<!-- fulcra-agent:end -->`)
into the workspace's `HEARTBEAT.md` and `BOOT.md`, embedding contract rules 1–2 for OpenClaw to read at
boot and on each heartbeat tick. The managed blocks are intentionally compact because OpenClaw reads
them repeatedly; detailed doctrine stays in this skill. Rule 3 (park on shutdown) is **not automated** — the prose-block layer
has no shutdown hook to fire it, so it must be followed as prose. This is the **prose-block layer
only** — no hooks-dir machinery. It
validates marker balance before any write and **refuses (exit 1) on unbalanced or crossed markers**
rather than risk destroying user content between an orphan marker and an appended one.

**Hermes (Daytona sandbox)** — provisioned out of band: the `fhd` provisioner that installs the Claude
Code adapter inside the sandbox lives in the standalone `hermes-daytona` repo (extracted from this
monorepo) and is tracked there, not here.

**Claude web / Cowork-cloud tier (best-effort):** cloud sessions have no persistent
filesystem or scheduler on your machine. Follow the lifecycle contract as prose
(fulcra-agent-continuity §contract), and use the platform's scheduled routines to
open a periodic duty-cycle session that runs steps 1–2 of the contract. There is
no durable background pickup on this tier; anything that must not wait for a
routine belongs with an agent on a host tier.

## 4. Resume on wake — structured, not a prose re-read
When a cron/heartbeat wakes an agent to do team work, the wake payload should **resume continuity first**
(this is the structured version of `fulcra-agent-teams`' "read progress.md before acting" rule):
```bash
coord-engine continuity resume <team> <agent>
```
Then process the team inbox and, before concluding, snapshot again
(`coord-engine continuity snapshot …`) and let the next reconcile heal the views.

## 5. Recommended loop for a team
1. **Heartbeat**: `install-heartbeat.sh <team>` — reconcile every ~20m (consent first).
2. **On wake**: `continuity resume` → do work (`task …`, inbox) → `continuity snapshot` → `reconcile`.
3. **Gate merges** with `fulcra-agent-review` (`review status`), and keep roles fresh with
   `fulcra-agent-roles` (`roles status`), escalating vacancies.

That's the full coord stack running unattended on top of a `fulcra-agent-teams` space.

See the bundled [`scripts/install-heartbeat.sh`](scripts/install-heartbeat.sh).
