# Fulcra Continuity

Fulcra Continuity turns a long-running agent task into a structured checkpoint
that another session or agent can resume from without guessing. The standalone
CLI writes local JSON and Markdown files; it needs Python 3.11+, no runtime
dependencies, and no Fulcra account or Collect installation.

The first use case is the **Context Cliff Rescue** demo: before compaction or a
handoff, capture the task objective, decisions, artifacts, open questions, next
actions, and memory writes. After compaction, render a resume brief that gives
the next session an inspectable operating state.

Fulcra Continuity pairs with the current coordination layer without depending
on it: **[coord-engine](../coord-engine/README.md)** is the operational ledger for task lifecycle
(`coord-engine task …`), and `coord-engine continuity snapshot|checkpoint|park|resume`
is its engine-native session-state interface. This package provides a separate
checkpoint schema and renderer for standalone use. Remote persistence and
automatic lifecycle hooks belong to the surrounding integration, not this CLI.

When using both systems, carry the coord task and agent identities as metadata.
Resume a standalone checkpoint by passing its path to `fulcra-continuity resume`;
the CLI does not discover a latest checkpoint or import engine snapshots.
For cross-agent handoff, supply the checkpoint JSON or an accessible path so the
receiver can read it before writing its own pickup checkpoint.

**Legacy note:** the original pairing target, `fulcra-coord`, was the retired
first-generation layer. Its implementation and handoff model remain available
in git history; don't build new work against it.

## Install

From the repository root:

```bash
uv tool install ./packages/fulcra-continuity
fulcra-continuity --help
```

Or run directly in the workspace:

```bash
uv run --package fulcra-continuity fulcra-continuity --help
```

## Create a checkpoint

```bash
uv run --package fulcra-continuity fulcra-continuity checkpoint \
  --task-id TASK-123 \
  --title "Migrate the example parser" \
  --objective "Replace the example parser while preserving its output" \
  --workstream-id example-project:parser \
  --agent-id agent-a \
  --coord-task-id TASK-123 \
  --coord-owner-agent agent-a \
  --decision "Keep the existing output format" \
  --artifact "repo=OWNER/REPO ref=BRANCH path=docs/parser.md" \
  --open-question "Which legacy inputs still need coverage?" \
  --session-context "The replacement is designed; implementation has not started" \
  --next "Audit current parser inputs" \
  --out /tmp/checkpoint.json
```

`--coord-task-id` and `--coord-owner-agent` are optional: omit them for a
standalone task. Repeat `--decision`, `--artifact`, `--open-question`, `--next`,
and `--memory` to carry more context. `--resume-brief PATH` writes a Markdown
brief alongside the JSON. Memory writes are recorded intentions; this command
does not update a separate memory store.

## Coord pairing model

Use the same identity values in both systems:

- `workstream_id`: the channel, team, or durable workstream that owns the work
- `agent_id`: the logical agent persona or runtime doing the work
- `coord_task_id`: the coord task this checkpoint resumes
- `coord_owner_agent`: the coord owner that should see or resume the task

Do not write a continuity checkpoint for every coord event. Coord should stay
cheap and chatty enough for operational state. Continuity should write at durable
pause points: before compaction, before handoff, when a session goes idle, when
several task events have accumulated without user action across wakes, or when
the user says they are done for a while.

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

## Test

From the repository root:

```bash
uv run --package fulcra-continuity --extra dev --no-editable pytest packages/fulcra-continuity/tests -q
```
