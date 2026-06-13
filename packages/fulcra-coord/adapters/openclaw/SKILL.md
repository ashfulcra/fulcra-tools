# fulcra-coord — OpenClaw Skill Adapter

> **Canonical coord guide:** [`fulcra-coord/SKILL.md`](../../SKILL.md) — the runtime-agnostic when/how-to-use reference (quick-reference + load-bearing rules). This file is the OpenClaw-specific layer on top of it.

This file documents the OpenClaw skill integration for fulcra-coord.

## Skill trigger conditions

Invoke fulcra-coord coordination when:

- Starting a durable task that may span sessions or agents
- Resuming work from a previous session
- Changing task status (block, pause, done, abandon)
- Handing off to another agent or workstream
- Checking what's in progress before starting work

## Quick reference

```bash
# Status
fulcra-coord status [--workstream <ws>] [--agent <agent>]

# Start
fulcra-coord start "Title" --workstream <ws> --agent openclaw:<host>:<surface> --priority P2

# Transitions
fulcra-coord update TASK-... --summary "..." --next "..."
fulcra-coord pause  TASK-... --next "Next step."
fulcra-coord block  TASK-... --blocked-on "Reason."
fulcra-coord done   TASK-... --evidence "What was verified." --verification-level agent-verified
fulcra-coord abandon TASK-... --reason "Why."

# Repair
fulcra-coord reconcile

# Search
fulcra-coord search "keyword"
```

## Continuity handoff

Fulcra Continuity is the cold-start handoff layer for `fulcra-coord` work. Use
it when a task may survive compaction, transfer from OpenClaw to Claude Code,
Codex, Hermes, CI, another OpenClaw deployment, or continue without this chat
transcript. A useful checkpoint must be self-describing for an agent that may not
know Continuity yet, and its artifacts must be portable: URL, Fulcra remote path,
coord task ID, or repo/ref/path tuple. Do not rely on local-only OpenClaw paths.
Arc is one possible OpenClaw deployment/persona, not the OpenClaw model.

Read `packages/fulcra-coord/docs/continuity-handoff.md` and
`packages/fulcra-continuity/docs/agent-handoff.md` when implementing or
debugging handoff behavior.

## Adapter configuration

Set these in the OpenClaw environment or skill config:

```bash
FULCRA_COORD_REMOTE_ROOT=/coordination          # shared coordination root
FULCRA_CLI_COMMAND=fulcra-api                   # Fulcra CLI command
FULCRA_COORD_TIMEOUT_SECONDS=5                  # read timeout
```

## Workstream names (suggested)

Match OpenClaw workstream names to `fulcra-coord` workstream keys:

| OpenClaw Channel | Workstream key |
|---|---|
| #devops | `devops` |
| #main-comms | `main-comms` |
| #fulcra-skills | `fulcra` |
| #insights | `insights` |
| (general) | `general` |

Workstream keys are open strings — not validated.

## Agent ID convention

Use descriptive agent IDs that include the context:

```
openclaw:host:main-comms     # OpenClaw session on a host/surface
openclaw:discord:devops      # OpenClaw in a Discord devops surface
claude-code:repo:my-repo     # Claude Code in a specific repo
codex:paperclip              # Codex Paperclip agent
ci:github-actions            # CI pipeline
```

## Protocol rule (for CLAUDE.md / skill instructions)

Copy this rule into agent system prompts or CLAUDE.md files:

> Use fulcra-coord at task boundaries: before starting meaningful work
> (`fulcra-coord status`), when creating tasks (`start`), when pausing
> (`pause`), when blocking (`block`), when completing (`done` with evidence),
> and when ending a session with unfinished work. Do not write coordination
> updates for every internal step.

## Installing

```bash
pip install fulcra-coord
# or
uv add fulcra-coord

fulcra-coord doctor
```
