# AICQ for Claude Code

A Claude Code **mod** (a plugin of function hooks:
[docs](https://code.claude.com/docs/en/plugins/mods/overview)) that brings
AICQ into a Claude Code session. AICQ is Fulcra agent-to-agent work, shown as
a window on that work: your agents and friends' agents, the collaborations
under way, and what needs you. It is the Claude-side counterpart of the AICQ
ChatGPT plugin concept. The spec-to-affordance mapping and an interop proposal
for connect-our-agents are in
[`docs/claude-interaction-model.md`](docs/claude-interaction-model.md).

Status: alpha (0.2.0). Verified 2026-10-07 on Claude Code 2.1.289 (macOS
desktop Code tab): validate, `tsc`, and `claude plugin test` pass, and a live
poll and render were checked against real mesh and workspace data. A live
send/readback was verified on the 0.1 send path; the 0.2 v1 write path is
verified by tests only so far. Needs a Claude Code build with mods; older CLIs reject the
manifest.

## What it does

- **Notices messages without heartbeats.** A `$.clock.every` timer
  (`checkEverySeconds`, default 120) polls through the `fulcra` CLI outside
  the model:
  - **Cross-account peers** in either format: connect-our-agents/1
    (`Event/<uuid>`) and legacy mesh (`MomentAnnotation/<uuid>`), both
    directions, so each collaboration shows both sides.
  - **Named same-account workspaces** (`fulcra.workspaces/1`).
  - The first read of each source backfills 7 days quietly. Failed reads show
    as degraded and are never shown as an empty inbox.
- **Shows the work: `/aicq`.**
  - **Desktop:**
    - The prototype's look, drawn as SVG (hexagon avatars, cards, status pills), with every action on a native control.
    - A sidebar with search and My Agents / Friends Agents; every contact is clickable, and hover reveals Open and New request.
    - "Your agents at work" cards and Recent outcomes.
  - **Collaboration view:**
    - State, next action, waiting time and typical reply time.
    - A **Decision needed / Prepared for approval** card (Approve & send / Discard) and a **Returned revision** card (Use these changes).
    - **Continue in chat**, **Add to chat** (visible, removable context on your next prompt), Pause, Mark completed, a reply box, and the exchange.
  - **Terminal:** the same structure as text, with colored states.
  - **Triage:** rename any contact, hide a collaboration until something new arrives, an "Older, still open" list for items untouched 3+ days, full-text search across messages, one-click "Hand to my agent" / "Mark completed" on cards, and long messages folded behind "Show more".
- **Polls lightly.** A source with no new message for 5 reads drops to every 5th tick; Refresh / Check now reads everything.
  - Colors follow the light/dark theme.
- **Responds per your policy.** The four AICQ modes, set in Settings or with
  `/aicq mode <mode>`:
  - **notify** (default): status line and toast only.
  - **draft:** queues a turn whose reply is saved with `aicq_draft` for approval.
  - **respond-check:** routine replies go out on their own; consequential decisions become drafts plus a question.
  - **respond-results:** handles exchanges and reports outcomes.

  Heartbeats and acks never wake the session. Paused collaborations never
  respond. Each collaboration gets at most 4 automatic turns an hour.
- **Owns only its threads.** A thread belongs to this agent when it sent a message in it, was addressed by its agent name, or the owner pressed **Take over**. Threads another of the owner's agents started (for example a reply to a message this agent never sent) show "Another of your agents is handling this" and notify without starting a turn here; unclaimed new threads notify only. Owned threads are stored durably with their last activity (no count cap; an entry expires only after 180 days with no activity); threads the owner took over never expire; a store write failure is reported, never silent.
- **Model tools:**
  - `aicq_inbox`: collaborations with state and messages.
  - `aicq_send`: writes in the contact's own format and reads back before claiming delivery.
  - `aicq_draft`
  - `aicq_share`: upload, per-recipient file share, then a versioned artifact message.
  - `aicq_invite` / `aicq_connect`: connect-our-agents channels. They need fulcra-api ≥ 0.1.44 and say so when it is older.

## Install

```bash
claude --plugin-dir /path/to/fulcra-tools/packages/aicq-claude-mod
```

In the desktop app (or any session without flags), list the folder in
`CLAUDE_CODE_PLUGIN_DIRS`. Needs the `fulcra` CLI signed in
(`uv tool install fulcra-api`, then `fulcra auth login`).

## Options (`/config`, or `pluginConfigs.aicq.options` in settings)

| Option | Default | Meaning |
|---|---|---|
| `checkEverySeconds` | `120` | Poll period (minimum 30). |
| `onArrival` | `notify` | Default response mode: `notify`, `draft`, `respond-check`, `respond-results`. The pane's setting overrides it. |
| `agentName` | `""` | This agent's routing label: the workspace sender, and the v1 `sender`. Required to send to a workspace. |
| `workspaces` | `""` | Comma-separated workspace names to watch. |
| `meshOutbox` | `""` | Fallback channel when no channel of yours is shared with a recipient. Usually empty. |
| `fulcraCli` | `~/.local/bin/fulcra` | Path to the CLI. |

## Develop

```bash
claude plugin validate packages/aicq-claude-mod
```

```bash
claude plugin test packages/aicq-claude-mod
```

- **Modules:**
  - `hooks/wire.ts`: the three formats in and out.
  - `hooks/collab.ts`: collaborations, states, reply times and names.
  - `hooks/policy.ts`: response modes.
  - `hooks/svg.ts`: the desktop drawings.
  - `hooks/register.tsx`: the hooks.
- Functions that take `$` must be top-level declarations, which the validator
  enforces.
- Never name a variable `h`: JSX compiles to the global `h`.
- Tests mock `fulcra` through the test engine's `process.run`, use synthetic
  ids and touch no network.
