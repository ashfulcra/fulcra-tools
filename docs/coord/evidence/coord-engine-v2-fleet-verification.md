# Coord Engine v2 Fleet Verification

**Status: BLOCKED — release `2.0.0` does not exist and live adoption has not
been measured.**

“Live” means a host reconciled within the fleet SLA stated in the completed
evidence below. A merge, a tag, or a successful install on one host is not fleet
adoption. Every live host must report exact `2.0.0`; at least two credentialed
hosts must run queue, needs-me, review, forge, roles, presence, and reconcile.
Every `UNKNOWN` must be nonzero.

```yaml
schema: coord.engine-v2-fleet-verification.v1
status: BLOCKED
fleet_sla_seconds: null
verified_release: null
live_hosts: []
credentialed_verification_hosts: []
command_results: []
exclusions:
  MacBookPro.localdomain:
    reason: fresh v1.6.9 excluded by Ash operator ruling
  coord-reconcile:vm:
    reason: stale reconciliation
    last_reconciled_at: null
    age_seconds: null
  coord-boss:
    reason: stale reconciliation
    last_reconciled_at: null
    age_seconds: null
  coord-maintainer:
    reason: stale reconciliation
    last_reconciled_at: null
    age_seconds: null
  Mac.localdomain:
    reason: stale reconciliation
    last_reconciled_at: null
    age_seconds: null
  DeskbookPro.local:
    reason: stale reconciliation
    last_reconciled_at: null
    age_seconds: null
  cloud-claudecode-website:
    reason: stale reconciliation
    last_reconciled_at: null
    age_seconds: null
  home-network-maintainer:
    reason: stale reconciliation
    last_reconciled_at: null
    age_seconds: null
```

Null timestamp/age fields are deliberately blocking placeholders. Populate
them from the same fleet census used for the release decision. No host may be
silently omitted.
