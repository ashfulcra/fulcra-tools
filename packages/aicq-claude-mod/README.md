# AICQ for Claude Code

A Claude Code **mod** (a plugin of function hooks:
[docs](https://code.claude.com/docs/en/plugins/mods/overview)) that brings
AICQ into a Claude Code session. AICQ is Fulcra agent-to-agent work, shown as
a window on that work: your agents and friends' agents, the collaborations
under way, and what needs you. It is the Claude-side counterpart of the AICQ
ChatGPT plugin concept. The spec-to-affordance mapping and an interop proposal
for connect-our-agents are in
[`docs/claude-interaction-model.md`](docs/claude-interaction-model.md); the
file convention agents use to publish their own session links is
[`docs/agent-links.md`](docs/agent-links.md).

Status: alpha (0.6.0; the checks in this paragraph were run on 0.2.0). Verified 2026-10-07 on Claude Code 2.1.289 (macOS
desktop Code tab): validate, `tsc`, and `claude plugin test` pass, and a live
poll and render were checked against real mesh and workspace data. A live
send/readback was verified on the 0.1 send path; the 0.2 v1 write path is
verified by tests only so far. Needs a Claude Code build with mods; older CLIs reject the
manifest.

## What it does

- **Waiting on you** (the first thing the `/aicq` home shows, on terminal and desktop; `/aicq waiting` opens it): one ranked list of everything waiting on the owner or the owner's agents. It holds friends' agents in AICQ threads that need a reply, decision or approval, and the owner's own agents blocked on `user:<owner>` on the coord bus, because someone else's agent waiting on you is the same thing as your own agent waiting on you. Ranked by priority (P0..P3; decision and approval threads count as P1), then by longest wait. Each row shows who is waiting (yours or a friend's), what it needs, how long, machine and platform, and **Open thread** or **Help me unblock**, plus the session jump when known. Up to 8 rows, then a count of the rest. The universe is also read shortly after session start, so coord items appear without opening the map.
- **Notices messages without heartbeats.** A `$.clock.every` timer
  (`checkEverySeconds`, default 120) polls through the `fulcra` CLI outside
  the model:
  - **Cross-account peers** in either format: connect-our-agents/1
    (`Event/<uuid>`) and legacy mesh (`MomentAnnotation/<uuid>`), both
    directions, so each collaboration shows both sides.
  - **Named same-account workspaces** (`fulcra.workspaces/1`).
  - Every session fills its own inbox: its first read of each source backfills
    7 days (or the ignore-after limit, when that is longer), so a new session, a
    restart, or a second session running at the same time shows the same
    collaborations. Failed reads show as degraded and are never shown as an
    empty inbox.
  - Notifying and waking happen once per message across all sessions on the
    machine. They share one record of what has been handled, so a message that
    arrived while no session ran still notifies in the next one, and one already
    handled elsewhere does not notify again.
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
  - **Ignore after** (Settings; default 5 days, or "Never ignore"): a collaboration with no activity for longer than the limit leaves the pane (cards, "Older, still open", contact lines) and `aicq_inbox`; the pane shows "N with no activity for over D days ignored · Show them" ("Ignore them again" hides them), and search still finds them. A message stamped older than the limit never notifies or wakes, even when delivered late. A collaboration with your own pending draft is never ignored.
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
- **Universe map** (`/aicq map`, or **Universe map** in the pane): an interactive, status-colored map of the owner's agent universe — machine/env → platform/runtime → identity/session — with the meshes between them (workspaces, V5 workspaces, the cross-account mesh) and red blocked-on-you links. Built from `coord-engine agents|board|health` (team `coordTeam`), the V5 actor configs on this machine (one root per actor at `~/.local/share/coord-v5-<name>/state/config.json`, plus `~/.local/share/coord-v5/<actor>/config.json` and the alpha3 root), workspace member lists and mesh peers; it refreshes every 10 minutes and on Refresh, and any unreadable source is named, never shown as empty. Placement is inferred from identity names (`platform:machine:name`), V5 instance ids and workspace notes; correct any node's machine or platform from its detail (stored). Clicking a node shows liveness, annotation, open work and each blocked item with its next action, and offers: message it on the coord bus (delivery confirmed by the bus), help me unblock this, open its AICQ thread or contact, ask my agent about it.

  An agent node's detail (not a cross-account peer's) can also open the session it runs in, through an app link and a web link. The session is found only explicitly, never guessed from a name or folder, in this order: the owner's pin, chosen from the node's **Session** picker (this machine's Claude desktop sessions and Codex threads, "From its V5 binding", or "No session link"; stored across restarts); else an exact match of its V5 binding's `session_id` against this machine's app records (the session's Claude CLI id or desktop id); else the agent's own report in `team/<team>/agent-links/<identity>.json` (format agent-links/1, see [`docs/agent-links.md`](docs/agent-links.md)), read on each universe refresh, with unreadable files named as degraded. App records come from Claude desktop's records in `~/Library/Application Support/Claude/claude-code-sessions/` and in each `~/.claude-instances/<name>/claude-code-sessions/` (any `<kind>_<id>` desktop id, so sessions the local app shows that run elsewhere are included), which also give a session's Remote Control web page, `https://claude.ai/code/<last bridgeSessionIds entry>`; and from Codex's `~/.codex/session_index.jsonl`. A missing app contributes nothing, and an unreadable one is named as degraded, never shown as empty. The web link is https and is drawn as a real Link, clickable on every surface including the desktop pane and a phone. The app link is a button (**Open in Claude/Codex**) that runs macOS `open`; it works from the terminal too and is not offered on mobile. Only allowlisted link shapes are ever opened or drawn clickable: `claude://claude.ai/…`, `https://claude.ai/…`, `codex://threads/<uuid>` (optionally `?hostId=remote-control:<env>`) and `https://chatgpt.com/c/<id>`; reports are written by agents, so nothing else becomes a one-click link. Self-reports cover agents on other machines; an identity with no pin, no matching app record and no report gets no link.
- **Model tools:**
  - `aicq_inbox`: collaborations with state and messages. `include_stale: true` includes ignored ones; its status reports `ignore_after_days` and `ignored_as_stale`.
  - `aicq_send`: writes in the contact's own format and reads back before claiming delivery.
  - `aicq_draft`
  - `aicq_share`: upload, per-recipient file share, then a versioned artifact message.
  - `aicq_invite` / `aicq_connect`: connect-our-agents channels. They need fulcra-api ≥ 0.1.44 and say so when it is older.

## Install

From the fulcra-tools plugin marketplace, on any machine with a mods-capable Claude Code (2.1.289+), in Claude Code:

```
/plugin marketplace add ashfulcra/fulcra-tools
/plugin install aicq@fulcra-tools
/plugin configure aicq@fulcra-tools
```

or from a shell:

```bash
claude plugin marketplace add ashfulcra/fulcra-tools
```

```bash
claude plugin install aicq@fulcra-tools
```

Then set at least `agentName`, unique per machine or session (it decides which threads this agent owns), and `workspaces`. Start a new session, then run `/aicq` and `/aicq map`. `/plugin marketplace update fulcra-tools` picks up new versions.

Needs the `fulcra` CLI signed in to your Fulcra account (`uv tool install fulcra-api`, then `fulcra auth login`). The universe map also reads `coord-engine` (see [`docs/coord/GET-ON-THE-BUS.md`](../../docs/coord/GET-ON-THE-BUS.md)); without it the map names the coord sources it could not read.

For development, load the folder directly instead: `claude --plugin-dir /path/to/fulcra-tools/packages/aicq-claude-mod` (or list it in `CLAUDE_CODE_PLUGIN_DIRS`).

## Options (`/config`, or `pluginConfigs.aicq.options` in settings)

| Option | Default | Meaning |
|---|---|---|
| `checkEverySeconds` | `120` | Poll period (minimum 30). |
| `onArrival` | `notify` | Default response mode: `notify`, `draft`, `respond-check`, `respond-results`. The pane's setting overrides it. |
| `ignoreAfterDays` | `5` | Ignore collaborations with no activity for longer than this many days, and never notify or wake on messages older than it. `0` turns it off; clamped to 0..365. The pane's setting (Never ignore, 1, 2, 3, 5, 7, 14, 30 days) overrides it and is kept across sessions. |
| `agentName` | `""` | This agent's routing label: the workspace sender, and the v1 `sender`. Required to send to a workspace. |
| `workspaces` | `""` | Comma-separated workspace names to watch. |
| `meshOutbox` | `""` | Fallback channel when no channel of yours is shared with a recipient. Usually empty. |
| `fulcraCli` | `~/.local/bin/fulcra` | Path to the CLI. |
| `coordTeam` | `fulcra` | coord-engine team the universe map reads and messages. |
| `coordEngine` | `~/.local/bin/coord-engine` | Path to coord-engine. |
| `owner` | `ash` | Your principal on the bus (`user:<owner>` marks blocked-on-you). |

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
  - `hooks/universe.ts`: the universe map, session links and the link allowlist.
  - `hooks/waiting.ts`: the "Waiting on you" list and its ranking.
  - `hooks/register.tsx`: the hooks.
- Functions that take `$` must be top-level declarations, which the validator
  enforces.
- Never name a variable `h`: JSX compiles to the global `h`.
- Tests mock `fulcra` through the test engine's `process.run`, use synthetic
  ids and touch no network.
