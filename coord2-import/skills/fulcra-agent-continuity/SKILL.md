---
name: fulcra-agent-continuity
description: "Give agents structured, resumable continuity in a fulcra-agent-teams space: snapshot objective / decisions / next-actions / open-questions to a schema, and get a deterministic resume brief when a fresh session or cron run wakes up."
homepage: "https://github.com/ashfulcra/coord2"
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
uv tool run coord-engine briefing <team> [--agent X] [--json]                  # session start: presence + board + inbox + needs-me + latest snapshot in ONE call
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
