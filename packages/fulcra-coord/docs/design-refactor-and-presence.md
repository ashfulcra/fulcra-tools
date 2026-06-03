# Design: agent presence (workstream-on-connect) + structural cleanup

**Date:** 2026-06-03
**North star:** the human's situational awareness — *what every agent is doing* and *what's blocked on me*.

This design covers two things the clearer requirements now justify:

1. **Agent presence** — agents report their current major workstream(s) on connect, so `agents` / `status` / `resume` / the SessionStart banner show what each agent is working on **even when it has no active coordination task** (the exact failure mode: "I context-switch and forget a session churning in the background").
2. **Structural cleanup** — pay down the debt the codebase accumulated while the requirements were still moving (cli.py god-object, duplicated write/install boilerplate, the `--agent` inconsistency, scattered status literals).

---

## 1. Requirements

### Functional
- **F1 (presence):** On connect, an agent records a durable presence entry: who it is, its current major workstream(s), a one-line "what I'm on," and a `last_seen` timestamp.
- **F2 (no-task case):** Presence shows even when the agent owns zero active tasks — that's the whole point.
- **F3 (surface):** `agents` leads with a presence roster (agent · workstreams · last-seen · live/idle/stale); `status`, `resume`, and the SessionStart banner reuse it.
- **F4 (declarable + derived):** Workstreams can be declared explicitly (`fulcra-coord workstream …`) and are auto-seeded from the agent's active tasks' `workstream` fields so the common case needs no extra step.
- **F5 (subsumes the manual convention):** Replaces the ad-hoc "joined. Focus: X" broadcasts agents do today with one structured record.

### Non-functional / constraints
- **Stdlib-only**, Fulcra Files as the only bus (CLI file ops), each write is a network round-trip → **minimize writes on hot paths**.
- **Best-effort:** presence must never break or slow a task op (same rule as annotations).
- **Backward compatible:** old agents (arc/codex/openclaw already on the bus) and old task/view files keep working; presence is additive.
- **Concurrency-safe:** many agents update presence at once without clobbering each other.
- **CI stays green + hermetic.**

---

## 2. High-level design — presence

Presence follows the **same pattern the codebase already uses for tasks → views**: per-entity files that a periodic `reconcile` rolls up into one aggregate the read commands consume.

```
/coordination/
  presence/<agent-slug>.json     ← per-agent record. ONLY that agent writes it →
                                    zero cross-agent write contention.
  views/presence.json            ← aggregate roster {slug: record}. Rebuilt by
                                    reconcile + refreshed opportunistically on
                                    connect. ONE read for the `agents` digest.
```

**Record shape** (`presence/<slug>.json`):
```json
{
  "schema": "fulcra.coordination.presence.v1",
  "agent": "claude-code:DeskbookPro:vercel",
  "workstreams": ["fulcra-litellm", "hermes-vercel"],
  "summary": "Fly deploy blocked on payment method",
  "last_seen": "2026-06-03T18:22:11Z",
  "session": "<opaque session key, optional>"
}
```

**Data flow:**
```
SessionStart hook ──► fulcra-coord connect           (writes presence/<slug>.json,
   (on connect)        --workstream <ws> [--summary]   merges self into views/presence.json)
                              │
fulcra-coord workstream ──────┘  (explicit declare/clear, same writer)
                              │
reconcile / build_all_views ──► rebuilds views/presence.json from presence/*.json
                              │   + marks each entry live / idle / stale by last_seen age
                              ▼
agents · status · resume · SessionStart banner ──► read views/presence.json (1 read)
```

**Liveness** reuses the existing stale model: `live` (< idle threshold), `idle` (between), `stale` (> `FULCRA_COORD_STALE_HOURS`). So the roster distinguishes "actively working," "quiet," and "probably crashed."

**Why a single aggregate read for `agents`:** the digest is human-invoked but should stay one round-trip. Per-agent files keep writes contention-free; the aggregate keeps reads cheap. Staleness between reconciles is acceptable and the connecting agent refreshes its own entry in the aggregate via the same merge logic tasks use (disjoint keys → trivial union, newest `last_seen` wins).

