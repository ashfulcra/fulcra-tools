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
# roles/<name>.md — type: Role, with policy / sla_hours / maintainer in frontmatter;
# complete worked examples live in ../examples/
uv tool run fulcra-api file upload /tmp/reviewer.md "team/<team>/roles/reviewer.md"
```
(A `roles/index.md` is optional — the engine folds role status from the directory
listing; add one only as human courtesy.)

## Claim / refresh a lease
```bash
# Re-run to REFRESH (new timestamp) each time you act in the role; this is the liveness signal.
# Echoes the lease shard filename (slug + 6-char hash) — note it so you can inspect your exact
# shard (e.g. raw-read it and check the timestamp is one you wrote) or delete it by hand if needed.
coord-engine roles claim <team> reviewer [--agent <your-id>]
```
Never hand-write lease files: the engine names shards `<slug>-<hash6>.md` via `agent_key`, so a
hand-named `leases/<your-agent>.md` creates a SECOND shard for the same id — spurious CONTESTED on
exclusive roles.

## Read role status (the fold) — deterministic, via coord-engine
Do NOT classify by eyeballing timestamps. The engine folds policy + lease freshness:
```bash
coord-engine roles status "<team>" "reviewer" --json
# -> {status: HELD|VACANT|CONTESTED|UNKNOWN, policy, sla_hours, holders, fresh_holders, escalation_due}
```

## Release
```bash
coord-engine roles release <team> reviewer [--agent <your-id>]
```
(Deletes your engine-named shard. A raw `fulcra-api file delete` of a hand-guessed filename silently
misses the real shard — the lease then goes stale instead of released.)

## Escalate a vacancy (at most once per day)
`coord-engine escalate <team>` performs this whole sweep for every registered role
(computes `escalation_due`, writes the daily marker, notifies each maintainer) — prefer
it. The raw per-role equivalent:
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

## Dormancy — park a role without muting the sweep by hand
Deliberately leaving a role unattended (a reviewer on leave, seasonal on-call) is an ENGINE fact, not an
agent-side convention. Set `dormant_until: <ISO>` in the role doc:
```bash
# roles/<name>.md frontmatter — add alongside policy / sla_hours / maintainer:
#   dormant_until: 2026-08-01T00:00:00Z
uv tool run fulcra-api file upload /tmp/reviewer.md "team/<team>/roles/reviewer.md"
```
While that date is future, the mechanical `escalate` sweep **suppresses the role's vacancy escalation**
on every heartbeat host, and `roles status` reports `DORMANT (until <ts>)`:
```bash
coord-engine roles status <team> reviewer --json
# -> {status: DORMANT, dormant_until: <ts>, ...}
```
Escalation resumes automatically past the date; a live lease still shows HELD; and a **garbage
`dormant_until` fails OPEN** (noted on stderr, escalation still fires) so a typo can never silently mute
a role. See [`SKILL.md`](../SKILL.md) for the full doctrine.
