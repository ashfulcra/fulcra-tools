# Fulcra Continuity

Fulcra Continuity turns a long-running agent task into a structured checkpoint
that another session or agent can resume from without guessing.

The first use case is the **Context Cliff Rescue** demo: before compaction or a
handoff, capture the task objective, decisions, artifacts, open questions, next
actions, and memory writes. After compaction, render a resume brief that gives
the next session an inspectable operating state.

Fulcra Continuity is designed to pair with `fulcra-coord` without depending on
it. `fulcra-coord` remains the operational ledger for task lifecycle updates;
Fulcra Continuity stores the durable "how to pick this work back up" snapshot.
When both are used, checkpoints can carry the same workstream, agent, and coord
task identity so another session can find a coord task and import the latest
continuity snapshot.

## Install in the workspace

```bash
uv run --package fulcra-continuity fulcra-continuity --help
```

## Create a checkpoint

```bash
uv run --package fulcra-continuity fulcra-continuity checkpoint \
  --task-id TASK-123 \
  --title "Migrate daily check-ins" \
  --objective "Move spreadsheet parsing onto fulcra-coord without noise" \
  --workstream-id openclaw:discord:main-comms \
  --agent-id arc \
  --coord-task-id TASK-123 \
  --coord-owner-agent openclaw:discord:main-comms \
  --decision "Use lifecycle updates instead of channel broadcasts" \
  --artifact packages/fulcra-coord/README.md \
  --next "Audit current parser inputs" \
  --out /tmp/checkpoint.json
```

## Coord pairing model

Use the same identity values in both systems:

- `workstream_id`: the channel, team, or durable workstream that owns the work
- `agent_id`: the logical agent persona or runtime doing the work
- `coord_task_id`: the `fulcra-coord` task this checkpoint resumes
- `coord_owner_agent`: the coord owner that should see or resume the task

Do not write a continuity checkpoint for every coord event. Coord should stay
cheap and chatty enough for operational state. Continuity should write at durable
pause points: before compaction, before handoff, when a session goes idle, when a
listener has seen several task events without user action, or when the user says
they are done for a while.

## Resume from a checkpoint

```bash
uv run --package fulcra-continuity fulcra-continuity resume /tmp/checkpoint.json
```

## Generate a demo fixture

```bash
uv run --package fulcra-continuity fulcra-continuity demo --out-dir /tmp/context-cliff-demo
```

This writes a sample checkpoint JSON and a human-readable resume brief.
