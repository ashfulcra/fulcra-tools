# coord-boss — harness self-description

Maintained by coord-boss under the operator's self-service rule (agents keep
their own harness docs current; coord-boss review; push direct). Last
updated 2026-08-06.

## Cold start — read these, in this order

Required by [`CHECKPOINT-HANDOFF.md`](../CHECKPOINT-HANDOFF.md); this is the
stable place handoff checkpoints point at, so a successor needs one pointer
instead of a reading list copied into every park (and rotting there).

1. [`docs/coord/GET-ON-THE-BUS.md`](../GET-ON-THE-BUS.md) — the join ceremony.
   Run it before anything else. **Adopt the pin FIRST**: a restored container
   carries a stale engine, and a stale engine reads the bus wrong while
   looking healthy.
2. **The store's authorities, which outrank every doc in this repo** —
   `team/fulcra/_coord/bus-v3/adopt-latest.sh` (the `PIN=` line is fleet
   truth), `records.json` (the channel — resolve it every wake, never
   hardcode), `tags.json` (identity tags), and `BOOTSTRAP.md` (the standing
   read/send contract). A pin or channel copied into prose is a future silent
   failure; read them, don't remember them.
3. `coord-engine continuity resume fulcra coord-boss` — the predecessor's
   park. Its **decisions** section is standing law and is not to be
   re-litigated; its **artifacts** section is the live reading list.
4. This document, below — wake sources, container-reset survival, and the
   operating rules that bind the role.
5. `team/fulcra/_coord/bus-v3/directives/` — the live rulings. The
   `2026-08-05-*` and `2026-08-06-*` files are current; earlier ones are
   history unless a park names them.
6. [`AGENTS.md`](../../../AGENTS.md) — review handshake, delivery rule,
   backlog, ATC routing — plus the `team/fulcra/review/` register conventions
   (exact 40-hex heads; verdicts at `<slug>/verdicts/<head>--<reviewer>.md`).

Then prove the takeover rather than asserting it: `doctor --self` (engine
currency), `doctor --delivery --agent coord-boss` (your writes land), a
`presence beat`, a `roles claim` — a claim printing *taking over an existing
lease shard* is the expected shape for a continuation — and `bus-v3
tag-provision` with your ACTUAL model, since the engine cannot verify that
declaration and a stale one mislabels everything you send.

**Before the turn ends, arm a scheduled wake** (§ Wake sources). Routines
bind to the session that created them, so an inherited one wakes your
predecessor, not you: a successor re-creates its own.

## What this agent is

Persistent fleet coordinator ("Tycho") for team `fulcra`, running as ONE
long-lived Claude Code **cloud session** (claude.ai/code) on the
`ashfulcra/fulcra-tools` repo. The session is the agent; containers
underneath it are disposable and are reclaimed/reset several times a day —
and the session itself is mortal, so the handoff park is the real continuity
mechanism, not the container. Each generation works on its own branch (this
one: `claude/coord-boss-handoff-resume-60sjua`). The assembled pattern this
harness implements is documented in
[`skills/fulcra-agent-cloud-coordinator/SKILL.md`](../../../skills/fulcra-agent-cloud-coordinator/SKILL.md).

## Wake sources (most durable first)

1. **Server-side Routines** (claude-code-remote scheduler) — standing duties:
   hourly watchdog, hourly Linear sync, 2-hourly blocked-work sweep, nightly
   docs QA, daily operator brief. Survive worker restarts and container
   resets. Only Ash retires a standing duty. **They bind to the session that
   created them, not to the role** — so they survive a container reset but NOT
   a session handoff: a successor's first duty is re-creating its own, and the
   predecessor's keep firing into a dead conversation until they are retired.
   Minimum interval is hourly; anything finer needs an in-session cron, which
   dies with the session and is therefore never the survival wake.

   **The successor CANNOT retire the predecessor's Routines** (verified by
   attempt, 2026-08-06): `update_trigger` returns *"updating a trigger bound to
   another session is not enabled for this organization"*, and it carries no
   `persistent_session_id` field, so "retarget" is not a thing that exists —
   the choices are disable or delete, and only from the owning session or the
   Routines UI. Plan the handoff around that: **the old session must retire its
   own wake sources before it goes dark**, or the operator does it by hand.
   A successor that assumes it can clean up after its predecessor will leave
   the dead session being woken indefinitely. Re-creating the equivalents is
   the successor's half; retiring the originals is not.
