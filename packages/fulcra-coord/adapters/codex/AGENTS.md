# Fulcra Coordination Protocol for Codex Agents

This project uses **fulcra-coord** for durable task coordination across agent sessions. Fulcra Files acts as a shared coordination bus — no shared memory or direct agent-to-agent calls required.

## Setup

```bash
pip install fulcra-coord
fulcra-coord doctor
```

If `doctor` reports auth issues, see `docs/auth.md`.

## Reading coordination state

Before starting non-trivial work:

```bash
fulcra-coord status --workstream <workstream>
fulcra-coord status --agent <your-agent-id>
fulcra-coord inbox --agent <your-agent-id>
```

Check for tasks already `waiting` or `active` that may belong to your workstream.
For an already-open Codex Desktop thread, remember that `install-listener` emits
desktop notifications and prepares the next `SessionStart`; it cannot inject
new broadcast text into the live conversation. Use the Codex app heartbeat for
thread wakeups, or poll `inbox` manually at task boundaries.

## Creating a task

```bash
fulcra-coord start "Short objective" \
  --workstream devops \
  --agent codex:paperclip \
  --kind feature \
  --priority P2 \
  --surface "codex:session" \
  --summary "Brief current state." \
  --next "First concrete step."
```

## Updating and transitioning

```bash
# Update progress
fulcra-coord update TASK-... --summary "..." --next "..."

# Pause when session ends
fulcra-coord pause TASK-... --next "Next step for next session." --agent codex:paperclip

# Mark blocked (on an agent / external thing)
fulcra-coord block TASK-... --blocked-on "Reason." --agent codex:paperclip

# Mark blocked ON THE OPERATOR — when you need the human to do something
fulcra-coord block TASK-... --on-user "Approve the deploy / paste the key."
# ^ assigns the task to the human, tags needs:human, lands it on `needs-me`,
#   and leads their next SessionStart.

# Mark done (requires evidence)
fulcra-coord done TASK-... --evidence "What was verified." --verification-level agent-verified

# Abandon
fulcra-coord abandon TASK-... --reason "Why." --agent codex:paperclip
```

## Key rules

- **Declare a stable, human-legible identity** (`identity set vendor:host:purpose`)
  and always identify yourself, so directives reach you and the human can tell
  who's who. Identity is scoped per working directory.
- **Work in your own git worktree, not a shared checkout.** Concurrent sessions
  sharing one working tree clobber each other's index/`HEAD` (interleaved commits,
  orphaned conflicts). `git worktree add ../<repo>-<purpose> -b codex/<purpose>
  origin/main` gives this session an isolated checkout + its own per-cwd identity.
  Conflict markers or staged files you didn't create = you're sharing a checkout.
- Write at **boundaries**: start, pause, block, done, abandon.
- Never write for every internal step.
- **Mark anything you need the operator to do** with `block --on-user "<ask>"`.
- `next_action` is required when pausing or blocking — it's the handoff.
- `evidence` is required when marking done.
- If `fulcra-coord` is unavailable, cache files locally and run `reconcile` when connectivity recovers.

## Environment variables

```bash
export FULCRA_COORD_REMOTE_ROOT=/coordination   # coordination root
export FULCRA_CLI_COMMAND=fulcra-api            # CLI command
```
