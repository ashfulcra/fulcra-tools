# Fulcra Coordination Protocol for Claude Code

This repo uses **fulcra-coord** to coordinate durable work across agent sessions using Fulcra Files as a shared bus. Read this before starting any non-trivial task.

## Setup check

```bash
fulcra-coord doctor
```

If it fails: see `docs/auth.md` in the fulcra-coord package.

## Before starting meaningful work

```bash
# Check what's active in this workstream
fulcra-coord status --workstream <workstream-name>

# Or check your own agent's tasks
fulcra-coord status --agent claude-code
```

## Starting a task

```bash
fulcra-coord start "Short durable objective" \
  --workstream devops \
  --agent claude-code \
  --kind ops \
  --priority P2 \
  --summary "One-sentence current state." \
  --next "What happens next."
```

## Updating a task

```bash
fulcra-coord update TASK-... \
  --summary "Progress note." \
  --next "What to do next."
```

## Status transitions

```bash
# Pause (session ending, work unfinished)
fulcra-coord pause TASK-... \
  --next "Specific next step for whoever picks this up." \
  --agent claude-code

# Block
fulcra-coord block TASK-... \
  --blocked-on "Waiting for X before I can proceed." \
  --agent claude-code

# Done — requires evidence
fulcra-coord done TASK-... \
  --evidence "PR #123 merged, tests passing, deployed to prod." \
  --verification-level agent-verified \
  --agent claude-code

# Abandon
fulcra-coord abandon TASK-... \
  --reason "Superseded by TASK-..." \
  --agent claude-code
```

## Rules

1. **Do not** write coordination updates for one-message answers or internal tool steps.
2. **Do** write updates at task boundaries: start, pause, block, done, abandon.
3. **Always** set `next_action` when pausing or blocking — it's the handoff note.
4. **Always** provide `evidence` when marking done.
5. **Print** the done line prominently to the user: `>>> Marked TASK-... done: <evidence>`
6. **Hooks cover the boundaries** — SessionStart surfaces in-flight work,
   PreCompact checkpoints before context loss, SessionEnd parks your task.
   Your job is to keep `next_action` and `--summary` *meaningful* via `update`
   at real milestones, so those automatic checkpoints capture useful state.

## Search

```bash
fulcra-coord search "deployment"
```

## Reconcile (if views are stale)

```bash
fulcra-coord reconcile
```

## Environment

| Variable | Default | Notes |
|---|---|---|
| `FULCRA_COORD_REMOTE_ROOT` | `/coordination` | Override to isolate environments |
| `FULCRA_CLI_COMMAND` | `fulcra-api` | Override if using a wrapper |

## Install

```bash
pip install fulcra-coord
# or (standalone tool install — use this outside a Python project)
uv tool install fulcra-coord
```
