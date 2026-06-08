# Continuity Handoff in fulcra-coord

`fulcra-coord` is the shared coordination bus. Fulcra Continuity is the
cold-start resume layer that makes bus work survive compaction, session loss,
agent transfer, and sandbox teardown.

The broader goal is not "write a checkpoint for a PR." The goal is that any
agent can pick up durable work from another agent without shared memory,
transcript access, a shared filesystem, or a GitHub-backed task.

## Responsibilities

`fulcra-coord` owns operational state:

- task lifecycle: `proposed`, `active`, `waiting`, `blocked`, `done`,
  `abandoned`
- owner and assignee identity
- inbox routing, broadcasts, review routing, listeners, and heartbeats
- durable task files under the Fulcra Files coordination root
- materialized views for cheap status/resume reads

Fulcra Continuity owns resume context:

- a bootstrap primer for agents that do not know Continuity yet
- broader session/program context, not only the immediate task
- objective and current state
- decisions already made
- open questions
- concrete next actions
- portable artifacts the next agent can access
- memory writes that should survive outside the checkpoint

Use both when possible: coord says who owns the work and what lifecycle state it
is in; Continuity says how to continue it cold.

## Resume Flow

When an agent starts or receives directed work:

1. Run `fulcra-coord resume --with-continuity --agent <agent-id>`.
2. For each active/waiting task, read the task state and latest same-agent
   checkpoint.
3. If a checkpoint exists, treat it as the primary resume source before reading
   broad history.
4. Inspect every portable artifact listed in the checkpoint.
5. Update the coord task once the agent has actually picked up the work.
6. Write a pickup checkpoint if the accepting agent changes the plan, cannot
   access artifacts, or discovers new constraints.

## Writing Checkpoints

Write checkpoints at durable boundaries:

- pre-compaction
- explicit handoff to another agent
- session end or archive
- gateway shutdown
- sandbox teardown
- idle/overnight pause
- "done for now"

Do not write a checkpoint for every coord event. Coord events can be chatty;
Continuity checkpoints are handoff packets.

## Portable Artifact Rule

Agents do not necessarily share local paths. A checkpoint artifact such as
`packages/fulcra-continuity/docs/agent-handoff.md` is only useful if paired with
a portable locator.

Prefer artifacts like:

```text
https://github.com/OWNER/REPO/pull/123
repo=ashfulcra/fulcra-tools ref=<branch> commit=<sha> path=packages/fulcra-continuity/docs/agent-handoff.md
fulcra-file=/coordination/tasks/TASK-...
coord-task-id=TASK-...
continuity-latest=/coordination/continuity/<workstream>/<agent>/<task>/latest.json
```

GitHub is optional. It is one portable artifact type, not a requirement.

Current coord-side lookup stores checkpoints under workstream, agent, and task.
That means `resume --with-continuity` finds checkpoints written with the same
agent identity it is resuming as. During cross-agent transfer, include the
explicit `continuity-latest` path or checkpoint JSON as a portable artifact so
the receiving agent can load the producer's checkpoint directly before writing
its own pickup checkpoint.

## Bootstrap for Agents That Do Not Know Continuity

The receiving agent may not have Continuity instructions installed. Include a
plain-language primer in the checkpoint or handoff message:

```text
You are receiving a Fulcra Continuity checkpoint. Treat it as durable resume
state. Read objective, decisions, open_questions, next_actions, and artifacts
before acting. If the fulcra-continuity CLI is available, run
`fulcra-continuity resume <checkpoint.json>`; otherwise read the JSON directly.
Artifacts may be GitHub URLs, Fulcra remote paths, coord task IDs, or
repo/ref/commit/path tuples. Do not assume local paths exist on your machine.
```

## Cross-Agent Transfer

For OpenClaw/Arc to Claude, Claude to Codex, Codex to Hermes, or any other
transfer:

1. Producing agent writes a checkpoint with portable artifacts.
2. Producing agent routes the coord task if coord is available:
   `fulcra-coord assign TASK-... <target-agent>`.
3. Target agent runs `resume --with-continuity` for any same-agent checkpoints
   and loads the producer's checkpoint directly from the `continuity-latest` path
   or checkpoint JSON provided in the handoff.
4. Target agent writes a pickup checkpoint after accepting the work.

If no coord task exists, use continuity-only identity. Do not invent a GitHub PR
or issue just to make handoff work.

## Relationship to Agent Adapter Docs

Adapter-specific docs should point here and to
`packages/fulcra-continuity/docs/agent-handoff.md`.

- Claude Code: lifecycle hooks checkpoint at `PreCompact` and park at
  `SessionEnd`.
- Codex: `ensure-codex-watch` keeps hooks/listener armed; explicit checkpoints
  are needed before handoff because there is no stop hook.
- OpenClaw/Arc: `install-openclaw --with-heartbeat --with-listener --agent <id>`
  gives both lifecycle hooks and idle bus pickup.
- Hermes: ephemeral sandboxes must checkpoint before teardown and use portable
  bootstrap payloads rather than local paths.
