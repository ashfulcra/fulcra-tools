# fde-engine

Engagement engine for the **fulcra-fde** skill (see
[`skills/fulcra-fde/SKILL.md`](../../skills/fulcra-fde/SKILL.md)) — the
deterministic half of a forward-deployed-engineer workflow that takes a
business plan, deck, or idea and builds it with Fulcra as the backend.

coord2 doctrine applies: **judgment stays in prose, bookkeeping is code.**
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

    uv tool install --from git+https://github.com/ashfulcra/fulcra-tools#subdirectory=packages/fde-engine fde-engine
    # (plain `uv tool install fde-engine` once the package is published to PyPI — do not use it before then)

    fde-engine init <slug> --title "Sourdough Coach"
    fde-engine status <slug> [--json]     # phase + artifact checklist + next move
    fde-engine phase <slug> <new-phase>   # validated transition
    fde-engine sync <slug> push|pull [--dir DIR]
    fde-engine resume <slug>              # session-start brief
    fde-engine list [--json]

## Architecture

Stdlib-only; the transport shells out to the `fulcra-api` CLI binary
(`FULCRA_CLI_COMMAND` overrides the command). Every command function takes an
injected transport, so the test suite (`uv run pytest packages/fde-engine`)
runs entirely in memory. The transport is deliberately a copy of
coord-engine's proven shape, not an import — the FDE engine works standalone,
without the coordination bus.

Errors surface as clean one-line messages (`EngagementError`, `SyncError`,
`TransportError` all exit 1) — never a traceback at the user.

## Testing

    uv run pytest packages/fde-engine

The e2e test (`tests/test_e2e_fixture.py`) drives a fixture one-page business
plan through all seven phases, including a prototype→plan backward loop, and
is the executable form of the design spec.
