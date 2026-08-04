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
always report checkpoint age. `--max-age DURATION` accepts `s`, `m`, `h`, or `d` units (for example
`30m` or `2d`) and makes freshness mechanical: missing, invalid-age, or over-age checkpoints exit 2.
With no snapshot, `--json` returns
`{"snapshot":null,"checkpoint_age_seconds":null}`; future-dated `created_at`
is invalid-age rather than being clamped to a fresh zero seconds.

## Timeline emission (the checkpoint channel)

Every **successful** `snapshot` and `park` write also emits ONE moment to the account's
**Agent Checkpoint** channel, identity-tagged from the same `_coord/bus-v3/tags.json`
registry the bus uses. `resume`, `checkpoint --role`, and `briefing` are reads and emit
nothing.

```json
{"v":1,"kind":"checkpoint","agent":"amy","task":"role-reviewer",
 "objective":"<first 140 chars, hard slice, no ellipsis>",
 "path":"team/<team>/member/<agent>/continuity/<task>/latest.json"}
```

Driven by `team/<team>/_coord/bus-v3/checkpoints.json` — a document **separate from
`records.json`**, because an engine that has not upgraded classifies a bus authority
carrying unknown fields as malformed and fails its queue closed:

```json
{"schema": "coord.checkpoints-channel.v1",
 "data_type": "MomentAnnotation/<uuid>", "api_version": "v1alpha1"}
```

| config state | emission | stderr | exit code |
| --- | --- | --- | --- |
| absent | none | *(silent — pre-adoption teams)* | unchanged |
| ok | one moment per save | *(silent)* | unchanged |
| malformed | none | one LOUD line; never auto-created | unchanged |
| store unreadable | none | one line (UNKNOWN ≠ absent; not cached) | unchanged |
| record write refused/raised | none | one line | unchanged |

**Fail-open, the inverse of park's loud rule.** The checkpoint file is the source of
truth; the moment is its shadow. `park` exits non-zero and shouts `CHECKPOINT NOT
WRITTEN` when the *file* cannot be written, but no emission outcome may ever change an
exit code — failing a park because its telemetry failed would trade the load-bearing
act for its shadow. A `checkpoint moment:` line on stderr never means the checkpoint
was lost.

## Notes
- One `latest.json` per task; re-snapshotting overwrites it (the File Store keeps prior versions).
- `resume` with no `<task>` folds to the newest snapshot by `created_at` across the agent's tasks.
- Schema id: `coord.teams.continuity.v1`.
- `continuity park <team> [--agent X] [--role R] [--objective "…"] [--next "…"] [--open-question "…"]`
  snapshots **every** role the agent holds a fresh lease on and points each role doc's
  `checkpoint_ref` at it. `--role R` narrows the pass to that single role (a fresh lease on `R` is
  required) — the way to park one role without touching the checkpoints of the others.
- `continuity park` exits 2 and states `CHECKPOINT NOT WRITTEN` when the agent holds no fresh roles
  (under `--role`, no fresh lease on that role), and exits 1 with the same banner when the role state
  is unreadable rather than empty; rc 0 means at least one role checkpoint was written.
