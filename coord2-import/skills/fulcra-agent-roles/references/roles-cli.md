---
name: fulcra-agent-roles-cli
description: "Exact commands for establishing roles, claiming/refreshing/releasing leases (coord-engine verbs), and escalating vacancies (fulcra-api file)."
---

# Fulcra Agent Roles — CLI reference

Lease operations (claim/refresh/release) are `coord-engine roles` verbs — the engine owns lease shard
naming. Role establishment and escalation remain raw `fulcra-api file` calls against the team namespace
(needs `fulcra-api auth login`). Every upload is versioned by the Fulcra File Store.

## Establish a role
```bash
# roles/<name>.md — type: Role, with policy / sla_hours / maintainer in frontmatter
uv tool run fulcra-api file upload /tmp/reviewer.md "team/<team>/roles/reviewer.md"
# add it to the roles index
uv tool run fulcra-api file upload /tmp/roles-index.md "team/<team>/roles/index.md"
```

## Claim / refresh a lease
```bash
# Re-run to REFRESH (new timestamp) each time you act in the role; this is the liveness signal.
# Echoes the lease shard filename (slug + 6-char hash) — note it so you can inspect your exact
# shard (e.g. raw-read it and check the timestamp is one you wrote) or delete it by hand if needed.
uv tool run coord-engine roles claim <team> reviewer [--agent <your-id>]
```
Never hand-write lease files: the engine names shards `<slug>-<hash6>.md` via `agent_key`, so a
hand-named `leases/<your-agent>.md` creates a SECOND shard for the same id — spurious CONTESTED on
exclusive roles.

## Read role status (the fold) — deterministic, via coord-engine
Do NOT classify by eyeballing timestamps. The engine folds policy + lease freshness:
```bash
uv tool run coord-engine roles status "<team>" "reviewer" --json
# -> {status: HELD|VACANT|CONTESTED|UNKNOWN, policy, sla_hours, holders, fresh_holders, escalation_due}
```

## Release
```bash
uv tool run coord-engine roles release <team> reviewer [--agent <your-id>]
```
(Deletes your engine-named shard. A raw `fulcra-api file delete` of a hand-guessed filename silently
misses the real shard — the lease then goes stale instead of released.)

## Escalate a vacancy (at most once per day)
```bash
# 1. first-writer-wins daily marker (dedupe)
uv tool run fulcra-api file upload /tmp/escalation.md \
  "team/<team>/roles/reviewer/escalations/$(date -u +%Y-%m-%d).md"
# 2. notify the maintainer via the teams inbox lifecycle
uv tool run fulcra-api file upload /tmp/notice.md \
  "team/<team>/member/<maintainer>/inbox/$(date -u +%Y%m%d-%H%M%S)_<you>_role-vacant-reviewer.md"
```

## Freshness window
Treat a lease as fresh if its `timestamp` is within the role's `sla_hours` (default 24h). A role with no
fresh lease is VACANT; an `exclusive` role with two or more fresh leases is CONTESTED.
