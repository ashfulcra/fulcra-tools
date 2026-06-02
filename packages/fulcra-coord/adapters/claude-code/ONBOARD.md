# Onboard an already-running Claude Code session

A new session is wired automatically once `fulcra-coord install-claude-code`
has run (its SessionStart hook fires at launch). A session that was ALREADY
running when hooks were installed must onboard manually — run these now:

1. Check setup: `fulcra-coord doctor`
   - If unauthed: `fulcra-api auth login` and complete the device flow.
2. Wire future sessions (idempotent): `fulcra-coord install-claude-code --global`
3. Load current in-flight work: `fulcra-coord status`
4. If you are continuing or claiming a task, run `fulcra-coord start ...` or
   `fulcra-coord update <id> --status active --agent claude-code:<host>:<repo>`.
   This stamps this session's task pointer, so PreCompact/SessionEnd hooks
   checkpoint it for the rest of this session's life.

Report milestones as you work (start / done / block); the hooks handle
start-surfacing, pre-compaction checkpoints, and session-end parking.
