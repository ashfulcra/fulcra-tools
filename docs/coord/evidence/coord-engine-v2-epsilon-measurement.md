# Coord Engine v2 Feed-Visibility Evidence

**Status: BLOCKED — zero of two required credentialed-host measurements are recorded.**

This file is the activation evidence schema, not evidence that measurement has
happened. Do not set `public_read_epsilon_verified`, bump/release `2.0.0`, or
move the fleet pin until two distinct credentialed hosts have produced complete
rows and the configured epsilon is at least the largest observed value.

**Candidate host note — not measurement evidence:** coord-boss provisionally
offers the distinct credentialed host `vm` as host 2 only after this harness is
approved and pushed. That pairing is sampling-biased: the two hosts are the
implementer/reviewer hosts, both are heavy store users, and `vm` is stale as a
reconcile participant. If used, disclose the pair as floor evidence rather than
a representative fleet estimate; an independent credentialed host is preferred.
Do not run either measurement from this blocked template.

Run once per credentialed host. `--host-id` is a display-only label; it cannot
attest a host or make two runs on one machine count twice. The harness derives
`host_identity` from the sanitized machine identity and resolves the write
principal through the shared authority: explicit API input, then
`FULCRA_COORD_AGENT`, then persisted per-cwd identity. Hostname fallback is
disabled for the write principal. The two evidence runs require
`principal_source: env`, so export `FULCRA_COORD_AGENT` on both hosts; a
hostname can only attest `host_identity`, never mint `principal_identity`.
The harness reads stable non-secret `transport_authority` from the canonical
team configuration and binds the complete probe evidence, including
`principal_identity` and `principal_source`, to an
`evidence-sha256` `credential_provenance`. The binding does not read or hash a
transient access credential, so token refresh and authentication mode changes
cannot manufacture a second host identity. `producer_build` is the running
installation's exact PEP 610 VCS commit; an editable/wheel build without that
identity is `UNKNOWN`:

```bash
export FULCRA_COORD_AGENT=<approved-agent-identity>
# After the reviewed harness commit is pushed, install that exact VCS build;
# do not substitute a checkout/path/wheel install, which lacks the PEP 610 SHA.
uv tool install --force "git+https://github.com/ashfulcra/fulcra-tools@<approved-full-commit>#subdirectory=packages/coord-engine"
coord-engine measure-feed-lag fulcra --host-id host-a --timeout 30 --poll 0.25
```

Record each complete one-value JSON output without reshaping it. The producer's
`coord.feed-visibility-lag.v1` row is the only schema accepted by the activation
fence; there is no parallel aggregate or hand-authored maximum schema. Never
record secrets or machine paths. Each successful row includes the stable feed
`update_id`, exact correlated `probe_path`, authoritative lifecycle `event_at`,
local `observed_at`, and `observed_seconds` calculated between those timestamps;
a path-only `files` response is not evidence.

```json
{
  "schema": "coord.feed-visibility-lag.v1",
  "state": "DATA",
  "team": "fulcra",
  "host_identity": "coord-reconcile:<canonical-host>",
  "display_label": "host-a",
  "principal_identity": "<FULCRA_COORD_AGENT value>",
  "principal_source": "env",
  "transport_authority": {
    "data_type": "<canonical-data-type>",
    "api_version": "<canonical-api-version>"
  },
  "probe_schema": "coord.feed-visibility-lag-probe.v1",
  "producer_build": "<exact lowercase VCS commit>",
  "credential_provenance": "evidence-sha256:<64 lowercase hex characters>",
  "credentialed": true,
  "observed_seconds": "<finite nonnegative seconds>",
  "measured_at": "<aware UTC instant>",
  "probe_id": "<32 lowercase hex characters>",
  "probe_path": "team/fulcra/_coord/projections/lag-probes/<probe_id>.json",
  "event_at": "<aware authoritative feed instant>",
  "observed_at": "<aware local observation instant>",
  "update_id": "<canonical UUID>",
  "reason": null
}
```

The harness writes one nonce document and polls for that exact immutable path.
Only a comparable `after`/`through` window with a supported lifecycle state,
stable update identity, and aware authoritative event timestamp is accepted.
The entire harness deadline starts before identity/authentication preflight and
covers canonical-authority acquisition, probe upload, feed polling, and final
observation, serialization, hashing, result construction, and renderer return.
Every transport operation receives only its remaining budget; an operation
without deadline support is refused before it starts. Timeout,
malformed feed output, missing stable authority, or an unproven write is
`UNKNOWN` with rc 3.

Activation revalidates every row exactly: field set, canonical host and update
identities, stable authority, probe/path correlation, evidence binding, aware
timestamps, and the finite nonnegative lag derived from
`observed_at - event_at`. Both rows must form a single canonical cohort: exact
case-sensitive team, the complete canonical transport authority/account/store
identity (including every versioned authority field when present), measurement
schema, `probe_schema`, `producer_build`, and `principal_source` (`env` for this
two-host protocol). Caller-normalized aliases do not declare equivalence. The
configured epsilon lives in release configuration, not in these measurement
rows, and must cover the maximum of two distinct valid rows.
An unmeasured or one-host epsilon is not a conservative estimate; it is no
bound at all and blocks activation.
