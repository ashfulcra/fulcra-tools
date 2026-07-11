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
⇒ the step no-ops.

The cursor's deterministic id dedups within a host, but the endpoint has no server-side dedup: if two
hosts run `reconcile && annotate project` in the same window, both read the same cursor and pending and
both emit, duplicating each transition on the timeline. Until a bus lease serializes projection
(follow-up), run projection from a **single** host — keep `annotate resolution transitions` scoped to
one heartbeat and leave the others at `off`.

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
directives** for the agent — including directives routed to a **role you hold a fresh lease on** (a
strict superset of the bare `inbox` fold); **responses to directives you own** (the return of
`respond`); and **new verdicts on reviews you requested** (the await leg of `review request`, including
the terminal `SETTLED <slug>` line when a review closes). It also surfaces **orphan review dirs** (a
`<slug>/` verdicts dir with no `<slug>.md` doc) as a one-time `ORPHAN` event — visibility only, repair
stays a maintainer action. One line per new item — `DIRECTIVE`/`RESPONSE`/`VERDICT`/`SETTLED`/`ORPHAN`,
or one JSON object per line under `--json`. Quiet ticks print nothing. A transport failure prints
`LISTEN DEGRADED: <what>` to stderr **once per source per streak** across five independent sources —
`inbox`, `responses`, `orphans`, `verdicts`, and `roles` (an unreadable role-lease listing while
resolving role-routed directives; independent so a chronic role failure can't mask a fresh inbox
outage). A degraded read never advances state, so the pending event re-surfaces on recovery.

```
coord-engine listen <team> --agent <agent> [--interval N=60] [--once] [--json] [--verbose]
```
`--once` runs exactly one tick and **always exits 0** — a tick never fails the schedule, so a scheduler
re-running it treats no output as "nothing new", not an error. Long-running mode loops at `--interval`
seconds and exits cleanly on SIGINT.

### Per-platform — pick the leg that matches how the agent runs
- **launchd / cron (unattended host):** the bundled installer — unchanged UX. The scheduled job's tick
  delegates to `listen --once` and keeps the notification + consent-gated wake around it.
  ```bash
  ./scripts/install-listener.sh <team> <agent> [interval-minutes]           # notify only (default 10m)
  ./scripts/install-listener.sh <team> <agent> 10 --wake-cmd "…headless…"   # + consent-gated wake
  ./scripts/install-listener.sh --uninstall <team> <agent>
  ```
  On NEW events it posts a macOS notification (or a log line) and, only if you consented to `--wake-cmd`
  at install, runs your wake command. `--yes` skips BOTH the schedule prompt and the wake-command
  acknowledgement — only use it when that consent was already given. Hardened like the heartbeat:
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
below automate it. Each keys everything on a distinct `coord` marker and coexists with the legacy
`fulcra-coord` adapters until the phase-3 freeze retires them — installing one never touches a legacy
entry. All installers are idempotent (reinstall replaces, never duplicates) and ship an `--uninstall`
inverse.

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

**Degraded review fold.** The `briefing`/`needs-me` fold that surfaces pending reviews is wall-clock
bounded (`COORD_REVIEW_FOLD_BUDGET`, default 45s): on a slow transport it stops early and emits a
`review-fold-degraded` row (`{scanned, total}`, plus `skipped` when a slug's doc or verdict read failed)
rather than a clean-looking partial. A watcher that sees it must NOT treat the fold as authoritative —
fall back to a per-slug `review status` sweep over the `review/` listing for the unscanned and skipped
remainder, and clear those verdicts before acking. Codex's repaired watch prompt already does this; the
fallback is doctrine, not optional.

**Claude Code / Cowork** — settings.json hooks.
```bash
./scripts/claude-code/install-claude-code.sh <team> <agent>
./scripts/claude-code/install-claude-code.sh --uninstall <team> <agent>
```
Writes three scripts to `~/.claude/fulcra-agent-hooks/` and merges their command paths into
`~/.claude/settings.json`: **SessionStart** → bounded `continuity resume` + inbox brief injected as
context; **PreCompact** and **SessionEnd** → backgrounded `continuity park`. It touches only its own
command paths, so legacy `fulcra-coord-hooks` keep firing until the freeze; a coord2-era
`fulcra-coord2-hooks` install is migrated to the new dir in place. Cowork uses the same core
and the same settings.json, so the same installer covers it.

**Codex** — `hooks.json` merge + app-thread automation.
```bash
python3 scripts/codex/install_codex_watch.py <team> <agent> [--codex-dir DIR] [--thread-id ID] [--uninstall] [--dry-run]
```
Merges SessionStart (matcher `startup|resume|clear|compact`) + PreCompact entries into
`~/.codex/hooks.json` — same entry shape as Claude Code — and seeds a coord-first app-thread automation
under `~/.codex/automations/coord-watch-<agent>/` whose prompt embeds contract rules 1–3 and ticks the
inbox. The consent-gated `wake.json` host-wake layer is **deliberately not shipped** (security ruling:
it spawns headless `codex exec` with approvals/sandbox bypassed; the coord listener already covers
wake). Deployment precondition: on the first real host, verify the SessionStart hook actually fires
before relying on hook-based automation seeding — pass `--thread-id` for the deterministic path if you
already know the watch thread.

**OpenClaw** — managed prose block.
```bash
python3 scripts/openclaw/install_openclaw.py <team> <agent> [--workspace DIR] [--uninstall] [--dry-run]
```
Merges a `fulcra-agent`-fenced block (`<!-- fulcra-agent:begin … -->` / `<!-- fulcra-agent:end -->`)
into the workspace's `HEARTBEAT.md` and `BOOT.md`, embedding contract rules 1–2 for OpenClaw to read at
boot and on each heartbeat tick. Rule 3 (park on shutdown) is **not** embedded — the prose-block layer
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
