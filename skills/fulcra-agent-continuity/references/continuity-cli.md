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
coord-engine continuity resume <team> <agent> <task> --json   # snapshot + derived checkpoint_age_seconds
coord-engine continuity resume <team> <agent> --max-age 1h    # rc 2 if missing/unknown/stale
```
The brief lists objective, next actions, open questions, recent decisions, and artifacts — deterministic,
so a fresh session or cron run re-establishes state without re-reading prose. Human and JSON output
always report checkpoint age. `--max-age DURATION` accepts `s`, `m`, `h`, or `d`
units (for example `30m` or `2d`) through `999999999d` and makes freshness
mechanical: invalid duration, invalid-age, or over-age checkpoints exit 2 and
JSON distinguishes them with `error_code` values `invalid-max-age`,
`checkpoint-age-unknown`, or `checkpoint-stale`.
With no snapshot, `--json` returns
`{"snapshot":null,"checkpoint_age_seconds":null,"error_code":null}`. Up to
one second of future clock skew is clamped to zero age; farther-future
`created_at` is invalid-age.

## Notes
- One `latest.json` per task; re-snapshotting overwrites it (the File Store keeps prior versions).
- `resume` with no `<task>` folds to the newest snapshot by `created_at` across the agent's tasks.
- Schema id: `coord.teams.continuity.v1`.
- `continuity park` exits 2 and states `CHECKPOINT NOT WRITTEN` when the agent holds no fresh roles;
  rc 0 means at least one role checkpoint was written.
