# Fulcra Continuity

Fulcra Continuity turns a long-running agent task into a structured checkpoint
that another session or agent can resume from without guessing.

The first use case is the **Context Cliff Rescue** demo: before compaction or a
handoff, capture the task objective, decisions, artifacts, open questions, next
actions, and memory writes. After compaction, render a resume brief that gives
the next session an inspectable operating state.

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
  --decision "Use lifecycle updates instead of channel broadcasts" \
  --artifact packages/fulcra-coord/README.md \
  --next "Audit current parser inputs" \
  --out /tmp/checkpoint.json
```

## Resume from a checkpoint

```bash
uv run --package fulcra-continuity fulcra-continuity resume /tmp/checkpoint.json
```

## Generate a demo fixture

```bash
uv run --package fulcra-continuity fulcra-continuity demo --out-dir /tmp/context-cliff-demo
```

This writes a sample checkpoint JSON and a human-readable resume brief.
