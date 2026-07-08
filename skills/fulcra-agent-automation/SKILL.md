---
name: fulcra-agent-automation
description: "Keep a fulcra-agent-teams space healthy unattended: schedule coord-engine reconcile on a heartbeat so the index/views stay healed, and resume structured continuity on cron/heartbeat wake-ups."
homepage: "https://github.com/ashfulcra/fulcra-tools"
license: "MIT"
user-invocable: true
metadata: { "openclaw": { "emoji": "⏱️" } }
---

# Fulcra Agent Automation

Ties the coord2 skills together for **unattended** operation. `fulcra-agent-reconcile` heals a team's
index/views, but someone has to run it; this skill **schedules** it, and makes wake-ups
(cron/heartbeat) **resume structured continuity** first. Scheduling is a single, platform-specific action
(not a fold), so this skill is prose + one bundled install script — no engine logic.

**Require consent:** always ask the user before creating any scheduled job or background automation.

## Where to start — the re-entrancy probes

Before installing anything, probe what this host already runs. Enter at the **first probe that
fails** (per the repo's skill-quality pattern, `docs/skill-quality-pattern.md`):

| Probe (run in order) | Command | Passes when | If it fails, enter at |
|---|---|---|---|
| Engine usable? | `uv tool run coord-engine doctor <team>` | exits 0 and last line is `doctor: healthy` | fix engine/auth first (see fulcra-agent-reconcile) — do NOT install jobs on a broken engine |
| Heartbeat installed? | `ls ~/Library/LaunchAgents/com.fulcra.coord-engine.heartbeat.<team>.plist` (macOS) or `crontab -l \| grep coord-engine.heartbeat.<team>` (Linux) | file/line exists | §1 (install the heartbeat) |
| Heartbeat loaded? | `launchctl list \| grep coord-engine.heartbeat.<team>` (macOS only) | a line appears | reinstall via §1 (plist exists but is not loaded — a reboot-era failure mode) |
| Listener installed + loaded? | `ls ~/Library/LaunchAgents/com.fulcra.coord-engine.listener.<team>.*.plist` and `launchctl list \| grep coord-engine.listener.<team>.` | file matches AND a launchctl line appears — then confirm the plist's `ProgramArguments` names the intended `<agent>` (the suffix is sanitized-agent+checksum; a wildcard can match a DIFFERENT agent's listener; never grep the raw id — colons sanitize to dashes) | §2 (install the listener) |
| Views actually fresh? | `uv tool run coord-engine health <team>` | this host's row shows a recent `last reconcile` | jobs exist but are not ticking — check `log show` / cron mail, then reinstall |
| Claude Code hooks installed? | `ls ~/.claude/fulcra-coord2-hooks/` | 3 scripts | §3 (install-claude-code.sh) |
| Codex watch coord2-first? | `grep -l "coord2 watch" ~/.codex/hooks.json` | file matches | §3 (install_codex_watch.py) |
| OpenClaw block present? | `grep "fulcra-coord2:begin" <workspace>/HEARTBEAT.md` | one match | §3 (install_openclaw.py) |

All probes pass → nothing to install; re-running any installer is safe anyway (reinstall replaces the
job, never duplicates — CI-tested for both the launchd and cron paths in
`packages/coord-engine/tests/test_installers.py`).

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
`fulcra-agent-teams`' automation section) that runs `uv tool run coord-engine reconcile <team>` on your
chosen cadence. Prefer a longer interval or an external loop over waking the model every tick.

## 2. Listener — inbox notifications + consent-gated wake (A8)
Schedule an inbox check so directed work reaches you without polling:
```bash
./scripts/install-listener.sh <team> <agent> [interval-minutes]           # notify only (default 10m)
./scripts/install-listener.sh <team> <agent> 10 --wake-cmd "…headless…"   # + consent-gated wake
./scripts/install-listener.sh --uninstall <team> <agent>
```
Each tick runs `coord-engine inbox --agent <agent>`; on NEW items it posts a macOS notification (or a
log line) and, only if you consented to `--wake-cmd` at install, runs your wake command. Note: `--yes` skips BOTH the schedule prompt and the wake-command acknowledgement — only use it when
that consent was already given. Hardened like
the heartbeat: validated inputs, pinned `PATH`/`HOME` (scheduled jobs source no profile — the parent
project's wake silently 401'd on exactly this), `plutil` lint, install-time self-test.

