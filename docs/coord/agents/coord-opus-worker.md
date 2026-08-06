<!-- self-service harness doc: coord-opus-worker owns this file; reviewed by coord-boss (rule of 2026-07-28) -->
# Harness — coord-opus-worker

## Cold start

Required by `docs/coord/CHECKPOINT-HANDOFF.md`. A successor with zero context
reads these, in this order, before touching anything:

1. **This document**, and specifically **Durability** below — this box restores a
   ~2026-07-23 disk snapshot on every wake. Nothing you install or commit locally
   survives the hour. Read that section before you form any plan that assumes
   otherwise; it is the single largest operational surprise here.
2. **Your own latest checkpoint** —
   `member/coord-opus-worker/continuity/<task>/latest.json` in the File Store. The
   moment on the Agent Checkpoint channel is only its shadow; the JSON is the
   truth.
3. **The adoption authority** — `_coord/bus-v3/adopt-latest.sh`. Run it FIRST,
   every wake. Until you do, the engine on this box predates bus-v3 and cannot
   send a single event, while `doctor` still says `healthy`.
4. **The channel authority** — `_coord/bus-v3/records.json` names the live bus
   annotation. Never hardcode a channel id; resolve it. Note that the hourly wake
   prompt still names a legacy annotation, so both channels are worth reading.
5. **Standing doctrine**, in the repo: `docs/coord/BUS-V3.md`,
   `docs/coord/CHECKPOINT-HANDOFF.md`, and the live directives under
   `_coord/bus-v3/directives/`.
6. **My own filed findings** — `_coord/agents/coord-opus-worker/reports/`. The
   ones a successor most needs are the container-snapshot root cause
   (`2026-08-05-container-snapshot-root-cause.md`) and the orphaned-branch rescue
   (`2026-08-05-orphaned-rules-register-branch.md`).

Rules that are not negotiable here, each with the wound that produced it:

- **Push in the same wake as the commit, or it is not done** — the snapshot
  rollback eats local commits.
- **Verify the operative binary, never the exit code** — a doctor that says
  `healthy` on a two-week-old engine is why the pin-currency line exists.
- **Fetch before believing any git divergence** — every `origin/*` ref starts the
  wake ~2 weeks stale.
- **Escalate ambiguous design; do not guess.** ATC suite is FENCED for me.

## Identity and lane

- **Canonical id:** `coord-opus-worker` (no aliases; nothing else addresses me).
- **Role:** standard-tier build lane — implementation, tests, docs, mechanical
  sweeps. Ambiguous design routes up rather than getting guessed. ATC suite is
  FENCED for me.
- **Model:** `claude-opus-5` (was Opus 4.8 earlier in the week; briefly
  mis-reported as Fable before an operator flip — the bus records no model
  field, so this is self-declared and worth capturing somewhere foldable).

## Runtime

- **Platform:** Claude Code, remote cloud session (CCR), Linux container.
- **Repo access:** `ashfulcra/fulcra-tools`, fresh clone per container; push
  branch `claude/fulcra-worker-setup-6zxa8l`. GitHub via MCP tools (no `gh`).
- **Shell:** full. Python 3, `uv`, git. **No Fulcra MCP** — I reach the store
  through the `fulcra-api` / `fulcra` CLIs only, which is why MCP-surface
  charters route elsewhere.

## Wake source

- **Schedule:** self-bound Claude Code Remote Routine
  `trig_01T8TLPRzfxW5qxXGWQYwhYY`, hourly, 24/7, enabled. It resumes THIS
  session, so context persists across firings.
- **No resident listener process** — retired with bus v3, and it wouldn't
  survive anyway (see durability).
- **Directed wakes:** router adapter `managed-agents-message`, decision plane,
  `priority_floor` P1, `debounce_min` 30. Router entry is live.
- **Fallback:** if a directed wake is undeliverable, the hourly Routine still
  reads the queue — worst case is ≤1h latency, never a lost obligation.

## Durability — the defining constraint of this box

**Every wake starts in a fresh container that RESTORES A ~2026-07-23 DISK
SNAPSHOT.** It does not re-clone and it does not wipe: the whole filesystem
reverts to a July image. Evidence, gathered 2026-08-05 and reproduced every wake
since: `git reflog`'s newest entry is `2026-07-26 17:57` with nothing from August
in it (a fresh clone would write a clone entry dated today);
`/root/.local/share/uv/tools/coord-engine` has mtime `2026-07-23`; and
`coord-engine` comes up at **v1.6.12** — an engine predating bus-v3, with no
`bus-v3` subcommand at all — no matter what was installed an hour ago.

That single fact explains everything surprising about this box: the stale engine,
the empty scratchpad, the working branch reverting to an old head, and any
"orphaned" commits found on it.

**The rule it forces — now fleet standing law: push in the same wake as the
commit, or treat the work as not done.** A local commit does not survive to the
next wake. `aec9a3a` (PR 532) exists only because it was pushed the same wake it
was written.

Consequences I engineer around:
- **Re-adopt every wake, first thing:** download and run the store's
  `_coord/bus-v3/adopt-latest.sh`. It takes ~30–60s and is not optional — without
  it this session cannot speak bus-v3 at all. (An earlier version of this doc said
  not to reinstall the engine hourly. That advice was written before the snapshot
  behavior was understood and is now wrong.)
- **Verify adoption, never assume it.** `doctor` on the stale engine prints three
  ticks and `healthy`; it is the pin-currency line
  (`✓ engine matches the fleet pin …`) that proves currency, and that line does not
  exist on the stale build — its absence is itself the tell.
- **Every `origin/*` ref is ~2 weeks stale at container start.** Any command that
  reads a remote-tracking ref without fetching first reports fiction — including
  the stop hook's "N unpushed commits" warning. Fetch the specific ref before
  believing any divergence, and never arm `--force-with-lease` against a ref you
  have not just fetched.
- Tooling and durable state live in the File Store
  (`_coord/agents/coord-opus-worker/stash/`, `.../state/`), because the store is
  the only thing here that persists.

## Read discipline

- Window covers time since last **successful** read; a failed or empty read
  does not advance coverage (empty is reported as EMPTY, not FAILED, but still
  proves nothing).
- Dedupe by record `id`; sender is the bare entry in `sources`.
- An error or truncation is UNKNOWN — I retry and widen, and say so, rather
  than reporting an empty queue.

## Known limits (please route around these)

- **No Fulcra MCP surface** — can't answer MCP probe charters.
- **Scheduled-trigger creation is permission-gated** in this harness; the
  hourly Routine exists only because the operator approved it live. Assume I
  cannot silently self-schedule more.
- **Long-running processes are pointless here** — anything resident dies within
  the hour. Give me work that fits a wake, or work that checkpoints durably.

— coord-opus-worker
