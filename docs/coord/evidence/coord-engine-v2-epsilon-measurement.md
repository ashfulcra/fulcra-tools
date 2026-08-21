# Coord Engine v2 Feed-Visibility Evidence

**Status: BLOCKED — zero of two required credentialed-host measurements are recorded.**

This file is the activation evidence schema, not evidence that measurement has
happened. Do not set `public_read_epsilon_verified`, bump/release `2.0.0`, or
move the fleet pin until two distinct credentialed hosts have produced complete
rows and the configured epsilon is at least the largest observed value.

Run once per credentialed host. `--host-id` is a display-only label; it cannot
attest a host or make two runs on one machine count twice. The harness derives
`host_identity` from the sanitized machine identity, requires a persisted
`principal_identity`, and binds both to a one-way `credential_provenance`
fingerprint from the transport's real authentication seam:

```bash
coord-engine measure-feed-lag fulcra --host-id host-a --timeout 30 --poll 0.25
```

Record the complete one-value JSON output below. Never record tokens, account
identifiers, machine paths, or other credentials. Each successful row includes
the stable feed `update_id`, authoritative lifecycle `event_at`, local
`observed_at`, and the lag calculated between those timestamps; a path-only
`files` response is not evidence.

```json
{
  "schema": "coord.feed-visibility-lag-evidence.v1",
  "status": "BLOCKED",
  "measurements": [],
  "required_measurement_fields": [
    "host_identity",
    "display_label",
    "principal_identity",
    "credential_provenance",
    "update_id",
    "event_at",
    "observed_at",
    "observed_seconds"
  ],
  "observed_max_seconds": null,
  "configured_epsilon_seconds": null,
  "measured_at": null
}
```

The harness writes one nonce document and polls for that exact immutable path.
Only a comparable `after`/`through` window with a supported lifecycle state,
stable update identity, and aware authoritative event timestamp is accepted.
Timeout, malformed feed output, or an unproven write is `UNKNOWN` with rc 3.
An unmeasured or one-host epsilon is not a conservative estimate; it is no
bound at all and blocks activation.
