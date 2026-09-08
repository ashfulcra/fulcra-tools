# fde-engine

Engagement engine for the **fulcra-fde** skill (see
[`skills/fulcra-fde/SKILL.md`](../../skills/fulcra-fde/SKILL.md)) — the
deterministic half of a forward-deployed-engineer workflow that takes a
business plan, deck, or idea and builds it with Fulcra as the backend.

coord doctrine applies: **judgment stays in prose, bookkeeping is code.**
The skill decides how to interview a founder and how to map a product onto
Fulcra primitives; this package owns only what agents get wrong when
improvising across sessions and harnesses:

- the **seven-phase lifecycle** (`intake → interview → architecture → plan →
  prototype → build → retro`, with prototype allowed to loop back to
  architecture/plan when verification findings demand it),
- the **canonical file layout** at `fde/engagements/<slug>/` in the user's
  own Fulcra file store,
- **explicit-direction sync** (`push`/`pull`) with a local mirror, and
- the **deterministic resume brief** a fresh session reads first.

## Usage

Requires Python 3.12+ and an installed, authenticated `fulcra-api` CLI. From
the repository root:

    uv tool install ./packages/fde-engine
    fde-engine --help

The engine runs independently of coord-engine and Collect. Its engagement
commands read or write your Fulcra file store.

    fde-engine init example-project --title "Example project"
    fde-engine status <slug> [--json]     # phase + artifact checklist + next move
    fde-engine phase <slug> <new-phase>   # validated transition
    fde-engine sync <slug> push|pull [--dir DIR]
    fde-engine resume <slug>              # session-start brief
    fde-engine list [--json]

The local mirror defaults to `./fde/<slug>`, while the remote root is
`fde/engagements/<slug>/`. Sync is explicit and text-only. Push excludes the
machine-managed `engagement.md`; binary originals belong in
`intake/originals/` and need a separate upload. Review the direction before
syncing: push writes remote files, and pull writes local files. Neither direction
merges conflicting edits or propagates deletions.

**A pull can skip a file whose transport read returns `None` without failing
the pass.** The current transport does not distinguish that case from a missing
file. A successful sync is therefore not proof of a complete remote read.
Push also excludes hidden files/directories and does not follow symlinked
directories. These limits are documented in [the sync implementation](fde_engine/sync.py).

## Architecture

Stdlib-only; the transport shells out to the `fulcra-api` CLI binary
(`FULCRA_CLI_COMMAND` overrides the command). Every command function takes an
injected transport, so [the tests](tests) use fake storage and temporary local files. The transport is deliberately a copy of
coord-engine's proven shape, not an import — the FDE engine works standalone,
without the coordination bus.

Errors surface as clean one-line messages (`EngagementError`, `SyncError`,
`TransportError` all exit 1) — these errors are caught at the CLI boundary.

## Testing

    uv run --package fde-engine --extra dev --no-editable pytest packages/fde-engine/tests -q

Run from the repository root. The [e2e test](tests/test_e2e_fixture.py) drives a fixture one-page business
plan through all seven phases, including a prototype→plan backward loop, and
is the executable form of the design spec.
