---
name: fulcra-agent-health
description: "Operational visibility for a fulcra-agent-teams space: doctor preflight (tooling + store reachability), and a fleet health fold showing which hosts are keeping the team healed and who went dark."
homepage: "https://github.com/ashfulcra/fulcra-tools"
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
  (`_coord/health/<host-key>.json`: host, timestamp, engine version, `engine_build`, task
  count, warnings, `advanced`, `degraded_reason`) and prunes shards older than 30 days (age-based GC).
  **EVERY exit writes one, aborts included** — the shard write runs before the early
  `return` — so a shard proves a pass *ran*, never that it *advanced the index*.
- **`coord-engine health <team>`** folds the shards into TWO separate facts per host:
  - **fresh / STALE (>24h)** — shard recency. Did this host write a beat lately?
  - **`advanced`** — did the pass move the index? `False` prints
    `DID NOT ADVANCE: <reason>` and feeds the `aborting` rollup plus a headline
    warning. **A shard with no `advanced` key is `None` = UNKNOWN, never `True`**
    (an older engine wrote it; a missing field is not evidence a pass succeeded),
    counted in `unknown_advance`.

  Rollups count FRESH hosts only, because they answer "is the index moving now":
  `advancing`, `aborting`, `unknown_advance`. `fresh`/`healthy` and the exit code are
  unchanged — **`healthy: true` with `aborting > 0` is a real and dangerous state**, and
  a monitor alerting only on `healthy: false` will miss it. Alert on `aborting` too.

  Earned live: `team/fulcra` went ~16h without the index advancing while `health` read
  `4/7 host(s) fresh` with four `[ok]` rows; the only trace was an unlabelled `1 warn`.
  - **build vs the fleet pin** — `health` reads the `PIN=` line of the store's
    `_coord/bus-v3/adopt-latest.sh` once and compares each shard's `engine_build` (the
    VCS commit from the install's `direct_url.json`). Each host row shows
    `build <sha> = pin`, `build <sha> NOT the pin` (ahead or behind; offline the engine
    cannot tell which) or `build UNKNOWN`, and a headline counts FRESH hosts:
    `at_pin`, `off_pin`, `unknown_build` (JSON also carries `fleet_pin`, `pin_status` and
    per-host `pin_verdict`). **A shard with no `engine_build` (an older writer or a
    non-VCS install) is UNKNOWN, never current, and an unreadable pin makes every host
    UNKNOWN.** The exit code is unchanged. The version label cannot answer this: it changes only
    on a release, so hosts running different commits can report the same version.
- **`coord-engine doctor [team]`** is the local preflight: storage CLI on PATH, File Store reachable,
  engine version. Run it after install and inside scheduled jobs' self-tests (the parent project's
  heartbeats failed silently on exactly these).

## Operator digest (A5b)
`coord-engine digest <team>` folds the aggregate + presence into the four operator questions:
**blocked on you** (`needs:human` tags / tasks assigned to your handle — env `FULCRA_COORD_HUMAN`),
**upcoming** (`not_before` within 7 days), **agents** (liveness + open work each), and **stale**
(active tasks untouched > 48h). `--store` persists it to `_coord/digests/<date>-<window>.md`, deduped
per day+window (morning/evening) — heartbeat-safe. `--emit-timeline` additionally lands the digest as
a moment on the **'Agent Tasks — Digest'** timeline track: the record id is deterministic per
(team, day, window) and the typed ingest endpoint upserts on explicit ids (live-verified 2026-07-14),
so fleet races and retries converge on one record; a failed emit warns loud, keeps rc 0, and retries
on the next heartbeat tick (the `.emitted` state is written only after a confirmed emit). Requires the
`fulcra_common` writer installed next to coord-engine — without it the emit degrades to a loud warn.

## Role-vacancy escalation (A5b)
`coord-engine escalate <team>` sweeps every role doc: if a role is VACANT past its `sla_hours` and
today's marker doesn't exist, it writes the marker and files a **P1 directive to the role's
`maintainer`** ("claim it or reassign"). Idempotent per day; run it from the heartbeat.

## Usage
```bash
coord-engine doctor <team>          # preflight; exit 0 = healthy
coord-engine health <team> [--json] # fleet fold; exit 1 if no fresh reconciler
coord-engine digest <team> [--human H] [--json] [--store] [--emit-timeline]
coord-engine escalate <team>        # vacancy sweep (heartbeat-safe)
```

## When to use
- After installing coord on a new machine (doctor).
- In monitoring/heartbeat wrappers (health --json; alert on `healthy: false`
  **and on `aborting > 0`** — a fleet of fresh hosts that all abort still reports
  `healthy: true`, which is exactly the freeze that hid for 16h).
- Diagnosing "the index looks stale" — health shows which reconciler stopped, and now
  also which reconcilers are running but advancing nothing (`DID NOT ADVANCE`).
