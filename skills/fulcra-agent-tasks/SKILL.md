---
name: fulcra-agent-tasks
description: "Give a fulcra-agent-teams space a typed task lifecycle: create tasks with structured status/priority/assignee, and move them through a validated state machine (proposed→active→done) instead of freeform markdown."
homepage: "https://github.com/ashfulcra/coord2"
license: "MIT"
user-invocable: true
metadata: { "openclaw": { "emoji": "✅" } }
---

# Fulcra Agent Tasks

Enhances the [`fulcra-agent-teams`](https://github.com/fulcradynamics/agent-skills) skill. Bare teams
tracks long-running work as freeform `task/<name>.md`. This skill gives those docs a **typed lifecycle** —
a real `status`/`priority`/`assignee` and a **validated state machine** — so a task's state is queryable
(via `fulcra-agent-reconcile`) and can't take an illegal jump (e.g. `done → active`). Pairs with
`fulcra-agent-reconcile`: this skill *writes* task state; reconcile *reads/heals* the views.

## Why the writes go through the engine
Writing OKF frontmatter correctly and enforcing which transitions are legal are **deterministic**
requirements — a malformed doc or an illegal `waiting → done` is a correctness bug, not a style choice.
So the lifecycle commands are the shared **`coord-engine`** tool (parse→modify→write, transition-checked),
not prose the agent hand-edits. *Composing the human note in the task body is fine as prose; the
structured state is not.*

## The state machine
```
proposed → active | waiting | abandoned | done
active   → waiting | blocked | done | abandoned
waiting  → active  | blocked | abandoned
blocked  → active  | waiting | abandoned
done, abandoned → (terminal)
```
`done` requires evidence. A same-status update is always allowed (idempotent edit).

## Usage
Needs `fulcra-api` authenticated and `coord-engine` installed (see `fulcra-agent-reconcile`).
```bash
# create a task doc at team/<team>/task/<slug>.md
uv tool run coord-engine task start <team> "Fix the widget" \
    --workstream web --priority P1 --status proposed --assignee ash --summary "one-liner"

# move it through the machine (illegal transitions are rejected with a clear error)
uv tool run coord-engine task update <team> fix-the-widget --status active --next "write the test"
uv tool run coord-engine task update <team> fix-the-widget --status blocked --blocked-on "waiting on review"

# finish it — evidence is required
uv tool run coord-engine task done <team> fix-the-widget --evidence "PR #42 merged"
```
Each write stamps `timestamp` and appends a dated note to the task body; the Fulcra File Store versions
every write, so the full history is preserved. After changing tasks, run
`uv tool run coord-engine reconcile <team>` to refresh the index and views (or let a scheduled reconcile
do it).

See [`references/tasks-cli.md`](references/tasks-cli.md) for the full flag list and the OKF Task shape.
