---
name: fulcra-agent-reconcile-cli
description: "Exact commands to run coord-engine reconcile over a fulcra-agent-teams namespace."
---

# Fulcra Agent Reconcile — CLI reference

The logic lives in the shared **`coord-engine`** tool
([`packages/coord-engine`](../../../packages/coord-engine)). It shells out to `fulcra-api file`
for all storage I/O, so the Fulcra CLI must be authenticated (`fulcra auth login`).

## Install / run

Install once, then invoke the bare binary (`coord-engine` is not on PyPI — `uvx`/`uv tool run`
cannot resolve it). The pinned install command lives in
[`packages/coord-engine/README.md`](../../../packages/coord-engine/README.md) — one home, don't copy it here.
```bash
coord-engine reconcile <team>
```

## Commands
```bash
# Fold data-updates changes (or full-scan fallback) -> heal index/log/summaries
coord-engine reconcile <team>

# Read views (one aggregate download each; run reconcile first):
coord-engine status   <team> [--json]          # counts by status
coord-engine board    <team> [--json]          # open work grouped active/waiting/blocked/proposed
coord-engine needs-me <team> --agent <id> [--json]  # assigned-to / blocking <id>, gated on not_before; PLUS pending-required reviews for <id> or any role it holds (rows with type: review-pending)
coord-engine search   <team> <query> [--json]  # substring over id/title/description/tags
```

## Environment
- `FULCRA_CLI_COMMAND` — override the storage CLI (default `fulcra-api`). E.g. `uv tool run fulcra-api`.
- `FULCRA_COORD_AGENT` — identity recorded as `reconcile_host` in the aggregate (default `coord-reconcile:<hostname>`). Set it to the ROLE you act as, not a folder/cwd string — see fulcra-agent-presence "Pick your identity by ROLE". EXCEPTION: scheduled reconcile heartbeats feed per-host health shards, so when several hosts run the heartbeat use `role@host` variants — collapsing them onto one id would mask a dead host while any other host beats.
- `COORD_LOG_LEVEL` — `debug|info|warn|error` (structured JSON logs to stderr; default `info`).

## Behavior notes
- **Incremental:** a durable cursor in `_coord/summaries.json` consumes the authoritative
  `data-updates` feed and reads only changed task shards. Unrelated events still advance the cursor.
- **Fail closed:** a missing/corrupt cursor, unavailable/malformed feed, doubtful lifecycle, or
  unreadable changed shard takes the full listing scan. If that fallback listing fails, the pass
  aborts and writes nothing (the prior index/log/aggregate stay intact).
- **Drift check:** every `COORD_RECONCILE_FULL_EVERY` passes (default 72), or when the aggregate is too
  old, a full scan compares against the incremental view and loudly rebuilds any divergence.
- **Concurrency:** output remains deterministic from the feed lifecycle ordering and the same full-scan
  ground truth; Fulcra File Store is last-writer-wins and versions every write.

## Tests
```bash
uv run pytest packages/coord-engine -q
```
