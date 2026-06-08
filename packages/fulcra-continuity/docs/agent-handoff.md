# Agent Handoff Contract

Fulcra Continuity stores the "how to resume this work" snapshot that survives
agent restarts, context compaction, sandbox teardown, and cross-agent transfer.
It is intentionally separate from `fulcra-coord`: coord is the task/event ledger;
continuity is the cold-start handoff packet. The broader project is building
`fulcra-coord` as a shared coordination bus; Continuity is the resume layer that
lets work on that bus move between agents without shared transcript or local
filesystem access.

Continuity must work even when there is no GitHub issue, no pull request, and no
repo-backed task. GitHub links are artifacts only, not identity.

For the coord-side view of this relationship, read
`packages/fulcra-coord/docs/continuity-handoff.md`.

Assume the receiving agent may not know what Fulcra Continuity is. The
checkpoint and any handoff message must be self-describing: it should say that it
is a Fulcra Continuity checkpoint, explain that `objective`, `decisions`,
`open_questions`, `next_actions`, and `artifacts` are the resume payload, and
include a way to render it with `fulcra-continuity resume <checkpoint>` when the
CLI is available.

## Common Contract

Every agent that participates in continuity follows the same loop:

1. Establish identity.
   - `workstream_id`: durable workstream, channel, project, sandbox, or task lane.
   - `agent_id`: logical agent identity, not just a transient process ID.
   - `coord_task_id`: optional `fulcra-coord` task ID.
   - `coord_owner_agent`: optional owner on the coord bus.
2. On start or resume, look for a relevant checkpoint.
   - If a coord task is known, load the latest checkpoint for that task identity.
     In current `fulcra-coord`, the automatic latest lookup is same-agent; for a
     cross-agent pickup, use the producer-provided checkpoint path or JSON first.
   - If no coord task is known, load by workstream/session identity when available.
   - Render the resume brief and inspect listed artifacts before acting.
3. While working, keep the task or checkpoint context rich enough for a cold
   handoff.
4. Before a durable pause point, write a checkpoint.
5. If handing to another agent, make the target explicit in coord when coord is
   available, or include the target agent/workstream in the checkpoint metadata.

Durable pause points include:

- pre-compaction or context-window pressure
- explicit handoff to another agent
- session end, archive, idle stop, or sandbox teardown
- gateway shutdown or agent restart
- overnight pause or "done for now"
- several task events without user input where the agent may go stale

Do not checkpoint every message or every task update.

## Minimum Useful Checkpoint

A checkpoint is not useful unless another agent can continue without reading the
original transcript. Include:

- Objective: current goal and success condition.
- Current state: what is already done and what is in progress.
- Decisions: choices made, especially constraints and rejected assumptions.
- Open questions: unknowns the next agent should not guess.
- Next actions: concrete ordered steps.
- Artifacts: portable references to task files, checkpoint paths, docs, branch
  names, commits, command-output files, URLs, or remote object paths.
- Identity: workstream ID, producing agent ID, and coord task identity when
  present.
- Memory writes: facts or requirements that need durable storage outside the
  checkpoint.

Thin checkpoints are acceptable only as plumbing tests. Production checkpoints
must carry decisions, artifacts, and open questions.

## Portable Artifacts

Cross-agent artifacts must be resolvable by the receiving agent. A bare local
path like `packages/fulcra-continuity/docs/agent-handoff.md` is not enough unless
the checkpoint also tells the agent which repository, branch, commit, or remote
workspace contains it.

Prefer artifact references like:

```text
https://github.com/OWNER/REPO/pull/123
repo: ashfulcra/fulcra-tools
ref: BRANCH
commit: COMMIT_SHA
path: packages/fulcra-continuity/docs/agent-handoff.md
```

or:

```text
fulcra-file: /coordination/tasks/TASK-...
continuity-latest: /coordination/continuity/<workstream>/<agent>/<task>/latest.json
coord-task-id: TASK-...
```

If an artifact is local-only, say so and explain how to reproduce or ignore it.
Do not assume that Claude, Codex, OpenClaw, and Hermes share a filesystem.
For cross-agent handoff, include the exact `continuity-latest` path or
checkpoint JSON produced by the sending agent; the receiver's automatic
same-agent lookup may not discover it.

## Bootstrap for Unaware Agents

When handing off to an agent that may not have continuity instructions installed,
include this primer in the handoff message or checkpoint:

```text
You are receiving a Fulcra Continuity checkpoint. Treat it as the durable resume
state for this task. Read `objective`, `decisions`, `open_questions`,
`next_actions`, and `artifacts` before acting. If the `fulcra-continuity` CLI is
available, run `fulcra-continuity resume <checkpoint.json>` to render the brief.
If not, read the JSON directly. Artifacts may be GitHub URLs, Fulcra remote
paths, coord task IDs, or repo/path/ref triples; do not assume local paths exist
on your machine.
```

