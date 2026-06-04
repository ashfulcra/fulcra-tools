# fulcra-coord ClawHub Positioning — 2026-06-04

## Live ClawHub Scan

Top downloaded ClawHub skills are dominated by:

- self-improvement / long-term memory (`self-improving-agent`, `self-improving`, `proactive-agent`, `elite-longterm-memory`)
- security / vetting (`skill-vetter`, `skillscan`)
- single-service/API skills (`github`, `gog`, `weather`, `notion`, `slack`, etc.)
- browser/search/document/media tools
- generic workflow / automation playbooks

Closest coordination search matches:

- `agent-orchestrator`: decomposes complex tasks and spawns/coordinates subagents inside an orchestration workflow.
- `multi-agent`: coordinator-mode parallel worker spawning.
- `agent-coordination`: coding-agent coordination / vibekanban-style workflow guidance.
- `tick-coord`: multi-agent task coordination via Git-backed Markdown.
- `telegram-agent-coordination`: loop hygiene for multiple bots in a Telegram group chat.
- `workflow`: generic automated pipelines with state management.

## Gap

`fulcra-coord` should not be positioned as another "spawn subagents" or "workflow decomposition" skill. The gap is durable cross-runtime coordination:

- shared coordination bus backed by Fulcra Files
- stable agent identity and presence
- directed work via `tell`, broadcast directives, and per-agent inboxes
- durable task lifecycle across restarts, context resets, crashed agents, CI jobs, local shells, OpenClaw, Codex, Claude Code, and future ChatGPT facade
- materialized views for cheap status/resume/needs-attention reads
- installable lifecycle hooks/listeners/reconciler for OpenClaw, Codex, Claude Code, and generic/cloud agents
- no SSH/Tailscale/shared workspace/central broker required, only Fulcra CLI + credentials

## Suggested ClawHub Angle

Title: `Fulcra Coordination Bus`

Short description:

> Durable multi-agent coordination over Fulcra Files: identity, presence, task lifecycle, directed inboxes, broadcasts, resume views, and lifecycle hooks for OpenClaw, Codex, Claude Code, CI, and cloud agents.

Trigger language:

- "coordinate multiple agents across sessions"
- "handoff work between agents"
- "agent inbox / broadcast / presence"
- "resume work after compaction or restart"
- "durable task bus"
- "coordination without shared workspace"

Not the pitch:

- not "spawn agents"
- not a generic todo list
- not another memory skill
- not an API connector to a single SaaS

## Best-Possible Public Framing

`fulcra-coord` lets independent agents leave each other durable work without sharing a filesystem, process, chat room, or memory store. A Claude Code session, Codex session, OpenClaw heartbeat, CI job, and future ChatGPT facade can all point at the same Fulcra Files root and see the same task lifecycle: who is present, what is active, what is blocked, what needs attention, and what is waiting in each agent's inbox.

The attention hook is the free Fulcra backend: users can create a free Fulcra account when they authenticate the CLI, with 5 GB of Fulcra Files storage. That makes the coordination bus portable for hobbyists and small teams without asking them to stand up Redis, Postgres, Tailscale, SSH, a shared repo, or a hosted orchestration server.

Suggested one-liner:

> A free-Fulcra-backed coordination bus for AI agents: persistent identity, presence, inboxes, broadcasts, task handoffs, resume views, and lifecycle hooks across local, cloud, and CI agents.

## Package / Security Notes

Start with a docs-first ClawHub package that points to the canonical `ashfulcra/fulcra-tools/packages/fulcra-coord` package and documents the CLI install/auth boundary.

Important caveats:

- Experimental: this may not be supported long term in its current form.
- Publishing should happen under the `ashfulcra` GitHub/publisher identity.
- The skill needs an explanatory onboarding flow, not just a command list.
- Avoid bundling local workspace scripts or private OpenClaw-specific state.
- If executable helpers are included later, keep them minimal and make Fulcra CLI credential handling explicit.

## Blockers Before Public Skill Push

The genericization and best-description pass is blocked on:

- Fulcra Files support being wrapped into the main Fulcra CLI branch.
- Fulcra annotation support being wrapped into the main Fulcra CLI branch.
- Potentially Fulcra's upcoming "what's new" function, if that becomes the best onboarding/discoverability surface.

## Bus Follow-Up

Created coordination bus task:

`TASK-20260604-resume-fulcra-skill-dist-dbd4340f`

Next action:

Draft the actual ClawHub package strategy: target gap, title/description, trigger conditions, artifact contents, security notes, onboarding flow, and publish path.
