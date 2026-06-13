# Platform adapters

`fulcra-prefs` has one capture model across every agent:

1. Load the compiled preference block at session start when the platform can do
   so.
2. When the user states a durable preference, queue a candidate with
   `fulcra-prefs notice`.
3. Drain candidates at a lifecycle boundary with `fulcra-prefs drain-candidates`
   or, for shell-less agents, POST signals directly with the raw HTTP recipe.
4. Run `fulcra-prefs compile` after new captures so the next agent sees the
   updated projection.

Candidate queues live at:

```text
~/.local/state/fulcra-prefs/candidates/<platform>/<session_id>.json
```

Each file is a JSON array accepted by `capture-batch`. `notice` appends one item
to that file. `drain-candidates` ingests the file and renames it to `.captured`.

## Claude Code

Claude Code supports deterministic shell hooks in `~/.claude/settings.json` and
project `.claude/settings.json`. Install:

```bash
fulcra-prefs install-hooks --platform claude-code
```

The installed `SessionStart` hook runs `compile` and injects the
`claude-code` preference block. `PreCompact` and `Stop` drain the current
session candidate file. Agents should queue preferences during work:

```bash
fulcra-prefs notice \
  --platform claude-code \
  --session "$CLAUDE_SESSION_ID" \
  --key docs.style.human_agent_quality \
  --value '{"preference":"Write direct, concrete documentation for humans and agents."}' \
  --strength 1.0 \
  --confidence 1.0 \
  --half-life 365
```

If the session id is not exposed, mint one once per conversation and reuse it.

## Codex

Codex supports hooks in `~/.codex/hooks.json`, project `.codex/hooks.json`, and
plugin-bundled hook files. Install:

```bash
fulcra-prefs install-hooks --platform codex
```

The `SessionStart` hook injects compiled prefs. `PreCompact` drains candidates.
Codex `Stop` is turn-scoped in current Codex, so the installer deliberately does
not drain there. During work, queue durable preferences with `notice`:

```bash
fulcra-prefs notice \
  --platform codex \
  --session "$CODEX_SESSION_ID" \
  --key comms.tone.concise \
  --value '{"preferred":true}' \
  --strength 0.8 \
  --confidence 1.0
```

## Claude

Claude outside Claude Code has no local lifecycle hook contract equivalent to
Claude Code. Use the deepest capability available:

- Claude Desktop or a Claude client with shell/MCP server access: expose a tool
  that calls `fulcra-prefs inject`, `notice`, and `drain-candidates`.
- HTTP-capable Claude agent without shell: use
  `fulcra-prefs-tier2-http.md` to read `prefs/compiled.json` and POST signals
  to `/ingest/v1/record`.
- MCP-read-only Claude: it can apply preferences only if the compiled doc is
  surfaced by another tool; it cannot capture until a write-capable bridge is
  available.

Set `platform` to `claude` for general Claude, and `claude-code` only for Claude
Code.

## ChatGPT

ChatGPT has no deterministic start, stop, or compaction hook. Use an app/action
bridge:

- For ChatGPT Apps or developer mode, expose a remote MCP server with read tools
  for compiled docs and write tools for capture. Current OpenAI docs describe
  ChatGPT Apps as MCP-backed, and developer mode supports write-capable MCP
  tools.
- For Custom GPT Actions, expose REST operations that call the same underlying
  capture/read functions. The existing coord adapter pattern is a good model:
  a small facade turns model calls into durable Fulcra writes.
- Without a deployed tool, ChatGPT can still follow the raw HTTP recipe in
  `fulcra-prefs-tier2-http.md`, but capture is best-effort because the model
  chooses when to call it.

Use `platform=chatgpt`. The agent must mint and reuse a `session` value because
ChatGPT does not provide one.

## OpenClaw

OpenClaw has file-based automation hooks and a plugin lifecycle. The preferred
scaffold is:

- At `agent:bootstrap` or plugin `session_start`, run
  `fulcra-prefs compile` and inject `fulcra-prefs inject --platform openclaw`
  into the session bootstrap context.
- During the session, queue preferences with `notice --platform openclaw`.
- At `session:compact:before`, `gateway:shutdown`, or plugin `session_end`, run
  `drain-candidates --platform openclaw --session <sessionKey>`.

Use OpenClaw's stable `sessionKey` as the `session` argument.

## Hermes Agent

Hermes agents should use the same CLI queue when the sandbox has shell access:

```bash
fulcra-prefs inject --platform hermes
fulcra-prefs notice --platform hermes --session "$HERMES_SESSION_ID" ...
fulcra-prefs drain-candidates --platform hermes --session "$HERMES_SESSION_ID"
```

If the Hermes lane cannot run local commands, use the raw HTTP recipe. The
handoff identity convention in this repo uses `hermes:<host-or-surface>:<label>`;
that value should be passed as `agent` when the agent knows it.