For repository-backed artifacts, include a URL or repo/ref/path triple. For
non-repo work, include remote Fulcra paths, coord task IDs, chat/channel
identities, or explicit reproduction steps.

## Coord-Backed Work

When a `fulcra-coord` task exists:

1. Claim or update the task with the agent's identity.
2. Ensure the session-to-task pointer is written by making the task
   `active`, `waiting`, or `blocked` from the current session context.
3. Write continuity at durable pause points with the same identity values.
4. On resume, load both:
   - the coord task state
   - the latest same-agent continuity checkpoint for that task
   - for cross-agent pickup, the producer-provided checkpoint path or JSON

Recommended identity shape:

```text
workstream_id=<project/channel/workstream>
agent_id=<runtime>:<host-or-surface>:<workstream>
coord_task_id=TASK-...
coord_owner_agent=<coord owner agent>
```

Examples:

```text
openclaw:discord:main-comms
claude-code:Mac:fulcra-tools
codex:Mac.localdomain:main
hermes:vercel:<sandbox-label>
```

## Continuity-Only Work

When no coord task exists, do not invent a GitHub issue or PR. Use a stable
continuity identity:

```text
task_id=<local/chat/sandbox task id>
workstream_id=<channel/project/sandbox/workstream>
agent_id=<agent doing the work>
```

Use continuity-only mode for:

- chat tasks that have not entered coord
- Hermes guest sandboxes
- local debugging work
- one-off research
- handoffs where the receiving agent cannot access coord yet

If the work later joins coord, write a new checkpoint that carries the coord task
identity while preserving the earlier continuity task ID as an artifact or
decision.

## Cross-Agent Transfer

For a handoff from agent A to agent B:

1. Agent A writes a rich checkpoint.
2. Agent A lists portable artifacts that agent B can actually access.
3. If coord is available, Agent A assigns or tells the coord task to Agent B.
4. Agent B loads Agent A's provided checkpoint before reading broad history.
5. Agent B writes a new checkpoint after pickup stating what it accepted,
   changed, or could not access.

The checkpoint must be runtime-neutral. Avoid instructions that only one agent
can execute unless they are clearly labeled as optional artifacts.

Example transfer:

```text
OpenClaw/Arc writes checkpoint:
  agent_id=openclaw:discord:main-comms
  coord_task_id=TASK-...
  artifacts=[
    "repo=ashfulcra/fulcra-tools ref=feature/... path=packages/fulcra-continuity/docs/agent-handoff.md",
    "fulcra-file=/coordination/tasks/TASK-...",
    "continuity-latest=/coordination/continuity/<workstream>/<agent>/<task>/latest.json"
  ]
  next_actions=["Claude Code: render/read this checkpoint, then inspect the portable artifacts"]

Claude Code resumes:
  loads checkpoint
  inspects artifacts
  claims or updates coord task
  writes a pickup checkpoint with agent_id=claude-code:<host>:<workstream>
```

## Claude Code

Claude Code should use `fulcra-coord install-claude-code --global` for the
coord-backed lifecycle hooks when available:

- `SessionStart`: connect presence and surface work.
- `PreCompact`: checkpoint the active task before context loss.
- `SessionEnd`: park active work as waiting and write a checkpoint.

Claude-specific instructions:

- On new work, create or claim a coord task when the work is operationally
  durable. Then checkpoint with the same task identity.
- On cross-agent pickup, load the checkpoint first, then inspect the listed
  artifacts. Do not assume the previous agent was Claude.
- If the task is not GitHub-backed, keep the task ID and artifact paths generic.
- Before requesting review or handing off, write a checkpoint with decisions,
  open questions, and the exact files or docs to inspect.
- When writing artifacts for another machine, include repo/ref/path or remote
  Fulcra paths, not only local paths from Claude's workspace.

## Codex

Codex should use `fulcra-coord ensure-codex-watch` for coord-backed hooks and
the durable listener self-heal. At minimum, `fulcra-coord install-codex` wires:

- `SessionStart`: connect presence and keep review capability fresh.
- `PreCompact`: checkpoint the active task before context loss.

Codex has no stop hook by design. End-of-session parking is handled by heartbeat
and reconcile, so Codex should write explicit checkpoints before:

- long-running code edits
- handoff to another agent
- ending after user says pause/done
- any expected context compaction

Codex-specific instructions:

- Prefer adding or updating a coord task before implementation work that should
  survive the current chat.
- Include file paths, branch names, test commands, and verification results as
  artifacts or decisions.
- If receiving a checkpoint from OpenClaw, Claude, or Hermes, treat it as the
  primary resume source and only then read repo history or transcripts.
