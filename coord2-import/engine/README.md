# coord-reconcile (L1)

The linchpin layer of coord2. Gives a `fulcra-agent-teams` namespace **queryable, self-healing views**
by scanning the OKF markdown and regenerating its indexes + a fast-path aggregate — no shadow store.

- **Reads:** `team/<team>/task/*.md` (OKF `type: Task` concept docs).
- **Owns (single-writer):** `task/index.md` (OKF §6), `task/log.md` (§7), `_coord/summaries.json` (aggregate).
- **Serves:** `status` / `board` / `needs-me` / `search` — one aggregate download each.
- **Property:** rebuilt from the live listing each pass, so the orphan-leak bug class cannot recur.

Full design: [`../../docs/proposals/teams-convergence/02-L1-coord-reconcile.md`](../../docs/proposals/teams-convergence/02-L1-coord-reconcile.md).

**Status:** **implemented** (L1 v0.1.0) — OKF parser, model, aggregate, query verbs, transport, reconcile
orchestration, CLI. 59 unit tests + a live end-to-end run against the real Fulcra File Store (reconcile →
`index.md`/`log.md`/`_coord/summaries.json` → `status`/`board`/`needs-me`/`search`, incremental reuse
confirmed).

## Usage
```
coord-reconcile reconcile <team>              # scan + heal task/index.md, log.md, _coord/summaries.json
coord-reconcile status    <team> [--json]
coord-reconcile board     <team> [--json]
coord-reconcile needs-me  <team> --agent <id> [--json]
coord-reconcile search    <team> <query> [--json]
```
Transport is the `fulcra-api file` CLI (override via `$FULCRA_CLI_COMMAND`). Structured JSON logs to
stderr (`$COORD_LOG_LEVEL`).

## Dev
```
uv run --extra dev pytest
```
