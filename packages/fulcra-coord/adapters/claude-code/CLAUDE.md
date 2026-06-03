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

# Block (on an agent / external thing)
fulcra-coord block TASK-... \
  --blocked-on "Waiting for X before I can proceed." \
  --agent claude-code

# Block ON THE OPERATOR — when you need the human to do something
fulcra-coord block TASK-... \
  --on-user "Approve the deploy / paste the API key / decide between A and B."
# ^ assigns the task to the human, tags needs:human, lands it on `needs-me`,
#   and leads their next SessionStart. This is how "blocked on the human"
#   becomes visible instead of buried in a summary.

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

## Identity

Declare a clear, stable, human-legible identity so directives reach you and the
operator can tell who's who on the bus — set it once per repo (identity is now
scoped per working directory):

```bash
fulcra-coord identity set vendor:host:purpose   # e.g. claude-code:DeskbookPro:fulcra-coord
```

Always identify yourself in what you direct at others.

**Work in your own git worktree, not a shared checkout.** Concurrent sessions
sharing one working tree clobber each other's index/`HEAD` — interleaved commits
and orphaned merge conflicts. Give each session its own worktree (it also gets
its own per-cwd identity): `git worktree add ../<repo>-<purpose> -b
<vendor>/<purpose> origin/main`. Conflict markers or staged files you didn't
create mean you're sharing a checkout — move out before committing.

## Rules

1. **Declare your identity** (`identity set vendor:host:purpose`) and always
   identify yourself — see the Identity section above.
2. **Do not** write coordination updates for one-message answers or internal tool steps.
3. **Do** write updates at task boundaries: start, pause, block, done, abandon.
4. **Mark anything you need the operator to do** with `block --on-user "<ask>"` —
   it lands on the human's `needs-me` plate and leads their next SessionStart.
5. **Always** set `next_action` when pausing or blocking — it's the handoff note.
6. **Always** provide `evidence` when marking done.
7. **Print** the done line prominently to the user: `>>> Marked TASK-... done: <evidence>`
8. **Hooks cover the boundaries** — SessionStart surfaces in-flight work,
   PreCompact checkpoints before context loss, SessionEnd parks your task.
   Your job is to keep `next_action` and `--summary` *meaningful* via `update`
   at real milestones, so those automatic checkpoints capture useful state.

## Code review & merge (global — every repo)

**Never push directly to `main`. Every change goes through a PR, gets reviewed
by another agent, and is merged by its original author — not the reviewer.**

1. Do the work on a branch in your own worktree; open a PR (CI must pass).
2. Post a bus message to your **reviewer**: `tell <reviewer> "Review PR #<n> in
   <repo> — assume there are bugs that need fixing."` (Adversarial framing — the
   reviewer hunts for bugs, doesn't rubber-stamp.)
   - **Reviewer routing:** non-Arc Claude Code agents → the **Codex reviewer**
     (currently `codex:Mac.localdomain:main`). Arc sessions (`claude-code:ArcBot:*`)
     → the **Arc code-review** session (`claude-code:ArcBot:Arc-Code-Review`).
     If you *are* the Codex reviewer, ask the operator to assign a Claude agent
     to review your own PRs.
3. The reviewer reviews, **commits fixes onto the PR branch**, then messages you
   back to review those fixes.
4. **You (the author) review the reviewer's fixes and do the final merge.** The
   reviewer never merges.
5. If the reviewer doesn't respond in a reasonable window, **ping the operator
   (`block --on-user`) — never merge unreviewed.**

## Repo homes (where work lives)

Everything lives on **GitHub** so PRs are possible — if a repo isn't on GitHub,
get it there before merging (ask the operator where it should go if unsure).

- **`ashfulcra/fulcra-tools`** (this monorepo, currently Fulcra-internal) is
  **only for things that make Fulcra useful for other people** — Fulcra-ecosystem
  tools that may become public/product later (ingest, onboarding, agent
  coordination, …).
- **Everything else** — personal projects, unrelated infra — goes in **separate,
  logically-arranged repos under `ashfulcra` or `reversity`**, NOT in this
  monorepo.
- When unsure where a new repo or package belongs, **ask the operator first.**

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