- If Codex writes a checkpoint for another agent, include clickable/remote
  repository URLs or repo/ref/path triples. Do not assume the receiving agent can
  open Codex's local workspace paths.

## OpenClaw / Arc

OpenClaw should use `fulcra-coord install-openclaw` for Track A file hooks:

- `session:compact:before`: checkpoint before summarization.
- `gateway:shutdown`: park active work and checkpoint.
- `agent:bootstrap`: surface in-flight work during boot.

To make a fresh OpenClaw agent actually hear directed bus work while idle, bundle
the durable pickup path at install time:

```bash
fulcra-coord install-openclaw \
  --with-heartbeat \
  --with-listener \
  --agent openclaw:<surface>:<workstream>
```

The heartbeat is machine-global; the listener is per-agent, so the `--agent`
value must be the identity whose inbox should be polled. This composes the
standard `install-heartbeat` and `install-listener` installers rather than
open-coding scheduler state.

For deterministic per-session start/end, materialize and install Track B:

```bash
fulcra-coord install-openclaw --with-plugin
cd ~/.openclaw/plugins/fulcra-coord
npm install
npm run build
openclaw plugins install .
```

OpenClaw-specific instructions:

- Use channel/workstream identities such as `openclaw:discord:main-comms` or
  `openclaw:discord:personal-palantir`.
- When a Discord/chat task becomes durable, create or claim coord work so the
  session-to-task pointer exists.
- For short chat-only work, continuity-only checkpoints are allowed; do not force
  GitHub or coord if the work does not need it.
- Before handing to Claude/Codex/Hermes, write a checkpoint that names the target
  and includes accessible artifacts.
- If the handoff starts in Discord or another chat, include the channel/workstream
  identity and any coord task ID. Do not rely on a local OpenClaw path unless it
  is paired with a remote reference.

## Hermes

Hermes agents are often ephemeral sandboxes, so continuity is mandatory for
anything that should outlive the sandbox.

Hermes-specific instructions:

- On boot, read any checkpoint path, checkpoint JSON, or workstream identity
  passed through the sandbox environment or launch payload.
- If a coord task exists, preserve `coord_task_id` and `coord_owner_agent`.
- If no coord task exists, use continuity-only identity based on sandbox label,
  guest/workstream, or launch task.
- Before teardown, idle stop, handoff, or demo completion, write a checkpoint.
- Never assume the task is GitHub-backed. A Hermes handoff may be a guest
  onboarding session, a chat task, or a local sandbox operation.
- Do not include secrets in checkpoints. List secret-dependent setup as an open
  question or artifact note, not as values.
- Hermes sandboxes should treat local paths from other agents as hints only.
  Resolve work from URLs, Fulcra remote paths, coord task IDs, or explicit
  bootstrap payloads.

Suggested identity:

```text
agent_id=hermes:vercel:<sandbox-label>
workstream_id=hermes:<guest-or-demo-workstream>
task_id=HERMES-<launch-or-session-id>
```

## Checkpoint Quality Gate

Before writing a checkpoint, ask whether a different agent could continue after
only reading the checkpoint and listed artifacts. If not, add the missing
context.

Reject or enrich checkpoints that only say:

```text
Resume from latest next_action.
```

Prefer:

```text
Objective: write cross-agent continuity instructions.
Decisions: must support OpenClaw to Claude; must not assume GitHub.
Artifacts: repo/ref/path for continuity README, coord adapters, Hermes handoff docs.
Open questions: Hermes identity; taskless CLI shape.
Next actions: inspect repo, add docs, run tests, write pickup checkpoint.
```

## CLI Notes

The standalone CLI already supports rich checkpoint fields:

```bash
uv run --package fulcra-continuity fulcra-continuity checkpoint \
  --task-id TASK-123 \
  --title "Cross-agent handoff" \
  --objective "Move work from OpenClaw to Claude without transcript dependence" \
  --workstream-id coordination \
  --agent-id openclaw:discord:main-comms \
  --coord-task-id TASK-123 \
  --coord-owner-agent openclaw:discord:main-comms \
  --decision "Must work without GitHub" \
  --open-question "What is Hermes' canonical session ID?" \
  --artifact "https://github.com/OWNER/REPO/pull/123=PR with portable review context" \
  --artifact "repo=ashfulcra/fulcra-tools ref=BRANCH path=packages/fulcra-continuity/docs/agent-handoff.md" \
  --next "Claude Code picks up and edits adapter docs" \
  --out /tmp/checkpoint.json \
  --resume-brief /tmp/resume.md
```

If an automatic hook cannot pass rich fields safely through shell arguments,
write a structured context file and have the hook call the checkpoint API or a
future `--from-file` option.
