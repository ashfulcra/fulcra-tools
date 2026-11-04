# Extraction provenance

Source snapshot: prototype commit `02ef7475d7e685278e21c59c0817bc2a9ac06f68`.
Destination baseline: fulcra-tools commit
`8afb5a7c039b9ca9186eecb4a354094c8e64c501`. Source paths below are retained
unchanged, so all imports resolve inside the installed package.

Runtime files copied from `src/lib/gatekeeper/`:

```text
work-contract.js
work-projection.js
work-digest.js
checkpoint.js
handoff.js
protocol.js
projection.js
listener.js
```

Runtime files copied from `src/lib/server/gatekeeper/`:

```text
work-transport-config.js
work-transport-read.js
work-transport-store.js
work-transport-publish.js
listener-validation.js
listener-store.js
listener-runtime.js
work-listener.js
```

Copied commands: `scripts/gatekeeper-work-transport.mjs`,
`scripts/gatekeeper-listener.mjs`, `scripts/work-listener-observation.mjs`.
Their behavior is dispatched by the new `bin/coord-v5.mjs`; wire namespace strings
and validation boundaries were not migrated.

Copied tests: all eight core module `.test.js` peers above, plus
`src/lib/gatekeeper/work-acceptance.test.js`; all runtime `.test.js` peers except
`listener-validation` (covered by listener runtime/CLI), plus
`src/lib/server/gatekeeper/listener-cli.test.js`; and
`scripts/gatekeeper-work-transport.test.js`. Total: 18 copied test files.
Copied fixtures in `tests/fixtures/`: `conversation.json`,
`work-backlog-synthetic.json`, `work-handoff-synthetic.json`,
`listener-observation-synthetic.json`.

Privacy transformation applied consistently to copied code and tests: the source
probe's account UUID and stream UUID were replaced with reserved synthetic UUIDs
ending in `000000000900` and `000000000901`. This retains a pinned synthetic
transport without shipping a personal account/source binding. A later pass
(2026-10-03) also replaced a name-based record-id series in the transport read
and publish tests, whose origin could not be shown to be synthetic, with
`00000000-0000-4000-8000-0000000009f6`–`9f9`. No other semantic rewrite was made. No intake/admission, web app, deployment, private database,
credential, or real-agent evidence was extracted.

Task2 generalizes synthetic transport pins to explicit validated configuration,
preserving ownership preflight, branded receipts, SQLite scope and wire names.
New `work-view.js` factors the listener policy/replay seam without duplicating
state. New `coord-v5-operations.mjs` exposes retained view/digest and bounded stdin
wrappers over existing validation/package/readiness APIs. Installed integration
tests use synthetic principals, fixture responses and real local SQLite only.

New package-owned files: package manifest/exports/files allowlist, root API
namespaces, standalone Vitest config, product bin dispatcher, packed-install
smoke test, this provenance file, and README. The MIT license is the destination
repository license.
