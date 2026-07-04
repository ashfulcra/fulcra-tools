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

## Verbs (all `uv tool run coord-engine …`)
```bash
tell      <team> <assignee> <title> [-p P0..P3] [-s summary] [-n next] [--from me]   # direct work
broadcast <team> <title> …                        # assignee '*' — reaches every non-stale agent
remind    <team> <assignee> <when> <title> …      # hidden until WHEN (ISO or 5d/36h/10m)
later     <team> <title> …                        # backlog (@backlog; inbox --all surfaces it)
handoff   <team> <task> --to <agent> [--checkpoint REF] [-n next]   # ATOMIC: one write
inbox     <team> [--agent X] [--json]             # open directives for X, minus X's acks
inbox     <team> --agent X --ack <slug>           # ack: hides it for X, stops re-notify
respond   <team> <slug> --outcome TEXT [-e evidence]   # record a response + close the loop
```

## How the deterministic parts work
- **Inbox fold** (engine): open tasks assigned to you or `*`, minus your acks
  (`_coord/acks/<slug>/<agent-key>.md`, one file per agent — collision-safe key), gated on `not_before`,
  priority-sorted. Served O(1) from the reconcile aggregate (`acked_by` is folded in at reconcile time;
  freshness is bounded by the reconcile cadence).
- **Broadcast completion**: with `fulcra-agent-presence` installed, a `*` directive is complete when every
  non-stale roster agent has acked. Without presence, acking still hides per-agent (documented degradation).
- **Re-notify**: unacked P0/P1 directives keep surfacing (inbox top, digest) until acked — an ack is a
  deliberate act; a mis-fired ack permanently silences that item for you.
- **Handoff is atomic**: the checkpoint ref and the new assignee land in ONE task-file write, so there is
  no window where the work moved but the resume state doesn't exist.
- **Shard-GC**: reconcile prunes ack shards whose task no longer exists (orphan-proofing the ack dir).

## Fail-closed notes
- `respond` records the response shard first, then closes the task (done, evidence = outcome). If the
  close is an illegal transition, the response is still recorded and the failure reported.
- A `remind` with an unparseable WHEN errors — it never creates a directive that fires at the wrong time.
