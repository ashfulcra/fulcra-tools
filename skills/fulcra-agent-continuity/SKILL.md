---
name: fulcra-agent-continuity
description: "Give agents structured, resumable continuity in a fulcra-agent-teams space: snapshot objective / decisions / next-actions / open-questions to a schema, and get a deterministic resume brief when a fresh session or cron run wakes up."
homepage: "https://github.com/ashfulcra/fulcra-tools"
license: "MIT"
user-invocable: true
metadata: { "openclaw": { "emoji": "🧭" } }
---

# Fulcra Agent Continuity

Enhances the [`fulcra-agent-teams`](https://github.com/fulcradynamics/agent-skills) skill. Teams already
uses `member/<agent>/progress.md` to survive isolated cron/heartbeat runs, but it's freeform — a fresh
session has to re-read prose and guess what mattered. This skill adds a **structured** snapshot
(objective, decisions, next actions, open questions, artifacts, context-used-%) and a **deterministic
resume brief**, so waking up is reliable instead of a re-read.

Whether/when to snapshot is a judgment call (prose); building the schema and folding many snapshots to
the newest is deterministic (the `coord-engine` tool).

## The lifecycle contract (applies on every harness)

Any agent participating in a team space owes these four behaviors. Harness adapters
(see fulcra-agent-automation §Harness adapters) automate them where the platform
allows; where it doesn't, follow them as prose:

1. **On wake** (new session, cron fire, heartbeat, automation tick):
   `coord-engine continuity resume <team> <agent>` and read your inbox
   (`coord-engine inbox <team> --agent <agent>`) BEFORE taking new work.
2. **On material change** (a decision made, an artifact produced, a task claimed or
   finished) and at least hourly while actively working:
   `coord-engine continuity snapshot <team> <agent> <task> --objective … [--decision …] [--next …]`.
3. **Before context loss** (compaction, model handoff, session end):
   `coord-engine continuity park <team> --agent <agent> --objective "<one line>"` —
   parks every held role with a checkpoint.
4. **Inbox cadence:** while live, poll your inbox at least every 30 minutes. If your
   platform has a durable scheduler, install the listener instead of trusting yourself.

An agent that beats presence but has no fresh snapshot is flagged `continuity-stale` by `coord-engine health`.

## Which adapter automates this for you

| Harness | Lifecycle (rules 1–3) | Pickup (rule 4) |
|---|---|---|
| Claude Code (CLI/desktop) | hooks: SessionStart→resume+briefing, PreCompact/SessionEnd→park (`fulcra-agent-automation/scripts/claude-code/install-claude-code.sh`) | launchd/cron listener (`scripts/install-listener.sh`) |
| Claude Cowork (desktop) | same as Claude Code (same core; same settings.json) | same listener |
| Claude web (claude.ai) | prose only — run rule 1 when the skill loads; rule 3 before ending | no background pickup; use cloud routines that open a duty-cycle session |
| Codex | `~/.codex/hooks.json` + app-thread automation (`scripts/codex/install_codex_watch.py`) — the automation prompt embeds rules 1–3 | the same automation ticks the inbox |
| OpenClaw | managed block in `HEARTBEAT.md`/`BOOT.md` (`scripts/openclaw/install_openclaw.py`) embeds rules 1–2; rule 3 (park) can't be automated from a prose block (no shutdown hook) — follow it as prose | HEARTBEAT tick |
| Hermes (Daytona sandbox) | fhd provisioner installs the Claude Code adapter inside the sandbox at provision time (standalone hermes-daytona repo) | listener installed at provision time |

## Where to start — the re-entrancy probes

On waking (fresh session / cron), before doing anything else, probe whether a resume brief already
exists for you. Enter at the **first probe that fails** (per the repo's skill-quality pattern,
`docs/skill-quality-pattern.md`); a snapshot is a single-file overwrite and `resume` is a pure read, so
re-entry is always safe:

| Probe (run in order) | Command | Passes when | If it fails, enter at |
|---|---|---|---|
| Engine + auth usable? | `uv tool run coord-engine doctor <team>` | exits 0 and the last line is exactly `doctor: healthy` | fix engine/auth first (see fulcra-agent-reconcile) — do NOT snapshot/resume against a broken engine |
| Resumable snapshot for me? | `uv tool run coord-engine continuity resume <team> <agent>` | prints a line beginning `Resume:` (the newest snapshot across your tasks) — NOT the single line `No continuity snapshot found.` | **Snapshot first** — you have no resume state; take a snapshot now (see [Usage](#usage)) before spending context, so the next wake resumes clean |
| Latest snapshot fresh? | `coord-engine continuity resume <team> <agent>` | header timestamp < your work cadence | write one now (rule 2) |
| Am I continuity-stale? | `coord-engine health <team>` | your agent row has no `continuity-stale` flag | rules 2–3, then re-check |

Snapshot present → read the printed brief (objective / next actions / open questions / decisions) and
resume that work. `resume <team> <agent> <task>` narrows to one task; the bare `resume <team> <agent>`
form folds to the newest across all your tasks. Both are pure reads — safe to re-run any time.

## Snapshot schema (`member/<agent>/continuity/<task>/latest.json`)
```json
{ "schema": "coord.teams.continuity.v1",
  "checkpoint_id": "CHK-<iso>-<task>",
  "agent": "ash", "task": "build-l6",
  "objective": "ship the continuity layer",
  "decisions": ["chose structured json over freeform"],
  "next_actions": ["land the PR", "write the skill"],
  "open_questions": ["fold across tasks or per-task?"],
  "artifacts": ["https://github.com/.../pull/5"],
  "context_used_percent": 40, "transcript_path": null,
  "created_at": "2026-07-01T18:00:00Z" }
```

## Usage
```bash
# take a snapshot (e.g. before context runs out, at a natural stopping point, or on session end)
uv tool run coord-engine continuity snapshot <team> <agent> <task> \
    --objective "ship the continuity layer" \
    --next "land the PR" --next "write the skill" \
    --open-question "fold across tasks or per-task?" \
    --decision "chose structured json" --context-percent 40

# on waking (fresh session / cron), get a resume brief — deterministic, not a prose re-read
uv tool run coord-engine continuity resume <team> <agent> <task>
uv tool run coord-engine continuity resume <team> <agent>          # newest across all the agent's tasks
```

## Role checkpoints, park, and briefing (A6)
```bash
uv tool run coord-engine continuity checkpoint <team> --role <r> [--ref PATH]  # get/set the role's durable resume point
uv tool run coord-engine continuity park <team> [--agent X] [--objective "…"]  # session exit: snapshot EVERY held role + set its checkpoint_ref
uv tool run coord-engine briefing <team> [--agent X] [--json]                  # session start: presence + board + inbox + needs-me + pending reviews + latest snapshot in ONE call
```
- `park` is the session-exit verb: each role you hold (fresh lease) gets a snapshot and the role doc's
  `checkpoint_ref` points at it — the next holder (or your next session) resumes from there via
  `checkpoint --role`.
- `briefing` is the session-start verb and **tolerates absent add-ons** — with no presence/directives
  installed the sections are simply empty; it never fails a cold start.

## When to use
- **Before context runs low** or at a natural stopping point — capture what you'd need to resume.
- **On session end / hand-off** — the next session (or another agent picking up the work) resumes clean.
- **In a cron/heartbeat wake payload** — call `continuity resume` first to re-establish state, exactly as
  `fulcra-agent-teams` asks agents to read `progress.md` first, but structured.

Pairs with `fulcra-agent-teams`' MEMORY.md / heartbeat conventions: keep those, and add a structured
snapshot for the work in flight. See [`references/continuity-cli.md`](references/continuity-cli.md).