### New CLI surface
- `connect --workstream <ws>[,<ws>] [--summary "…"] [--agent …]` — idempotent; the SessionStart/Codex hooks call it. Auto-adds workstreams from the agent's active tasks.
- `workstream [set <ws>[,…] | add <ws> | clear] [--summary "…"]` — manual declare/update.
- `agents` gains a presence roster header; `presence [--format json]` for the raw roster (tooling/banner).

---

## 3. Deep dive — module placement

| Concern | Home | Notes |
|---|---|---|
| presence record schema + merge | `schema.py` (`make_presence`, `merge_presence`) | mirrors `make_task` / `_try_merge`; key-union merge is simpler than task merge |
| aggregate roster build + liveness | `views.py` (`build_presence`) | reuses `is_stale` age logic |
| remote paths | `remote.py` (`presence_remote_path`, `presence_view_path`) | mirrors `agent_remote_path` |
| `connect` / `workstream` / `presence` handlers | new `view_commands`/`presence` home (see §4) | call a best-effort `_write_presence` like `_stamp_session_pointer` |
| hook wiring | `claude_code.py` SESSION_START_SH + codex template | add one `connect` call after identity resolves |

---

## 4. Structural cleanup (the refactor)

Evidence from the audit (file:line counts):

| Debt | Evidence | Fix |
|---|---|---|
| **cli.py god-object** | 1956 LoC, 27 handlers + 15 helpers, 5 domains | Split into `task_commands` / `directive_commands` / `view_commands` / `install_commands` / `identity_commands` / `_helpers`; `entry.COMMAND_MAP` unchanged |
| **task write boilerplate** | load→null-check→cache→write→except repeated **7–9×** (cli.py 967, 1024, 1061, 1411, 1484, 1558, 1618, 1668, 1711) | Extract `_apply_task_and_write(task_id, transform_fn, command)` |
| **install boilerplate** | 6 install-* handlers ~90% identical (cli.py 564–642) | Extract `_handle_install(plan, …)` |
| **`--agent` inconsistency** | `cmd_start` *requires* `--agent`; 8 sibling write-commands auto-resolve via `resolve_agent` | Make `start` auto-resolve too (keep `--agent` as override) — also fixes the start ergonomics |
| **scattered status literals** | "active"/"waiting"/… as literals ~23× across cli/views | Use `schema` status constants everywhere |
| **single-use helper in cli** | `_age_str` only used by `needs-me` | move to a shared time helper |

**Trade-off — split aggressiveness:** the cli.py split is the highest-LoC, highest-churn change and lands in a monorepo with concurrent sessions (merge-conflict risk). Its user value is indirect (maintainability), where presence is direct (the north star). So the split is **optional/stageable** and called out as a separate decision below.

---

## 5. Scale & reliability
- **Writes added:** one presence write per *connect* (not per op) + the opportunistic aggregate merge. Negligible vs. task ops.
- **Failure mode:** presence write fails → task ops unaffected (best-effort); the roster just shows an older `last_seen`. Reconcile heals it.
- **Backward compat:** absent `presence/` dir → empty roster, `agents` behaves exactly as today. Old agents never see new files.
- **Revisit as it grows:** if agent count makes the per-agent files expensive to roll up, move the aggregate to an append-only or sharded form; if presence needs to be real-time, shorten the listener tick to refresh `last_seen`.

---

## 6. Trade-off summary
- **Presence as a new bus artifact** (chosen) vs. derived-only from tasks: derived is zero-state but can't show an agent with no task — which is the requirement. Presence adds one connect-time write and one new dir; additive and backward-compatible.
- **Per-agent files + aggregate** (chosen) vs. single shared `presence.json`: per-agent avoids write contention; the aggregate keeps the digest one read. Cost: brief staleness between reconciles (acceptable).
- **Full cli.py split** vs. **targeted dedupe only**: full split maximizes maintainability but is churn-heavy and conflict-prone right now; targeted dedupe (helpers + `--agent` fix + constants) captures most of the value with a fraction of the blast radius.

---

## 7. PERFORMANCE — the primary refactor goal

