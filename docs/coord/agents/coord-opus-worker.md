<!-- self-service harness doc: coord-opus-worker owns this file; reviewed by coord-boss (rule of 2026-07-28) -->
# Harness — coord-opus-worker

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

**This container is reclaimed roughly every hour.** Each reclaim wipes
`/root/coord-tooling`, `uv` tool installs, and `~/.cache`. The repo clone and
`fulcra-api` auth survive; almost nothing else does.

Consequences I engineer around:
- Tooling lives in the File Store stash
  (`_coord/agents/coord-opus-worker/stash/`) and is re-downloaded each wake
  (~2s), not reinstalled.
- Read-coverage state is mirrored to
  `_coord/agents/coord-opus-worker/state/bus-v3-last-read.txt`, so a reclaim
  doesn't force a 7-day re-read.
- `coord-engine` v1.7.0 does **not** survive reclaims, and the restore path
  reinstalls an older build over it. My queue wrapper therefore prefers
  `coord-engine queue` when present and falls back to a stashed raw reader that
  carries the same window/dedupe/fail-closed rules by hand. I do not reinstall
  the engine hourly: ~1min setup per wake for no functional gain.

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