## 3. Harness adapters — lifecycle wiring
The listener (§2) delivers inbox notifications, but the **lifecycle contract** — resume-on-wake,
snapshot-on-change, park-before-context-loss — is owned by a per-harness adapter that hooks the
platform's own session events. The contract itself (rules 1–4) lives in
[`fulcra-agent-continuity` §The lifecycle contract](../fulcra-agent-continuity/SKILL.md); the adapters
below automate it. Each keys everything on a distinct `coord2` marker and coexists with the legacy
`fulcra-coord` adapters until the phase-3 freeze retires them — installing one never touches a legacy
entry. All installers are idempotent (reinstall replaces, never duplicates) and ship an `--uninstall`
inverse.

**Tick doctrine (shared by every adapter).** All three adapters render the same canonical, briefing-led
tick: `continuity resume` → `briefing` (THE entry fold — identity, role inboxes, needs-me incl. pending
reviews) → for each review request, **slug-exact verdict-before-ack** (write the verdict file, verify
`review status` clears you from `pending_required`, only then ack — never ack bare or against a different
slug) → handle other work → `continuity snapshot` → `usage log` (ATC, when accounts are declared) →
`continuity park` before session end. **These hooks/prompts/blocks are rendered artifacts, not live
references:** after upgrading `fulcra-tools`, **RE-RUN YOUR ADAPTER INSTALLER** to regenerate them —
an un-regenerated hook keeps emitting the doctrine it was rendered under.

**Claude Code / Cowork** — settings.json hooks.
```bash
./scripts/claude-code/install-claude-code.sh <team> <agent>
./scripts/claude-code/install-claude-code.sh --uninstall <team> <agent>
```
Writes three scripts to `~/.claude/fulcra-coord2-hooks/` and merges their command paths into
`~/.claude/settings.json`: **SessionStart** → bounded `continuity resume` + inbox brief injected as
context; **PreCompact** and **SessionEnd** → backgrounded `continuity park`. It touches only its own
command paths, so legacy `fulcra-coord-hooks` keep firing until the freeze. Cowork uses the same core
and the same settings.json, so the same installer covers it.

**Codex** — `hooks.json` merge + app-thread automation.
```bash
python3 scripts/codex/install_codex_watch.py <team> <agent> [--codex-dir DIR] [--thread-id ID] [--uninstall] [--dry-run]
```
Merges SessionStart (matcher `startup|resume|clear|compact`) + PreCompact entries into
`~/.codex/hooks.json` — same entry shape as Claude Code — and seeds a coord2-first app-thread automation
under `~/.codex/automations/coord2-watch-<agent>/` whose prompt embeds contract rules 1–3 and ticks the
inbox. The consent-gated `wake.json` host-wake layer is **deliberately not shipped** (security ruling:
it spawns headless `codex exec` with approvals/sandbox bypassed; the coord2 listener already covers
wake). Deployment precondition: on the first real host, verify the SessionStart hook actually fires
before relying on hook-based automation seeding — pass `--thread-id` for the deterministic path if you
already know the watch thread.

**OpenClaw** — managed prose block.
```bash
python3 scripts/openclaw/install_openclaw.py <team> <agent> [--workspace DIR] [--uninstall] [--dry-run]
```
Merges a `fulcra-coord2`-fenced block (`<!-- fulcra-coord2:begin … -->` / `<!-- fulcra-coord2:end -->`)
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
uv tool run coord-engine continuity resume <team> <agent>
```
Then process the team inbox and, before concluding, snapshot again
(`coord-engine continuity snapshot …`) and let the next reconcile heal the views.

## 5. Recommended loop for a team
1. **Heartbeat**: `install-heartbeat.sh <team>` — reconcile every ~20m (consent first).
2. **On wake**: `continuity resume` → do work (`task …`, inbox) → `continuity snapshot` → `reconcile`.
3. **Gate merges** with `fulcra-agent-review` (`review status`), and keep roles fresh with
   `fulcra-agent-roles` (`roles status`), escalating vacancies.

That's the full coord2 stack running unattended on top of a `fulcra-agent-teams` space.

See the bundled [`scripts/install-heartbeat.sh`](scripts/install-heartbeat.sh).
