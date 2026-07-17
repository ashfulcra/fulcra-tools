# coord-tracker-bridge

`coord-tracker-bridge` is the provider-neutral projection core for mirroring
coord work into an external tracker. Phase 1 is intentionally pure: it defines
normalized source snapshots, a complete-identity state ledger, a versioned
projection policy, and a deterministic diff plan. It performs no network calls
and no tracker mutations.

The package fixes the unsafe shortcuts in the original Linear probe:

- identity is `(provider, namespace, item_id)`, never a title marker or short
  suffix;
- snapshots distinguish complete, unsupported, and degraded capabilities;
- destructive closes are suppressed only for the incomplete capability scope;
- policies declare field ownership and a bounded managed-label taxonomy;
- planning is diff-before-mutate and deterministic, so adapters can retry a
  partially executed run until it converges.

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
starting point. Resource creation, tracker I/O, source adapters, singleton
leases, and bounded retry/backoff are deliberately phase-2 concerns. Command
intake and expectation evaluation are not part of this package phase.

## Test

```bash
uv run --package coord-tracker-bridge pytest packages/coord-tracker-bridge/tests -q
```
