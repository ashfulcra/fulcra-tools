# Fulcra Coordination Protocol for Claude Code

> **Canonical coord guide:** [`fulcra-coord/SKILL.md`](../../SKILL.md) — the runtime-agnostic when/how-to-use reference (quick-reference + load-bearing rules). This file is the Claude Code-specific layer.

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
9. **Backlog convention — "do later" items go ON THE BUS.** When the operator
   hands you a deferred task, run
   `fulcra-coord later "<title>" -s "<context>"` — never park it only in
   session memory, where compaction eats it. `later` addresses the item to the
   `@backlog` role (durable, visible in the `board` ideas pipeline, spams
   nobody); route it later with the ordinary `assign`. Subagent work, by
   contrast, stays off the bus.
10. **Continuity is the cold-start handoff layer.** For work that may transfer to
   another agent or survive compaction, make checkpoints self-describing and
   portable: include what Fulcra Continuity is, coord task identity, decisions,
   open questions, next actions, and repo/ref/path or Fulcra remote artifacts
   instead of local-only paths. See
   `packages/fulcra-coord/docs/continuity-handoff.md`.
11. **In-session listening polls the bus DIRECTLY.** A long-running interactive
   session arms a background watcher that polls raw `tasks/` listings — never
   the view files — for new work addressed to it. Views (`summaries`,
   `presence`) may lag for hours under backend pressure (2026-06-10: stale
   views hid 6 review verdicts + 2 direct messages from every polling agent),
   and the listener app's notification cannot wake a session — only the
   session's own direct poll can.

## Code review & merge (global — every repo)
**Nothing merges without an independent review by a *different agent identity*
than the author.** That independent review is the guarantee that matters; *who
clicks merge* is mechanical and is NOT a separate gate. (Earlier this rule forced
the author to do the final merge; that mandatory hand-back stalled cross-agent
reviews for days whenever author and reviewer weren't online together, so it was
dropped — the review, not the merge-clicker, is the control.)

The handshake is **artifact-based** and runs **on the bus**, so it works with any
forge or none. The artifact under review is an opaque ref — a PR#, MR#, branch,
commit SHA, URL, patch id, or even a non-code deliverable (a doc, a dataset, a bus
task):

1. Do the work on a branch in your own worktree. Where a forge/PR exists, open
   the PR (CI must pass when the repo has CI) — but the artifact can equally be a
   branch or commit SHA on a shared remote with no forge at all.
2. Route the review with `request-review <artifact> [--repo <repo>]` (`--repo` is
   optional — a branch/URL ref carries its own context). It routes a `kind:review`
   directive to a live/idle reviewer, self-healing if nobody's up; the reviewer
   hunts for bugs adversarially, doesn't rubber-stamp.
   - **Reviewer routing:** non-Arc Claude Code agents → the **Codex reviewer**
     (currently `Ashs-MBP-Work:Codex-Review-Workbook`). Arc sessions
     (`claude-code:ArcBot:*`) → the **Arc code-review** session
     (`claude-code:ArcBot:Arc-Code-Review`). The Codex reviewer's own work → a
     live **Claude** agent.
3. The reviewer posts the outcome with
   **`review-done <artifact> --verdict approve|changes [--note "…"] [--to <author>]`**.
   This lands the verdict as a bus directive (tagged `kind:review-verdict`) in the
   author's inbox, so it **always** reaches them regardless of any forge. The bus
   is the source of truth — a review isn't "done" until `review-done` has run. A
   GitHub-only PR "Approve"/comment does NOT count and is the durable bug this
   command fixes: the listener / SessionStart only watch the bus, so a forge-only
   verdict silently never reaches the author.
4. Then:
   - **`--verdict approve` with NO code changes:** the reviewer (or whoever is
     around) merges it once green. Do NOT hand a clean approval back to the author
     and wait — that round-trip is the bottleneck.
   - **`--verdict changes`, or the reviewer pushed fixes onto the branch:** the
     author addresses the changes / signs off on those commits before merge —
     never ship changes to someone's code without them seeing it.
5. **Hard floor (never relax): never merge your own UNREVIEWED code.** If you are
   the Codex reviewer, get a Claude agent to review your work. No reviewer live in
   a reasonable window → ping the operator (`block --on-user`); never merge
   unreviewed.

Merging is forge-agnostic: a plain `git` push / fast-forward on a shared remote,
any forge, or `gh pr merge` all work — **`gh pr merge` is one option, not a
requirement**, and coord never calls a forge itself.

Note: local agents and Codex often push under the **same GitHub account**, so
GitHub's "Approve" can be a no-op — the review handshake lives on the **bus**, by
*agent identity*, not forge review state. "Reviewed by another agent" means a
different bus identity signed off (via `review-done`), regardless of which account
merged.

## Repo homes (where work lives)

Most work lives on **GitHub** so PRs + `gh` are available — the convenient
default, not a requirement. The review handshake itself is bus-based
(`request-review` → `review-done --verdict`) and works on any remote (or none),
so a non-GitHub repo is fine; put new repos where the operator wants them (ask if
unsure). On GitHub, use PRs; elsewhere, review on the bus and merge with plain
`git`.

- **`ashfulcra/fulcra-tools`** (this monorepo, currently Fulcra-internal) is
  **only for things that make Fulcra useful for other people** — Fulcra-ecosystem
  tools that may become public/product later (ingest, onboarding, agent
  coordination, …).
- **Fulcra-related** work that isn't "useful-to-others" enough for the monorepo
  (infra, proxies, runtimes — e.g. an LLM proxy, a Hermes runtime) → its own
  **`ashfulcra/<repo>`**.
- **Personal / unrelated** projects (e.g. a home-automation or hobby project) →
  its own **`reversity/<repo>`**.
- One logically-arranged repo per project; when unsure where something belongs,
  **ask the operator first.**

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
