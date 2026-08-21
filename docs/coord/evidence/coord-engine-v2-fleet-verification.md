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
measured_at: null
fleet_sla_seconds: null
verified_release: null
live_hosts: []
credentialed_verification_hosts: []
command_results: []
exclusions:
  MacBookPro.localdomain:
    host_identity: MacBookPro.localdomain
    reason: fresh v1.6.9 excluded by Ash operator ruling
    provenance: Ash operator ruling
  coord-reconcile:vm:
    host_identity: coord-reconcile:vm
    reason: stale reconciliation
    provenance: measured fleet census
    last_reconciled_at: null
    age_seconds: null
  coord-boss:
    host_identity: coord-boss
    reason: stale reconciliation
    provenance: measured fleet census
    last_reconciled_at: null
    age_seconds: null
  coord-maintainer:
    host_identity: coord-maintainer
    reason: stale reconciliation
    provenance: measured fleet census
    last_reconciled_at: null
    age_seconds: null
  Mac.localdomain:
    host_identity: Mac.localdomain
    reason: stale reconciliation
    provenance: measured fleet census
    last_reconciled_at: null
    age_seconds: null
  DeskbookPro.local:
    host_identity: DeskbookPro.local
    reason: stale reconciliation
    provenance: measured fleet census
    last_reconciled_at: null
    age_seconds: null
  cloud-claudecode-website:
    host_identity: cloud-claudecode-website
    reason: stale reconciliation
    provenance: measured fleet census
    last_reconciled_at: null
    age_seconds: null
  home-network-maintainer:
    host_identity: home-network-maintainer
    reason: stale reconciliation
    provenance: measured fleet census
    last_reconciled_at: null
    age_seconds: null
```

Every live-host row must carry a unique `host_identity`, exact engine version,
aware-UTC `last_reconciled_at`, and finite nonnegative `age_seconds`. Every
stale exclusion carries the same timestamp/age fields plus the exact measured
fleet-census provenance. Age must equal `measured_at - last_reconciled_at`
within the documented five-second serialization tolerance. NaN, infinity,
negative age, naive/malformed timestamps, inconsistent age, duplicate identity,
or missing ruling provenance is `UNKNOWN` and blocks activation.

Null timestamp/age fields are deliberately blocking placeholders. Populate
them from the same fleet census used for the release decision. No host may be
silently omitted.
