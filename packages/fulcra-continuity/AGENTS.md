# AGENTS.md - Fulcra Continuity

Fulcra Continuity is the durable handoff layer for agent work. It must work
across agents and surfaces, including OpenClaw/Arc to Claude Code, Claude Code to
Codex, Codex to Hermes, and back again. Do not assume the work is GitHub-backed:
checkpoints may describe a coord task, a chat-originated task, a local task, a
Hermes sandbox handoff, or a standalone continuity-only workstream.

Before changing this package or creating continuity-related agent instructions,
read `docs/agent-handoff.md`.

Assume the next agent may know nothing about Fulcra Continuity. A handoff must
explain what the checkpoint is, how to render or read it, and which artifacts are
portable enough for the receiver to access.

## Core Rules

- A checkpoint is a cold-start handoff, not a log line. It must contain enough
  context for another agent to continue without the original transcript.
- Use Fulcra Continuity at durable pause points: pre-compaction, handoff,
  teardown, idle/archive, overnight stop, or explicit "pause/done for now".
  Do not write a checkpoint for every chat message or every coord update.
- When a `fulcra-coord` task exists, carry its identity in the checkpoint:
  `identity.coord_task_id`, `identity.coord_owner_agent`, `identity.workstream_id`,
  and `identity.agent_id`.
- When no `fulcra-coord` task exists, still write continuity using a stable
  task/workstream/session identity. Do not fabricate a GitHub issue or PR just to
  make continuity work.
- Cross-agent transfer is a first-class use case. The producing agent must not
  write instructions that only its own runtime can understand.
- Do not use bare local paths as the only artifact reference for cross-agent
  handoff. Pair them with a repository URL, branch/commit, remote Fulcra path,
  coord task ID, or explicit access note.

## Minimum Checkpoint Content

Every useful checkpoint should include:

- `objective`: what the work is trying to accomplish now.
- `decisions`: choices already made, including rejected assumptions.
- `open_questions`: unknowns the next agent must resolve.
- `next_actions`: concrete ordered steps for the next agent.
- `artifacts`: files, remote paths, task IDs, docs, branches, or URLs to inspect.
- `identity`: workstream, logical agent, and coord task identity when available.
- `memory_writes`: durable facts or requirements that should be saved elsewhere.
- `bootstrap_primer`: what Fulcra Continuity is and how to resume/read the packet.
- `session_context`: broader program/session context the next agent would lack
  without the original transcript.

For a receiver that has no continuity-specific bootstrap, keep the
`bootstrap_primer` direct and plain: "This is a Fulcra Continuity checkpoint.
Render it with `fulcra-continuity resume <checkpoint>` or read the JSON fields
directly."

If the automatic writer cannot populate these fields, update the task summary
first or write a richer checkpoint through the structured CLI/API path.

## Agent-Specific Instructions

`docs/agent-handoff.md` defines how Claude Code, Codex, OpenClaw/Arc, and Hermes
join continuity, write checkpoints, and resume from another agent's checkpoint.
Keep any adapter-specific docs consistent with that file.
