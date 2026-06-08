---
name: fulcra-coord
description: Use when coordinating durable work across multiple agent sessions or hosts — handing off a task between sessions, telling another agent to do something, surfacing a blocker to the human operator, checking what other agents are doing, or resuming after a restart/compaction. Use when you see a `/coordination` Fulcra Files bus or a `fulcra-coord` CLI.
---

# fulcra-coord

## Overview

`fulcra-coord` coordinates durable work across independent agents (Claude Code, Codex, OpenClaw, Hermes, cloud, CI) using **Fulcra Files as the only shared store** — no shared memory, direct calls, or central broker. It is **runtime-agnostic**: every command below behaves identically on any platform that has the CLI + Fulcra credentials. Tasks are JSON on a `/coordination` bus; agents read cheap **materialized views**, write under optimistic concurrency, and a reconcile heartbeat repairs partial writes.

**Core principle:** the bus is the source of truth and the operator's situational awareness. Write durable state at task boundaries so any session — or the human — can pick up cold.

## When to use

- Handing work off between sessions / after a restart or compaction (`resume`, `pause`, `snapshot`).
- Directing another agent (`tell`) or all agents (`broadcast`); reading directives sent to you (`inbox`).
- Surfacing something **only the human can do** (`block --on-user`) so it lands on their plate (`needs-me`).
- Seeing what every agent is doing / who's blocked (`status`, `agents`, `health`).

**When NOT to use:** one-shot answers, internal tool steps, or work that won't outlive the current turn. Don't write coordination updates for those.

## Setup (do this first)

```bash
fulcra-coord doctor   # verifies CLI, auth, file commands, identity
```
If `File commands: FAIL`, the bus is dead silent: the public PyPI `fulcra-api` lacks the `file` group. Install a file-capable build (see `docs/fulcra-cli-branch.md`). This is the #1 fresh-agent failure.

## Quick reference

| Need | Command |
|---|---|
| What's happening on the bus | `status` · `agents` · `health` |
| Pick up after restart | `resume [--with-continuity]` |
| Start / progress / finish | `start` · `update` · `done --evidence … --verification-level …` |
| Pause or checkpoint | `pause --next … [--snapshot]` · `snapshot --reason …` |
| Direct another agent / everyone | `tell <agent> "…"` · `broadcast "…"` |
| Read work sent to you | `inbox [--ack <id>]` |
| **Block on the human** | `block <id> --on-user "<ask>"` |
| **What's on my (human) plate** | `needs-me` |
| Block on an agent/external | `block <id> --blocked-on "…"` |
| Repair stale views | `reconcile` |

Run any command with `--help` for flags; `--format json` is available on read commands.

## Load-bearing rules (these bite agents who skip them)

- **Declare identity, scoped per directory:** `identity set vendor:host:purpose` (e.g. `claude-code:DeskbookPro:fulcra-tools`). Identity is per-cwd — sibling sessions in different repos must not share one, or they clobber each other.
- **One git worktree per session**, not a shared checkout: `git worktree add ../<repo>-<purpose> -b <branch> origin/main`. Concurrent sessions in one checkout corrupt each other's index/HEAD.
- **Never push to `main`; every change goes through a PR reviewed by a *different* agent identity.** A clean approval is merged by whoever's around; never merge your own unreviewed code. The review handshake lives **on the bus** (`tell` your reviewer), because co-located agents often share one GitHub account so GitHub "Approve" can no-op.
- **Provide `evidence` on `done` and a `next_action` on `pause`/`block`** — that's the handoff note the next session reads.
- **A request like "handle my todos" authorizes reading the list, not executing it** — surface side-effectful items, don't auto-run them.

## Key concepts

- **Materialized views:** reads (`status`/`agents`/`resume`/`needs-me`) hit pre-built summaries, not full history — fast and cheap. If they look stale, `reconcile`.
- **Reconcile heartbeat:** a scheduled `reconcile` (launchd/cron) sweeps stale `active` tasks and rebuilds views for crashed / end-hook-less agents. `install-heartbeat`.
- **Listener:** a per-agent `notify-inbox` poll surfaces directed work while you're idle and self-heals at SessionStart. `install-listener` (or `ensure-codex-watch` for Codex).
- **needs-me / block --on-user:** the operator-facing surface — the one channel that puts an ask on the human's plate and leads their next SessionStart.

## Works on any runtime

The core needs **only the `fulcra-coord` CLI + Fulcra credentials** — it runs identically on Claude Code, Codex, OpenClaw, Hermes, ChatGPT/cloud, or CI. The commands and rules above are all any runtime needs. **Adapters are optional lifecycle sugar** (auto-surface work at session start, checkpoint on compaction, park on exit) for the runtimes that have one; without an adapter, install the durable pickup path yourself with `install-heartbeat` + `install-listener` (a scheduled `reconcile` + `notify-inbox`).

| Runtime | Lifecycle adapter | Arm coordination |
|---|---|---|
| Claude Code | `adapters/claude-code/CLAUDE.md` + `ONBOARD.md` | `install-claude-code` |
| Codex | `adapters/codex/AGENTS.md` | `ensure-codex-watch` |
| OpenClaw | `adapters/openclaw/SKILL.md` | `install-openclaw --with-heartbeat --with-listener` |
| Hermes / ChatGPT / other cloud | `adapters/generic-cloud-agent.md` (+ `adapters/chatgpt/INSTRUCTIONS.md`) — no dedicated hook adapter yet | `install-heartbeat` + `install-listener` |

Protocol, schema, auth, and remote layout: `docs/protocol.md`, `docs/schema.md`, `docs/auth.md`, and the README "Remote layout" section.
