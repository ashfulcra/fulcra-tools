---
name: fulcra-agent-continuity-cli
description: "coord-engine continuity snapshot/resume commands + the snapshot schema."
---

# Fulcra Agent Continuity — CLI reference

Both commands are `coord-engine continuity …` (the engine writes/reads structured JSON via
`fulcra-api file`; needs `fulcra-api auth login`).

## Snapshot
```bash
coord-engine continuity snapshot <team> <agent> <task> --objective "…" \
    [--next "…" ...]            # repeatable
    [--decision "…" ...]        # repeatable
    [--open-question "…" ...]   # repeatable
    [--artifact "…" ...]        # repeatable (links to deliverables)
    [--context-percent 40]      # how full your context was at snapshot time
    [--transcript <path>]
# writes team/<team>/member/<agent>/continuity/<task>/latest.json (versioned by the File Store)
```

## Resume
```bash
coord-engine continuity resume <team> <agent> <task>    # brief for one task's latest snapshot
coord-engine continuity resume <team> <agent>           # newest snapshot across the agent's tasks
coord-engine continuity resume <team> <agent> <task> --json   # raw snapshot JSON
```
The brief lists objective, next actions, open questions, recent decisions, and artifacts — deterministic,
so a fresh session or cron run re-establishes state without re-reading prose.

## Notes
- One `latest.json` per task; re-snapshotting overwrites it (the File Store keeps prior versions).
- `resume` with no `<task>` folds to the newest snapshot by `created_at` across the agent's tasks.
- Schema id: `coord.teams.continuity.v1`.