The operator flagged the CLI as slow ("this is getting slow… reduce overhead should be a focus"). Measured round-trip analysis of the hot paths (each round-trip = one `fulcra` CLI subprocess to Fulcra Files, ~0.5–1s):

### Where the time goes (counted from source)
**Every WRITE** (`start`/`update`/`block`/`tell`/`broadcast`/`done`/`assign`) calls `_write_task_and_views`, which does, **sequentially**:
1. pre-stat task (1) → upload task (1) → post-stat (1) ≈ 3
2. **`_load_all_tasks()` — and this re-fetches EVERY task body one file at a time**: `download index` + `search-index` + `next` (3) **+ one `_cache_remote_task` per task (N)**
3. **upload every view, sequentially** (~8 + per-workstream + per-agent + per-inbox) ≈ 8–15

→ **≈ N + 15 sequential round-trips per write.** At N≈30 tasks that's ~45 × ~0.7s ≈ **30s per write** — matching observed `broadcast`/`block` latency.

**Every READ** (`needs-me`/`resume`/`agents`/`status`/`search`) also calls `_load_all_tasks()` → **N + 3** round-trips → ~20–40s, matching observed `needs-me`.

### Root causes
1. **`_load_all_tasks` fetches full task *bodies* one-by-one** — but the read commands and the view rebuild only ever use *summary* fields (`task_summary`: id/title/status/priority/workstream/owner_agent/assignee/blocked_on/next_action/tags/updated_at). The bodies are fetched and thrown away.
2. **View uploads are sequential** — independent network waits run one after another.
3. The write path rebuilds views from full bodies it didn't need.

### The fixes (highest ROI first, by risk)

**P1 — Parallelize view uploads (safe, big).** Upload the ~8–15 independent view files with a stdlib `concurrent.futures.ThreadPoolExecutor` (each upload is its own subprocess; threads overlap the network waits). No semantic change. Turns the upload phase from O(#views) sequential into ~1 batch. **~8–15× faster upload phase.**

**P2 — Read commands stop fetching task bodies (big).** Introduce one authoritative summary aggregate `views/summaries.json` = `{id: task_summary(t)}` for all live tasks (every field the view builders + `needs_human`/`agents`/`resume`/`status`/`search` read is already in `task_summary`). Read commands consume it in **1 read** instead of N+3. New helper `_load_task_summaries()` replaces `_load_all_tasks()` on read paths. `_load_all_tasks` (full bodies) is kept ONLY for the merge of the one task being written.

**P3 — Write path rebuilds views from summaries, not bodies (big, higher care).** `build_all_views` already only consumes `task_summary` fields. Feed it the summaries aggregate (1 read) + the freshly-written task's own summary, instead of `_load_all_tasks()` (N fetches). This removes the N-fetch from the write path. Guard the "fresh machine / truncated set" correctness case the current code calls out by sourcing summaries from the durable `summaries.json` (authoritative, not local cache). Parallelize the per-task summary refresh if any body fetch remains.

**Combined target:** write ≈ 3 (task) + 1 (summaries read) + 1 (parallel view batch) ≈ **~5 round-trips, mostly parallel** (from ~45). Read ≈ **1** (from ~N+3). I.e. seconds, not tens of seconds.

### Presence stays cheap
Presence is one extra small write on *connect* (not per op) and the aggregate `views/presence.json` is built in the same parallel batch as the other views — so it adds ~0 to steady-state latency.

### Reliability / correctness notes
- Parallel uploads: a partial failure still flags `needs_reconcile` (collect per-future results; any failure → same path as today).
- `summaries.json` is itself a view rebuilt every write, so it self-heals via `reconcile`; absence → fall back to `_load_all_tasks` (correctness over speed).
- Backward compatible: older agents that don't write `summaries.json` still get it rebuilt by any newer agent's write or by `reconcile`.

### Sequencing (subagent-driven, TDD)
1. P1 parallel uploads (isolated, measurable) → 2. summaries.json view + `_load_task_summaries` → 3. migrate read commands → 4. write path uses summaries → 5. presence feature on top → 6. targeted dedupe (`_apply_task_and_write`, `_handle_install`, `--agent` on `start`, status constants) folded in where it touches the same code. Measure write+read latency before/after on the live bus.
