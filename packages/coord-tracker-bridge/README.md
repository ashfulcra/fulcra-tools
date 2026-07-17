# coord-tracker-bridge

`coord-tracker-bridge` mirrors coord work into an external tracker without
making that tracker authoritative. Its provider-neutral core defines normalized
source snapshots, a complete-identity state ledger, a versioned projection
policy, and a deterministic diff plan. Phase 2 adds a `coord-engine --json`
source adapter, a Linear GraphQL adapter, and explicit operator-controlled run
phases. Phase 3 adds a lower-fidelity, read-only `teams` source that reads only
typed task documents under `team/<team>/task/` and never depends on derived
coord-engine views.

The package fixes the unsafe shortcuts in the original Linear probe:

- identity is `(provider, namespace, item_id)`, never a title marker or short
  suffix;
- snapshots distinguish complete, unsupported, and degraded capabilities;
- destructive closes are suppressed only for the incomplete capability scope;
- policies declare field ownership and a bounded managed-label taxonomy;
- planning is diff-before-mutate and deterministic, so adapters can retry a
  partially executed run until it converges.
- issues, labels, projects, per-issue labels, comments, and inbound events are
  paginated; rate-limit retries use bounded exponential backoff;
- a singleton lease covers each `(source, tracker, policy)` run;
- full source identity is stored in the ledger and provider metadata, so a
  create that succeeds just before a ledger-write crash is rediscovered.

## Core contract

Source adapters produce a `Snapshot` containing `items`, `complete`,
`diagnostics`, `capabilities`, and `observed_at`. Tracker adapters normalize
managed records. `build_plan()` compares those inputs with `BridgeLedger` and a
versioned `Policy`, returning semantic create/update/reopen/close changes for an
adapter to execute.

```python
from coord_tracker_bridge import BridgeLedger, build_plan, load_policy

plan = build_plan(snapshot, tracker_records, BridgeLedger.load("state.json"), load_policy())
```

The policy bundled at `coord_tracker_bridge/policies/default-v1.json` is a
starting point. Command intake and expectation evaluation remain disabled and
out of scope.

## Run phases

Set `LINEAR_API_KEY` and either `LINEAR_TEAM_ID` or `--linear-team-id`. Then use
the phases in order:

```bash
coord-tracker-bridge plan --coord-team fulcra
coord-tracker-bridge apply-resources --coord-team fulcra
coord-tracker-bridge sync --coord-team fulcra
```

Use `--source teams` to read the strict base-teams convention directly. The
teams source requires `type: Task`, an explicit stable `id`, a title, a valid
status, and typed tags in every task document. `index.md` and `log.md` are
ignored as derived artifacts. Any ambiguous listing, read, parse, duplicate ID,
or unexpected entry degrades the task capability and suppresses absence-based
closes. Asks, threads, health, due dates, expectations, and command intake are
reported as `UNSUPPORTED`; they are never represented as clean empty results.

- `plan` is read-only and shows projection changes plus missing bounded
  taxonomy resources.
- `apply-resources` is the only phase that creates labels or projects.
- `sync` refuses a non-empty resource plan; it never silently creates resources.
  It also refuses an overlapping run holding the same source/tracker/policy
  lease.

State defaults to `~/.local/state/coord-tracker-bridge`. Secrets are environment
references only. GraphQL failures never log variables or source content.

## Test

```bash
uv run --package coord-tracker-bridge --extra dev --no-editable pytest packages/coord-tracker-bridge/tests -q
```
