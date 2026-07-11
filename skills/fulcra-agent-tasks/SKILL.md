---
name: fulcra-agent-tasks
description: "Give a fulcra-agent-teams space a typed task lifecycle: create tasks with structured status/priority/assignee, and move them through a validated state machine (proposed→active→done) instead of freeform markdown."
homepage: "https://github.com/ashfulcra/fulcra-tools"
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

## Where to start — the re-entrancy probes

Before creating or transitioning a task, probe engine health and what work is already waiting. Enter at
the **first probe that fails** (per the repo's skill-quality pattern, `docs/skill-quality-pattern.md`);
task writes are parse→modify→write and a same-status update is idempotent, so re-entry never corrupts
state:

| Probe (run in order) | Command | Passes when | If it fails, enter at |
|---|---|---|---|
| Engine + auth usable? | `coord-engine doctor <team>` | exits 0 and the last line is exactly `doctor: healthy` | fix engine/auth first (see Usage / fulcra-agent-reconcile) — do NOT write tasks against a broken engine |
| Aggregate fresh? | `coord-engine status <team>` | output does NOT contain `(no aggregate for team/` (the CLI's missing-aggregate hint) | run `coord-engine reconcile <team>` to build the aggregate, then re-probe |
| Work for me? | `coord-engine needs-me <team> --agent <id>` | prints `0 item(s) need <id>:` (a non-zero count means work is waiting — enter there) — read the rows (including any `[REVIEW] pending verdict` review-pending rows) before starting new work | pick up the listed items (transition them via `task update`/`task done`) before creating more |

All probes pass with nothing needing you → the engine is healthy, views are fresh, and your queue is
clear; proceed to create or advance tasks below.

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
Needs `fulcra-api` authenticated and `coord-engine` installed — standalone:
`uv tool install "git+https://github.com/ashfulcra/fulcra-tools.git@<latest-coord-engine-tag>#subdirectory=packages/coord-engine"` (any
coord skill brings the same engine; installing once serves all).
```bash
# create a task doc at team/<team>/task/<slug>.md
coord-engine task start <team> "Fix the widget" \
    --workstream web --priority P1 --status proposed --assignee ash --summary "one-liner"

# move it through the machine (illegal transitions are rejected with a clear error)
coord-engine task update <team> fix-the-widget --status active --next "write the test"
coord-engine task update <team> fix-the-widget --status blocked --blocked-on "waiting on review"

# finish it — evidence is required
coord-engine task done <team> fix-the-widget --evidence "PR #42 merged"
```
Each write stamps `timestamp` and appends a dated note to the task body; the Fulcra File Store versions
every write, so the full history is preserved. After changing tasks, run
`coord-engine reconcile <team>` to refresh the index and views (or let a scheduled reconcile
do it).

See [`references/tasks-cli.md`](references/tasks-cli.md) for the full flag list and the OKF Task shape.
