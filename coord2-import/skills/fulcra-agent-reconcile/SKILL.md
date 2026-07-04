---
name: fulcra-agent-reconcile
description: "Give a fulcra-agent-teams space self-healing, queryable task views: scan the team's OKF task docs, regenerate task/index.md and log.md, and answer status/board/needs-me/search in one read."
homepage: "https://github.com/ashfulcra/coord2"
license: "MIT"
user-invocable: true
metadata: { "openclaw": { "emoji": "🩺" } }
---

# Fulcra Agent Reconcile

Enhances the [`fulcra-agent-teams`](https://github.com/fulcradynamics/agent-skills) skill. Bare teams
tracks tasks as OKF markdown under `team/<team>/task/` and asks agents to hand-maintain `task/index.md`.
At any real scale that index drifts. This skill makes the index **engine-owned and self-healing** and adds
**structured queries** the convention otherwise lacks — without changing how tasks are written.

## What it does
A bundled stdlib-only tool (`coord-reconcile`) that, for a given team:
- **Scans** `team/<team>/task/*.md` (OKF `type: Task` concept docs).
- **Heals** `task/index.md` (OKF §6, grouped by status) and appends `task/log.md` (OKF §7, status
  transitions) — regenerated from the live listing each pass, so stale/orphaned entries cannot accrue.
- **Emits** `team/<team>/_coord/summaries.json` — a fast-path aggregate so reads are one download, not N.
- **Answers** `status` / `board` / `needs-me` / `search` from that aggregate.

Properties: **orphan-proof** (full rebuild from ground truth), **incremental** (skips unchanged files by
the `fulcra-api file list` timestamp), **degraded-safe** (a listing failure aborts the pass and leaves the
prior index intact — never publishes a truncated view).

## The OKF Task contract (what a task doc looks like)
```yaml
---
type: Task                         # OKF required
title: Fix the widget             # OKF display name
description: one-line summary       # OKF — becomes the index bullet text
timestamp: 2026-07-01T14:00:00Z    # OKF last-change time
tags: [workstream:web, kind:bug]
# coord extensions (OKF-legal producer keys):
status: active                     # proposed|active|waiting|blocked|done|abandoned
priority: P1                       # P0|P1|P2|P3
assignee: ash                      # for needs-me
owner: claude-code:host:web
blocked_on: null
due: null
not_before: null                   # hides from needs-me until this time
---
<body: human notes>
```
Bare-teams tasks that lack the extension keys are still first-class — missing `status`/`priority` are
backfilled (`proposed`/`P2`).

## When to use
- After creating/updating tasks in a team space, to refresh the index and views.
- On a schedule (a heartbeat) to keep a busy team's index healed.
- Whenever you want to query a team's work (`status`/`board`/`needs-me`/`search`) instead of reading files.

## Ownership rule
Once you use this skill, `task/index.md` and `task/log.md` are **engine-owned** — let the tool regenerate
them; edit task *content* docs, not the indexes. `_coord/summaries.json` is a cache (delete + re-run
reproduces it). Recoverable archival is **move-not-delete** (Fulcra `file delete` isn't CLI-undoable).

## Retention (optional add-on)
With `--retention-days N` (or env `COORD_RETENTION_DAYS`), reconcile archives terminal tasks older than N
days to `task/archive/<YYYY-MM>/` — a **verified move** (copy → read-back → delete), never a bare delete —
and moves the task's ack/response shards with it. Once per day, capped per pass. `coord-engine task
restore <team> <slug>` brings one back; `coord-engine search <team> <q> --archived` searches the cold
archive. Off by default.

## Usage
This skill drives the shared **`coord-engine`** tool — invoked the same way this ecosystem already
invokes `fulcra-api` (`uv tool run …`), so the skill itself stays pure prose + references (no bundled
code). Needs `fulcra-api` authenticated and `coord-engine` installed (`uv tool install coord-engine`, or
from source: `uv tool install <coord2>/engine`). See [`references/reconcile-cli.md`](references/reconcile-cli.md).
```bash
uv tool run coord-engine reconcile <team>            # scan + heal index/log + write the aggregate
uv tool run coord-engine board    <team>
uv tool run coord-engine needs-me <team> --agent <id>
```