2. **send_later self-chained one-shots** — work-loop cadence (e.g. the
   respec execution loop). Re-armed each firing; the scheduler MCP can
   flicker — bus timers (`coord-engine remind`) are the durable fallback.
3. **Directed wakes** via the router (`managed-agents-message` adapter →
   this session's ref, registered in `_coord/router/config.json`).

Every wake starts with `coord-engine queue fulcra --agent coord-boss`
(any nonzero exit = fail closed, quiet is not clear: never infer the
terminal state from the rc — both rc 2 and rc 3 carry multiple states;
read `state` + `error_code` from the `--json` envelope. INVALID is
human-fixable, not retryable. `obligations` has a fixed split:
rc 3 = UNKNOWN, rc 4 = INVALID).

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
- **Fulcra credentials do not cross into a new session's environment**
  (verified on the 2026-08-06 handoff join). A container *restart* keeps
  `~/.config/fulcra/credentials.json`; a *reclaim* or a **new session** starts
  with none, and every bus verb then fails with `No credentials found`. That
  is the one step needing a human: `fulcra auth login` (device flow —
  `--get-auth-url`, one operator tap, then `--device-code <code>`). It
  persists thereafter, and the refresh grant covers expiry without bothering
  Ash again. Surface it at the adopt step, where it fails loudly, rather than
  discovering it mid-duty.

## Operating rules that bind this agent

- Durable-first dispatch: obligation document before delivery record.
- Operator grants recorded verbatim in
  `_coord/agents/coord-boss/operator-grants.md` the moment they are given.
- Merge grant: fulcra-tools PRs merge when author+approver span coord-boss
  and codex (exact-head verdicts only).
- Batch the operator: operator-gated items accumulate into one
  decision-ready message.
- **ALWAYS capture harness-specific behaviour in
  [`HARNESS-MAP.md`](../HARNESS-MAP.md), in the same pass that discovers it**
  (operator instruction, Ash 2026-08-07). Not only incidents — any behaviour
  that is TRUE OF ONE HARNESS AND NOT ANOTHER: what refuses, what silently
  no-ops, what a scheduler reaps, what a classifier gates, what an API permits
  from one environment and not another. The test is not "was this a wall" but
  "would an agent on a different harness be surprised by this, or an agent on
  the SAME harness have to rediscover it."
  Why this is a standing duty and not a nicety: every one of these costs a full
  diagnosis cycle to rediscover, and the rediscovering agent usually has no way
  to know it is re-treading ground — the failure looks like a bug in the work,
  not in the environment. Three separate agents lost time this week to harness
  facts that were known and unwritten. Capture it while the evidence is in
  front of you; a fact you mean to write up later is a fact the next agent
  pays for.
- **Verify a dispatch by reading it out of a RECIPIENT's queue** — never by the
  sender's exit code, and never by finding the record on the channel. Three
  separate mechanisms have now produced a send that printed rc 0 and reached
  nobody: a retired channel, a prose note, and (2026-08-07) `broadcast`
  addressing the task plane's `"*"` while every reader keeps only `"all"`. In
  the last one the record was on the LIVE channel in a correct `v:1` shape and
  still undelivered, so even "I found the record" is not the check. One
  `queue --peek --agent <recipient>` settles it in seconds.
- Subagents allowed for discrete tasks (Ash grant 2026-07-29) — every piece
  of delegated work gets a bus task.
- Never run `queue` under another agent's identity (consumption guard,
  v1.7.1); `--peek` for safe foreign reads.

## Session identifiers (non-secret adapter identifiers)

- Bus name: `coord-boss`. Census:
  `team/fulcra/_coord/agents/coord-boss/census.md`.
- Router route: `managed-agents-message` with this session's ref (see
  `_coord/router/config.json`).
