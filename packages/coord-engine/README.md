# coord-engine

The shared engine of **coord**, the agent-coordination layer: a **stdlib-only** Python CLI
that gives a fleet of independent agents (Claude Code, Codex, OpenClaw, CI, humans) durable
coordination over the Fulcra File Store as a bus. Judgment stays in prose — the twelve
[`fulcra-agent-*` skills](../../skills) — and every consistency-critical fold (who's live,
what's mine, is this review settled) is a deterministic engine verb, so two agents always
agree on derived state instead of eyeballing timestamps.

New to coord? Start with the [get-on-the-bus quickstart](../../docs/coord/GET-ON-THE-BUS.md)
(from zero: team bootstrap, auth, remote-sandbox requirements, the join sequence), the
protocol behind the design ([`COORDINATION-PROTOCOL.md`](../../COORDINATION-PROTOCOL.md)),
and the agent conventions ([`AGENTS.md`](../../AGENTS.md)).

## Install

```bash
uv tool install "git+https://github.com/ashfulcra/fulcra-tools@coord-engine-v1.5.0#subdirectory=packages/coord-engine"
coord-engine doctor <team>   # tooling + auth + store reachability, end to end
```

Not on PyPI yet — install from the git tag (or a checkout:
`uv tool install ./packages/coord-engine`). The engine shells out to the
[`fulcra-api` CLI](https://pypi.org/project/fulcra-api/) for storage
(`uv tool install fulcra-api && fulcra auth login`); override the launcher via
`$FULCRA_CLI_COMMAND`. Identity comes from `$FULCRA_COORD_AGENT` — set it to the **role**
you act as (see the [presence skill](../../skills/fulcra-agent-presence/SKILL.md)).

## The verbs, by concern

| Concern | Verbs |
|---|---|
| Wake up / what needs me | `briefing` (THE entry fold) · `needs-me` · `inbox` · `digest` |
| Task views (self-healing) | `reconcile` · `status` · `board` · `search` · `task` |
| Directives & messaging | `tell` · `broadcast` · `remind` · `respond` · `later` (backlog) · `handoff` · `listen` (the engine-owned watcher) |
| Identity & liveness | `presence` · `agents` · `roles` (claim/release/status) · `escalate` |
| Review handshake | `review` (request/status) — obligation persists until the verdict file exists |
| Continuity | `continuity` (snapshot/checkpoint/park/resume) |
| Fleet ops | `health` · `doctor` · `forge` · `migrate` · `annotate` |
| ATC (cap routing) | `route` · `usage` · `headroom` · `atc` · `dash` |

`coord-engine <verb> --help` for flags; most read verbs take `--json`. The
[skills](../../skills) carry the procedures (when to run what, and why);
per-verb command references live in each skill's `references/` directory.

## Properties worth knowing

- **Stdlib-only runtime.** No dependencies; transport is a subprocess call. Installs
  anywhere Python ≥3.10 runs.
- **Deterministic folds.** Views are rebuilt from the live listing each pass
  (`reconcile`), so orphaned index entries cannot recur; role/review/presence status are
  computed, never inferred by a model.
- **Fails loud, never silent.** Unverifiable writes are retried, cached locally, and
  announced; a degraded read fold says so (`review-fold-degraded`, `LISTEN DEGRADED`)
  instead of returning a clean-looking partial answer.
- **Structured logs** to stderr (`$COORD_LOG_LEVEL`).

## Dev

```bash
uv run --extra dev pytest       # from packages/coord-engine/
```

The suite is CI-gated on Linux and macOS; run it locally before pushing (see
[`AGENTS.md`](../../AGENTS.md) → CI section). Design history:
[`docs/coord/`](../../docs/coord) and [`docs/coord-DESIGN.md`](../../docs/coord-DESIGN.md).
