# fulcra-netflix-skill

An agent skill that takes a brand-new user from "I messaged a skill link to
my bot" to "my Netflix viewing history lives in my own Fulcra account as a
Watched annotation, shared with the movie-night pool."

The deliverable is the **skill folder** (`skills/fulcra-netflix/`), not a
Python library: a runtime-agnostic SKILL.md conversation state machine
(auth → export → import → share) plus a vendored, PEP 723 self-contained
import script that any agent runs with `uv run`. The Python package wrapper
exists so the monorepo's pytest tooling covers the script; end users never
install it.

Status: **design phase** — see [docs/design.md](docs/design.md) for the
full spec (flow, record schema, error handling, what's deferred until
CLI sharing lands).

## Why this exists

It's the flagship concrete demo of Fulcra as an agent context layer: a
relatable dataset (Netflix history), imported by the user's own agent over
chat, landing in the user's own datastore, then shared into a pool that
group-recommendation agents can work over. See the design doc for the
composition with upstream `fulcradynamics/agent-skills` (onboarding auth
flow, ingest-beta record pattern) and this repo's `media-helpers` (export
walkthrough, fingerprint conventions).

## Layout

```
docs/design.md              the approved design spec
skills/fulcra-netflix/      the shippable skill (SKILL.md, references/, scripts/)   [pending]
fulcra_netflix/             test-support shims for the vendored script              [pending]
tests/                      parser/UUID/envelope tests over synthetic fixtures      [pending]
```
