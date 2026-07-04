---
name: fulcra-agent-operator
description: "The operator loop for a fulcra-agent-teams space: agents surface waiting-for-operator asks with full context, an orchestrating agent pulls and re-surfaces them on its heartbeat so nothing is forgotten, and the operator's answer flows back as one atomic unblock."
homepage: "https://github.com/ashfulcra/fulcra-tools"
license: "MIT"
user-invocable: true
metadata: { "openclaw": { "emoji": "🙋" } }
---

# Fulcra Agent Operator

Enhances [`fulcra-agent-teams`](https://github.com/fulcradynamics/agent-skills). The failure this skill
kills: an agent hits a wall, quietly parks the work, and the operator never finds out — the workstream is
forgotten. Instead: **asks are first-class bus state**, an orchestrator nags on them by age, and the
answer is a single deterministic write that puts the work back in motion.

## The three parties

### 1. Any agent — raising an ask (when you're stuck on the operator)
```bash
uv tool run coord-engine task block <team> <slug> --on-user "<the ask>"
```
Rules for a GOOD ask (this is the part that makes the loop work):
- **Self-contained**: someone reading only the ask text can answer it. Include the options
  ("use vault A or B?"), the default you'd pick, and the consequence of waiting.
- Put longer context in the task body *before* blocking.
- Then **keep working other tasks** — blocking one task parks that workstream, not you.
- Your listener will notify you when the answer arrives (the task returns to your inbox, unblocked).

### 2. The orchestrator — never letting an ask rot (heartbeat/loop duty)
```bash
uv tool run coord-engine asks <team> [--human <handle>] [--json]   # oldest first, with age_hours
```
On every heartbeat: pull `asks --json`, diff against what you last surfaced, and
- surface **new** asks to the operator immediately (notification, chat, digest),
- **re-surface** any ask older than your nag threshold (suggested: 4h work-hours / 24h otherwise —
  age_hours is in the payload precisely so nagging is a pure function of it),
- relay the operator's reply with `answer` (below). Never answer on your own authority — you are a
  courier, not the operator.
`digest <team>` includes the same asks in its blocked-on-you section for the human-readable view; the
**stale** section (active tasks untouched >48h) is the companion detector for workstreams that stopped
*without* asking.

### 3. The operator's answer — one atomic return leg
```bash
uv tool run coord-engine answer <team> <slug> --with "<the answer>"
```
In ONE write: records the answer (`next_action: OPERATOR ANSWER: …` + body note), unblocks
(`blocked → active`), hands the task back to its **owner** (their inbox + listener fire), and strips
`needs:human`. Refuses non-asks (a task that isn't blocked/needs:human) and asks with no owner —
an answer can never land somewhere nobody is listening.

## Why this shape
- The ask/answer state lives **on the bus**, not in any one session's memory — any orchestrator instance,
  on any host, resumes the loop from `asks --json` alone.
- Age-first ordering makes forgetting structurally hard: the oldest ask is always at the top of every
  pull, every digest, until someone answers it.
- The inbound channel is whatever surface the operator is on (chat with an orchestrating agent, or
  answering directly). A phone-reply channel is a known future item — the notification gets attention;
  the answer currently arrives through an agent session.
