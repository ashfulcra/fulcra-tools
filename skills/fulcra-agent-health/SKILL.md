---
name: fulcra-agent-health
description: "Operational visibility for a fulcra-agent-teams space: doctor preflight (tooling + store reachability), and a fleet health fold showing which hosts are keeping the team healed and who went dark."
homepage: "https://github.com/ashfulcra/coord2"
license: "MIT"
user-invocable: true
metadata: { "openclaw": { "emoji": "🩹" } }
---

# Fulcra Agent Health

Enhances [`fulcra-agent-teams`](https://github.com/fulcradynamics/agent-skills). When a team is healed by
scheduled reconciles across several machines, the operational questions are *"is anything actually
running?"* and *"which host went dark?"* — this skill answers them deterministically. (The digest +
role-escalation sweep land here too — A5b.)

## How it works
- Every `coord-engine reconcile` writes a small **health shard**
  (`_coord/health/<host-key>.json`: host, timestamp, engine version, task count, warnings) and prunes
  shards older than 30 days (age-based GC).
- **`coord-engine health <team>`** folds the shards: per-host last-reconcile age, STALE flag (>24h),
  engine version — exits non-zero when no host is fresh (usable as a monitor probe).
- **`coord-engine doctor [team]`** is the local preflight: storage CLI on PATH, File Store reachable,
  engine version. Run it after install and inside scheduled jobs' self-tests (the parent project's
  heartbeats failed silently on exactly these).

## Usage
```bash
uv tool run coord-engine doctor <team>          # preflight; exit 0 = healthy
uv tool run coord-engine health <team> [--json] # fleet fold; exit 1 if no fresh reconciler
```

## When to use
- After installing coord2 on a new machine (doctor).
- In monitoring/heartbeat wrappers (health --json; alert on `healthy: false`).
- Diagnosing "the index looks stale" — health shows which reconciler stopped.
