# Task 1: bounded receiver checkpoint verification

## Scope and result

Implemented `verifyCheckpointArtifact(bytes, publicationEvent)` and
`coord-v5 checkpoint verify --artifact ABS --publication ABS`. The library
checks raw SHA256 before fatal UTF-8 decoding, checkpoint validation and
normalization, body digest, scope, checkpoint/work/assignment bindings, and full
actor identity. The CLI reads only bounded private files. No network adapter,
trust grant, readiness, replay, or receipt behavior changed.

## TDD evidence

- RED: `npx vitest run --config vitest.config.js src/lib/gatekeeper/checkpoint.test.js`
  exited 1: 4 new tests failed with `TypeError: verifyCheckpointArtifact is not a function`;
  56 existing tests passed.
- RED CLI: `NPM_CONFIG_CACHE=/tmp/coord-v5-checkpoint-verify-cache node --test test/install.test.mjs`
  exited 1: packed-install help lacked `checkpoint verify --artifact ABS --publication ABS`.
  An earlier unconfigured invocation could not access the pre-existing user npm cache
  (`EPERM`); the prescribed `/tmp` cache resolved that environmental issue.
- GREEN focused: `NPM_CONFIG_CACHE=/tmp/coord-v5-checkpoint-verify-cache npx vitest run --config vitest.config.js src/lib/gatekeeper/checkpoint.test.js`
  exited 0: 60/60 passed.
- GREEN packed install: `NPM_CONFIG_CACHE=/tmp/coord-v5-checkpoint-verify-cache node --test test/install.test.mjs`
  exited 0: 1/1 passed.
- Final package suite: `NPM_CONFIG_CACHE=/tmp/coord-v5-checkpoint-verify-cache npm test`
  exited 0: 21 unit files, 391 unit tests, 4 install tests passed; zero failed.
- `git diff --check` exited 0 with no output.

## Files

`packages/coord-v5/src/lib/gatekeeper/checkpoint.js`, its focused tests,
`packages/coord-v5/scripts/coord-v5-operations.mjs`, the bin help, the packed
install test, `packages/coord-v5/README.md`, root `README.md`, and root
`AGENTS.md`.

## Self-review / limits

- Success reports measured local byte and normalized-content facts only, not
  authenticated remote retrieval, authorization, native wake delivery, or
  source completeness.
- Invalid content and parser errors are reduced to stable codes; no bytes or
  private paths are echoed. The CLI retains the existing `privatePath` permission
  rules and file-read pattern, without weakening its other callers.
- No independent issue or unresolved blocker found in this bounded change.
