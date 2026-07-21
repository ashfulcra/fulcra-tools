---
name: fulcra-agent-directives
description: "Directed work for a fulcra-agent-teams space: tell an agent (or broadcast to all), schedule reminders, capture backlog, hand off with a checkpoint, and track a per-agent inbox with acks and re-notify — all deterministic."
homepage: "https://github.com/ashfulcra/fulcra-tools"
license: "MIT"
user-invocable: true
metadata: { "openclaw": { "emoji": "📨" } }
---

# Fulcra Agent Directives

Enhances [`fulcra-agent-teams`](https://github.com/fulcradynamics/agent-skills). Teams' native inbox is a
drop-zone of markdown files; this skill adds **structured directed work**: a directive IS a task with an
`assignee`, so it shows up in the reconcile views, carries priority + status machine, and has a
deterministic per-agent **inbox** with **acks** (acking hides an item for you and stops re-notify) —
without replacing the teams inbox for freeform messages.

## Where to start — the re-entrancy probes

On waking (or before directing more work), probe your own inbox — what is already assigned to you and
unacked. Enter at the **first probe that fails** (per the repo's skill-quality pattern,
`docs/skill-quality-pattern.md`); reading the inbox is a pure fold and an ack is an idempotent
single-file write (acking an already-acked slug just rewrites the same shard), so re-entry is always
safe:

| Probe (run in order) | Command | Passes when | If it fails, enter at |
|---|---|---|---|
| Engine + auth usable? | `coord-engine doctor <team>` | exits 0 and the last line is exactly `doctor: healthy` | fix engine/auth first (see fulcra-agent-reconcile) — do NOT direct/ack against a broken engine |
| Inbox clear for me? | `coord-engine inbox <team> -a <id>` | the header line ends `0 item(s)` (the fold found nothing open + unacked for you) — NON-mutating read | **Work your inbox** — the header names a non-zero count and each following line is an open directive; act on / ack it (`inbox <team> --agent <id> --ack <slug>`) via [Verbs](#verbs) before directing new work |

Inbox clear → nothing is assigned to you and unacked; proceed to `tell` / `broadcast` / `remind` others,
or capture backlog with `later`. Add `--all` only for audit/debugging: it restores
acknowledged, closed, future, and `@backlog` history.

## Verbs

All verbs below run as `coord-engine <verb> …`:

```bash
tell      <team> <assignee> <title> [-p P0..P3] [-s summary] [-n next] [--from me]   # direct work
broadcast <team> <title> …                        # assignee '*' — reaches every non-stale agent
remind    <team> <assignee> <when> <title> …      # hidden until WHEN (ISO or 5d/36h/10m)
later     <team> <title> …                        # backlog (@backlog; inbox --all surfaces it)
intent    <team> "<text>" --for ash [--by <when>]  # capture a spoken commitment (intent:ash item)
inbox     <team> [--agent X] [--json]             # actionable directives for X
inbox     <team> --agent X --all [--json]          # full directed history
inbox     <team> --agent X --ack <slug>           # ack: hides it for X, stops re-notify
respond   <team> <slug> --outcome TEXT [-e evidence]   # record a response + close the loop
threads   <team> --for <principal> [--json]       # FOLD: dropped work-in-progress for a principal
```

`threads` is a read-only **fold**, not a directive — it surfaces started-then-silent /
blocked-on-principal / intent-never-started items the intent-capture doctrine leans on.
Flags, modes, windows, and the `threads-degraded` row: see the [CLI reference](references/directives-cli.md).

## How the deterministic parts work
- **Inbox fold** (engine): open tasks assigned to you or `*`, minus your acks
  (`_coord/acks/<slug>/<agent-key>.md`, one file per agent — collision-safe key), gated on `not_before`,
  priority-sorted. `needs-me` and `briefing` apply the same satisfaction rule,
  so an acknowledged directive cannot linger in one queue after leaving another.
  Served O(1) from the reconcile aggregate (`acked_by` is folded in at reconcile time;
  freshness is bounded by the reconcile cadence).
- **Broadcast completion**: with `fulcra-agent-presence` installed, a `*` directive is complete when every
  non-stale roster agent has acked. Without presence, acking still hides per-agent (documented degradation).
- **Re-notify**: unacked P0/P1 directives keep surfacing (inbox top, digest) until acked — an ack is a
  deliberate act; a mis-fired ack permanently silences that item for you.
- **Handoff is atomic**: the checkpoint ref and the new assignee land in ONE task-file write, so there is
  no window where the work moved but the resume state doesn't exist.
- **`intent` — the spoken-commitment member of this family, with the capture doctrine.** When Ash states an
  intent to ANY agent ("later today", "I'll enumerate that list"), that agent files it in the SAME turn with
  `intent` — an uncaptured commitment is the drop nobody can see; the `coord-engine threads <team> --for ash`
  fold (dropped work-in-progress) only surfaces what was recorded. `intent` writes an ordinary directive
  (`intent:ash` tag + `assignee`
  + `intent_by` window) through the same hash-slug delivery + read-back as `tell`, with ONE deliberate
  identity deviation: **identity is text + assignee only — `--by` is NOT part of the slug.** So restatement
  is well-defined and never forks a second item:
  - *identical* (same text, no `--by` or the same window) → pure dedup, rc 0 `intent already captured`;
  - *new `--by`* → the SAME commitment with a revised deadline → a verified in-place window update on the
    existing doc (rewrite `intent_by` → read-back-confirm the new window landed; unverifiable → rc 1, retry,
    never a silently-stale deadline);
  - a *relative* `--by` (`5d`/`36h`/`10m`) re-resolves from now on each restatement, so re-stating "by end of
    day" pushes the window forward rather than pinning the first resolution.
- **Shard-GC**: reconcile prunes ack shards whose task no longer exists (orphan-proofing the ack dir).

## Fail-closed notes
- `respond` resolves the directive **first**: a name that maps to no directive doc (a display title
  instead of the hash-suffixed slug, or an unreadable read) fails **rc-1** and records nothing — no ghost
  shard under a slug nobody owns while the real directive stays open. A resolved directive records the
  response shard, then closes the task (done, evidence = outcome); if the close is an illegal transition
  the response is still recorded and the failure reported.
- A `remind` with an unparseable WHEN errors — it never creates a directive that fires at the wrong time.
