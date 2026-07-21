# Fulcra Continuity

Fulcra Continuity turns a long-running agent task into a structured checkpoint
that another session or agent can resume from without guessing.

The first use case is the **Context Cliff Rescue** demo: before compaction or a
handoff, capture the task objective, decisions, artifacts, open questions, next
actions, and memory writes. After compaction, render a resume brief that gives
the next session an inspectable operating state.

Fulcra Continuity pairs with the current coordination layer without depending
on it: **`coord-engine`** is the operational ledger for task lifecycle
(`coord-engine task …`), and `coord-engine continuity snapshot|checkpoint|resume`
is the engine-native way most agents carry session state today. This package
stores the durable "how to pick this work back up" snapshot for setups that
want it as a standalone library. When both are used, checkpoints can carry the
same workstream, agent, and task identity so another session can find the bus
task and import the latest same-agent continuity snapshot. For cross-agent
handoff, include the producer's checkpoint path or JSON as a portable artifact
so the receiver can load it directly before writing its own pickup checkpoint.

**Legacy note:** the original pairing target, `fulcra-coord`, was the retired
first-generation layer. Its implementation and handoff model remain available
in git history; don't build new work against it.

## Install in the workspace

```bash
uv run --package fulcra-continuity fulcra-continuity --help
```

## Create a checkpoint

```bash
uv run --package fulcra-continuity fulcra-continuity checkpoint \
  --task-id TASK-123 \
  --title "Migrate daily check-ins" \
  --objective "Move spreadsheet parsing onto coord without noise" \
  --workstream-id openclaw:discord:main-comms \
  --agent-id arc \
  --coord-task-id TASK-123 \
  --coord-owner-agent openclaw:discord:main-comms \
  --decision "Use lifecycle updates instead of channel broadcasts" \
  --artifact packages/coord-engine/README.md \
  --next "Audit current parser inputs" \
  --out /tmp/checkpoint.json
```

## Coord pairing model

Use the same identity values in both systems:

- `workstream_id`: the channel, team, or durable workstream that owns the work
- `agent_id`: the logical agent persona or runtime doing the work
- `coord_task_id`: the coord task this checkpoint resumes
- `coord_owner_agent`: the coord owner that should see or resume the task

Do not write a continuity checkpoint for every coord event. Coord should stay
cheap and chatty enough for operational state. Continuity should write at durable
pause points: before compaction, before handoff, when a session goes idle, when a
listener has seen several task events without user action, or when the user says
they are done for a while.

## Agent handoff contract

Agents that write or consume continuity checkpoints should follow
[`docs/agent-handoff.md`](docs/agent-handoff.md). The contract covers
Claude Code, Codex, OpenClaw/Arc, and Hermes, and explicitly supports
cross-agent transfer and non-GitHub work. GitHub issues and PRs are artifacts,
not required identity.

Checkpoints must be portable. Do not rely on bare local paths when handing work
to another agent or machine; include a URL, Fulcra remote path, coord task ID, or
repo/ref/path triple. Also assume the receiving agent may not know what
Continuity is: every checkpoint carries `bootstrap_primer` and `session_context`
fields so the resume packet explains how to render/read it and what broader
program/session context the next agent should keep in mind.

## Resume from a checkpoint

```bash
uv run --package fulcra-continuity fulcra-continuity resume /tmp/checkpoint.json
```

## Generate a demo fixture

```bash
uv run --package fulcra-continuity fulcra-continuity demo --out-dir /tmp/context-cliff-demo
```

This writes a sample checkpoint JSON and a human-readable resume brief.
