# coord-engine

The shared engine of **coord**, the agent-coordination layer — how agents on Fulcra work
with their user's other agents: coordinate work, discover what's new on every loop. It is
a **stdlib-only** Python CLI that gives a fleet of independent agents (Claude Code, Codex,
OpenClaw, CI, humans) durable coordination over the Fulcra File Store as a bus. Judgment stays in prose — the twelve
[`fulcra-agent-*` skills](../../skills) (of 14 total) — and every consistency-critical fold (who's live,
what's mine, is this review settled) is a deterministic engine verb, so two agents always
agree on derived state instead of eyeballing timestamps.

New to coord? Start with the [get-on-the-bus quickstart](../../docs/coord/GET-ON-THE-BUS.md)
(from zero: team bootstrap, auth, remote-sandbox requirements, the join sequence), the
protocol behind the design ([`COORDINATION-PROTOCOL.md`](../../COORDINATION-PROTOCOL.md)),
and the agent conventions ([`AGENTS.md`](../../AGENTS.md)).

## Install

```bash
uv tool install "git+https://github.com/ashfulcra/fulcra-tools@coord-engine-v1.6.8#subdirectory=packages/coord-engine"
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
| Directives & messaging | `tell` · `broadcast` · `remind` · `respond` · `later` (backlog) · `intent` (spoken commitment) · `handoff` · `listen` (the engine-owned watcher) |
| Dropped-work fold | `threads` (started-then-silent / blocked-on / intent-never-started, per principal) |
| Identity & liveness | `presence` · `agents` · `roles` (claim/release/status) · `escalate` |
| Operator loop | `asks` (waiting-for-operator, oldest first) · `answer` (unblock + hand back) |
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

## Environment / tuning

The single reference for every environment variable the engine reads. **Prefix rule:**
`COORD_*` is the engine-native, canonical prefix for all tuning knobs; `FULCRA_COORD_*`
is the legacy prefix, retained for the identity vars below and **alias-accepted for
`COORD_RETENTION_DAYS` only** (an operator migrating off the deprecated `fulcra-coord`
bus keeps working — when both are set, the `COORD_*` form wins). No other tuning knob
reads a `FULCRA_COORD_*` alias.

**Parse policy (all numeric knobs, one shared parser — `coord_engine/config.py`):** a
value is **positive-finite**, resolved **flag/constructor arg > env > default**; anything
unparseable, `NaN`, `inf`, or `≤ 0` falls back to the default — a bad value can never
disable a bound or make an op hang.

### Budgets & timeouts

| Variable | Default | Unit | Bounds |
|---|---|---|---|
| `COORD_TRANSPORT_TIMEOUT` | `30` | seconds | Hard per-op bound on every `fulcra-api file` subprocess. Constructor arg wins; run it TIGHT on a watcher (e.g. `8`) so the fold budgets buy real responsiveness. |
| `COORD_REVIEW_FOLD_BUDGET` | `45` | seconds | Aggregate deadline for the pending-review fold (`_pending_reviews_for`). |
| `COORD_BRIEFING_BUDGET` | `60` | seconds | Aggregate deadline for the `briefing`/`needs-me` transport-heavy add-on stack (chiefly the forge-feedback fan-out); opened once, spent cumulatively across sections. |
| `COORD_LISTEN_CLASSIFY_BUDGET` | `10` | seconds | Per-tick bound on the `listen` daemon's dir-only review-slug classification pass. |
| `COORD_OVERLAY_BUDGET` | `10` | seconds | Time bound on the freshness overlay's fresh-doc reads (the cap bounds read COUNT; this bounds TIME). |
| `COORD_OVERLAY_CAP` | `16` | count | Max fresh (unsummarized) task docs the overlay reads per surface-read before truncating (visibly). |
| `COORD_SUMMARY_TEXT_CAP` | `280` | chars | Per-field cap on `title`/`description` in a summaries row (ellipsis-marked). The index stays a *summary* — the full payload lives in the task doc; uncapped multi-KB directive payloads inflate `_coord/summaries.json` past what remote transports can read inside the fold budgets. |
| `COORD_THREADS_FOLD_BUDGET` | `30` | seconds | Aggregate deadline for the `threads` dropped-work fold's per-candidate reads; breach emits a `threads-degraded` row. |
| `COORD_THREADS_SILENCE_DAYS` | `3` | days | `threads` started-then-silent window (flag `--silence-days` wins). |
| `COORD_THREADS_INTENT_GRACE_HOURS` | `48` | hours | `threads` intent grace when an intent declares no window (flag `--intent-grace-hours` wins). |
| `COORD_ACKS_FULL_EVERY` | `12` | count | Passes between FORCED full ack folds in `reconcile`. The fold is change-driven (it asks the store what changed and re-folds only those slugs); this bounds how long a change the query never reported can persist, and carries the orphan-shard GC, which only rides the full fold. `1` disables the incremental path (every pass lists every ack dir). Any doubt — no change query, a query error, no anchor, a changed slug that wouldn't list — full-folds regardless of this knob, and does not advance the fold's anchor (`acks_folded_through`), so the unread change stays in the next pass's window. |
| `COORD_RETENTION_DAYS` | *unset → off* | days | When set `> 0` (or `--retention-days N`), `reconcile` archives terminal (`done`/`abandoned`) tasks older than N days to the cold archive. **OFF unless configured.** Legacy alias: `FULCRA_COORD_RETENTION_DAYS` (canonical wins; the legacy default of `30` is *not* adopted — coord-engine stays opt-in). |

### Identity, state & logging

| Variable | Default | Bounds |
|---|---|---|
| `FULCRA_COORD_AGENT` | `coord-reconcile:<host>` | Agent identity — set it to the **role** you act as (`--from` overrides per-command). Legacy prefix; still canonical for identity. |
| `FULCRA_COORD_HUMAN` | `human` | Operator handle for `--on-user` / `asks`. |
| `COORD_ENGINE_STATE_DIR` | `~/.local/state/coord-engine` | Local state root (write-verify nonce cache, etc.). |
| `COORD_LISTENER_STATE` | *(under the state dir)* | `listen` watcher's seen-ids state file. |
| `COORD_LOG_LEVEL` | `info` | Structured-log level to stderr (`debug`/`info`/`warn`/`error`). |

## Dev

```bash
uv run --extra dev pytest       # from packages/coord-engine/
```

The suite is CI-gated on Linux and macOS; run it locally before pushing (see
[`AGENTS.md`](../../AGENTS.md) → CI section). Design history:
[`docs/coord/`](../../docs/coord) and [`docs/coord-DESIGN.md`](../../docs/coord-DESIGN.md).

**Releasing:** cutting a `coord-engine-vX.Y.Z` tag REQUIRES bumping `__version__` in
[`coord_engine/__init__.py`](coord_engine/__init__.py) to the same `X.Y.Z` **in the same commit** —
`doctor` self-reports `__version__`, so a tag without the bump makes upgraded installs report a stale
version (v1.4.0/v1.5.0/v1.5.1 all shipped stale off a frozen `1.3.0`, caught by a remote field report).
