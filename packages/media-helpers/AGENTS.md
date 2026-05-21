# AGENTS.md — autoloaded by Aider / Cursor / Continue.dev / Claude Code / OpenHands

This repo's agent-facing skill lives at:

**`skills/fulcra-media/SKILL.md`**

Read that file first. It covers:
- The stable `--json` envelope schema every importer emits
- Per-importer auth / wizard / cadence guidance
- A decision tree for picking the right importer when a user names a service
- A heartbeat / cron template for periodic runs across **Hermes, openclaw, OpenHands**, and host schedulers

If your runtime is **Hermes Agent** or **openclaw**, this skill's full text can also be placed at the runtime's skills path (`~/.openclaw/workspace/skills/fulcra-media/SKILL.md`, or the `agentskills.io` registry) — the contents are identical.

If your runtime is **Letta/MemGPT**, note: "heartbeat" in Letta means intra-turn loop continuation (a tool flag), not a scheduler. Use an external cron/launchd to fire periodic `fulcra-media import …` runs.
