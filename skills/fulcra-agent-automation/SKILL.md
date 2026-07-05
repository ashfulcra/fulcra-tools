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
| Listener installed + loaded? | `ls ~/Library/LaunchAgents/com.fulcra.coord-engine.listener.<team>.*.plist` and `launchctl list \| grep coord-engine.listener.<team>.` | file matches AND a launchctl line appears (the suffix is the sanitized agent id + checksum — always probe with a wildcard, never the raw agent id: colons are sanitized to dashes) | §2 (install the listener) |
| Views actually fresh? | `uv tool run coord-engine health <team>` | this host's row shows a recent `last reconcile` | jobs exist but are not ticking — check `log show` / cron mail, then reinstall |

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

## 3. Resume on wake — structured, not a prose re-read
When a cron/heartbeat wakes an agent to do team work, the wake payload should **resume continuity first**
(this is the structured version of `fulcra-agent-teams`' "read progress.md before acting" rule):
```bash
uv tool run coord-engine continuity resume <team> <agent>
```
Then process the team inbox and, before concluding, snapshot again
(`coord-engine continuity snapshot …`) and let the next reconcile heal the views.

## 4. Recommended loop for a team
1. **Heartbeat**: `install-heartbeat.sh <team>` — reconcile every ~20m (consent first).
2. **On wake**: `continuity resume` → do work (`task …`, inbox) → `continuity snapshot` → `reconcile`.
3. **Gate merges** with `fulcra-agent-review` (`review status`), and keep roles fresh with
   `fulcra-agent-roles` (`roles status`), escalating vacancies.

That's the full coord2 stack running unattended on top of a `fulcra-agent-teams` space.

See the bundled [`scripts/install-heartbeat.sh`](scripts/install-heartbeat.sh).
