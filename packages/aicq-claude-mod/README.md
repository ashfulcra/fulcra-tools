# AICQ for Claude Code (bare-bones mod)

A Claude Code **mod** (a plugin of function hooks:
[docs](https://code.claude.com/docs/en/plugins/mods/overview)) that brings
Fulcra agent-to-agent messaging into a Claude Code session. It is the
Claude-side counterpart of the AICQ ChatGPT plugin spec. It lets a session
**notice new agent messages without heartbeats in the conversation**, the
problem that otherwise needs a listener session, a local script or an hourly
routine.

Status: alpha. Verified 2026-10-07 on Claude Code 2.1.289 (macOS desktop Code
tab). It needs a Claude Code build with mods (function hooks); older CLIs
reject the manifest.

## What it does

- **Polls on a timer, outside the model.** It polls every `checkEverySeconds`
  (default 120) using `$.clock.every` and `$.process.run` against the `fulcra`
  CLI. Polling spends no tokens and adds no turns.
  - **Mesh** (friends' agents, cross-account): every incoming share that
    names a `MomentAnnotation/<uuid>` outbox is a contact. Records are
    `{v,mid,to,to_user,kind,pri,slug,body}` JSON in `note`.
  - **Workspaces** (your own agents, same account): it reads
    `workspace/<name>/index.md` for `Message channel:`. Records are
    `{coord:{protocol:"fulcra.workspaces/1",message_id,sender,recipients,…}}`.
  - **Cursors** are a newest-timestamp watermark plus seen ids per source, kept
    in the mod's store. Each read overlaps the last one by 10 minutes. The
    first read of a source backfills 24 hours **quietly**: no toast, no wake.
- **Shows** messages without starting a turn:
  - the status line (`AICQ: N new · 12 contacts · last check 11:20`);
  - a toast when something arrives;
  - `/aicq`, a pane with **My Agents** (workspaces) and **Friends Agents**
    (mesh), each contact's latest topic and age, the latest messages, and
    **Check now**.
- **Wakes** the session when `onArrival = wake`. A real message is submitted
  as a turn (`$.prompt.submit`, queued until the session is idle). Heartbeats,
  `ack`s and `-ack`/`-retracted` topics never wake, so two agents can't loop.
  The wake prompt labels message bodies as another agent's request, not the
  user's instruction.
- **Gives the model two tools:**
  - `aicq_inbox` returns recent messages and the poll status.
  - `aicq_send {source: mesh|workspace, to, to_user?, workspace?, topic, body,
    in_reply_to?}` records the envelope, then **reads it back**. It reports
    `UNVERIFIED` rather than claiming delivery when the id isn't visible. A
    mesh send uses **the outbox you share with that peer**: it resolves the
    newest outgoing share whose permissions name `to_user`, and falls back to
    the `meshOutbox` option.
- **Fails loud.** Any failed read (user-info, shares, a peer, a workspace
  index) shows as `check failed (<source>)` in the status line and in red in
  the pane. It is never shown as an empty inbox.

Out of scope for bare bones:
- invites;
- the spec's four response modes (only `notify` and `wake`);
- collaboration work states;
- groups.

## Install

```bash
claude --plugin-dir /path/to/fulcra-tools/packages/aicq-claude-mod
```

In the desktop app (or any session without flags), list the folder in
`CLAUDE_CODE_PLUGIN_DIRS`.

Needs the `fulcra` CLI signed in (`uv tool install fulcra-api`, then
`fulcra auth login`). The default path is `~/.local/bin/fulcra`; set
`fulcraCli` if yours differs.

## Options (`/config`, or `pluginConfigs.aicq.options` in settings)

| Option | Default | Meaning |
|---|---|---|
| `checkEverySeconds` | `120` | Poll period (minimum 30). |
| `onArrival` | `notify` | `notify`: status + toast. `wake`: also start a turn. |
| `agentName` | `""` | This agent's routing label. Filters mesh `to` and workspace `recipients`, and is the workspace `sender`. Required to send to a workspace. |
| `workspaces` | `""` | Comma-separated workspace names to watch. |
| `meshOutbox` | `""` | Fallback `MomentAnnotation/<uuid>` when no per-peer outbox share exists. |
| `fulcraCli` | `~/.local/bin/fulcra` | Path to the CLI. |

## Develop

```bash
claude plugin validate packages/aicq-claude-mod
```

```bash
claude plugin test packages/aicq-claude-mod
```

- `hooks/wire.ts` is the pure wire logic (parsing, cursors, envelopes,
  outbox resolution). `hooks/register.tsx` holds the hooks.
- Functions that take `$` must be top-level declarations; the validator
  enforces this.
- Tests mock `fulcra` through the test engine's `process.run`. They never
  touch the network.
