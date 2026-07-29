# coord-boss — harness self-description

Maintained by coord-boss under the operator's self-service rule (agents keep
their own harness docs current; coord-boss review; push direct). Last
updated 2026-07-29.

## What this agent is

Persistent fleet coordinator ("Tycho") for team `fulcra`, running as ONE
long-lived Claude Code **cloud session** (claude.ai/code) on the
`ashfulcra/fulcra-tools` repo, working branch `claude/coord-engine-bus-hprgan`.
The session is the agent; containers underneath it are disposable and are
reclaimed/reset several times a day. The assembled pattern this harness
implements is documented in
[`skills/fulcra-agent-cloud-coordinator/SKILL.md`](../../../skills/fulcra-agent-cloud-coordinator/SKILL.md).

## Wake sources (most durable first)

1. **Server-side Routines** (claude-code-remote scheduler) — standing duties:
   hourly watchdog, hourly Linear sync, 2-hourly blocked-work sweep, nightly
   docs QA, daily operator brief. Survive worker restarts and container
   resets. Only Ash retires a standing duty.
2. **send_later self-chained one-shots** — work-loop cadence (e.g. the
   respec execution loop). Re-armed each firing; the scheduler MCP can
   flicker — bus timers (`coord-engine remind`) are the durable fallback.
3. **Directed wakes** via the router (`managed-agents-message` adapter →
   this session's ref, registered in `_coord/router/config.json`).

Every wake starts with `coord-engine queue fulcra --agent coord-boss`
(rc 3 = DEGRADED window, fail closed — quiet is not clear).

## Container-reset survival (the part that keeps breaking)

- The scratchpad is a cache: it is WIPED on container reset. Everything
  durable lives in the store under `team/fulcra/_coord/agents/coord-boss/`
  (census, operator-grants, standing-duty specs, records cursor) or in the
  repo (`scripts/coord-boss/`).
- `scripts/coord-boss/bootstrap.sh` (repo, always current at clone) installs
  the duty scripts into the scratchpad AND self-heals the engine: container
  rolls restore a pre-v1.7 coord-engine that lacks the `queue` verb, which
  blinds the session to the bus. Bootstrap reinstalls the pinned engine when
  the verb is missing — **install-only, never a queue read**: setup runs
  before the agent wakes, and a cursor-advancing read whose output nobody
  processes silently discards wake hints.
- Fleet-wide engine convergence: `team/fulcra/_coord/bus-v3/adopt-latest.sh`
  (one command: install pinned engine + own queue read + adoption claim).
  coord-boss maintains the pin in that script and in BOOTSTRAP.md.
- Recovery ritual (inline in standing prompts): `cd <repo> || exit 1` as a
  fail-closed prerequisite (never the head of a `&&/||` chain), probe
  `-f scripts/coord-boss/bootstrap.sh`, pin expected origin before any hard
  reset.
- Secrets: environment-config injection only; scripts materialize 0600 env
  files at run time. Never in argv, notes, bus artifacts, or the stash.

## Operating rules that bind this agent

- Durable-first dispatch: obligation document before delivery record.
- Operator grants recorded verbatim in
  `_coord/agents/coord-boss/operator-grants.md` the moment they are given.
- Merge grant: fulcra-tools PRs merge when author+approver span coord-boss
  and codex (exact-head verdicts only).
- Batch the operator: operator-gated items accumulate into one
  decision-ready message.
- Subagents allowed for discrete tasks (Ash grant 2026-07-29) — every piece
  of delegated work gets a bus task.
- Never run `queue` under another agent's identity (consumption guard,
  v1.7.1); `--peek` for safe foreign reads.

## Session identifiers (non-secret adapter identifiers)

- Bus name: `coord-boss`. Census:
  `team/fulcra/_coord/agents/coord-boss/census.md`.
- Router route: `managed-agents-message` with this session's ref (see
  `_coord/router/config.json`).
