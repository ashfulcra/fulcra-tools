---
name: fulcra-agent-linear
description: "Project a fulcra-workspaces space into Linear so the humans it blocks can see and answer it: one 'Blocked on <me>' view per person, assigned to them so it reaches My Issues, and a return leg that carries their reply back to the workspace. One-way projection out, messages back in — the workspace stays authoritative."
homepage: "https://github.com/ashfulcra/fulcra-tools"
license: "MIT"
user-invocable: true
metadata: { "openclaw": { "emoji": "📥" } }
---

# Fulcra Agent Linear

Enhances the [`fulcra-workspaces`](https://github.com/fulcradynamics/agent-skills)
skill with a **Linear visibility bridge**: your agents' work is projected out to
Linear, where the people it is waiting on already work, and their replies are
carried back into the workspace.

The one view it exists to produce is **"everything blocked on me, in one place"** —
per person, on a board they already have open.

## The rule that shapes everything else

> **Coordination stays in the workspace. Linear is a messaging surface.**

Work is never *created* in Linear for agents to pick up. That direction gives you
two sources of truth and no way to adjudicate between them. What crosses is:

```
   workspace ──── projection ────►  Linear   (what is blocked on you)
   workspace ◄─── return leg ─────  Linear   (your reply, delivered to the waiting member)
```

A Linear card is an **envelope**, never the record. An ask is not answered
because a comment exists; it is answered when the workspace says so.

## What a person sees

A Linear project per consumer — `Blocked on Ash`, `Blocked on Liz` — holding
every row that names them, **assigned to them** so it lands in their My Issues,
their inbox and their phone. They reply in a comment. The reply is carried back
to the agent that was waiting, and the card closes on the next sync.

> A project nobody is assigned to is not a view. Measured on a live board:
> 226 of 229 cards unassigned, and the newest thing in the operator's own Linear
> queue was six weeks stale — while the project sat there, correct and unread.

## Setup

### 1. Say who the consumers are

Consumers are configuration, not code. In your policy:

```json
{
  "consumer_project": "Blocked on {consumer}",
  "consumer_lanes": ["asks"],
  "consumers": {
    "ash": { "display": "Ash",  "linear_user": "1fb93548-…" },
    "liz": { "display": "Liz",  "linear_user": "8e21c07a-…" }
  },
  "unassigned_consumer": "someone"
}
```

- **handle → display name** are different things: `ash` is the identity the
  workspace resolves, "Ash" is what a person reads on a board.
- `linear_user` is what makes the card *reach* them. Omit it and they get a view
  with no notifications — a legal choice, not an accident.
- **A Linear user id is per-workspace.** The id from another Linear org names
  nobody in yours and the API rejects it as `INPUT_ERROR`. Read it off a card
  already assigned to that person in the target org.
- `unassigned_consumer` is where rows land when nobody is named. It must not
  collide with a real consumer, or unattributed work piles into their view.

### 2. Mark what is blocked on a person

In a task document:

```yaml
---
type: Task
id: labcorp-portal-re-login-0000dead
title: Labcorp portal re-login + verdict-table sign-off
status: blocked
blocked_on: user:ash          # ← the typed form. This is the whole signal.
owner: collect-maintainer     # ← who gets unblocked by the answer
tags: [needs:human]
---
```

`blocked_on: user:<handle>` is the **only** form that resolves a person. An
untyped block (`blocked_on: ash`, or a bare `needs:human` tag) stays unresolved
and goes to triage — never to a guessed person's view, and its reply is never
attributed. Guessing which human owns a decision is the worst thing this bridge
could do.

### 3. Run the phases

```bash
export LINEAR_API_KEY=…            # a bot actor, not a person (see below)
export LINEAR_TEAM_ID=…

coord-tracker-bridge plan --source teams            # read-only, shows every change
coord-tracker-bridge apply-resources --source teams # creates the per-person projects
coord-tracker-bridge sync --source teams            # applies the projection

coord-tracker-bridge linear-answers --source teams          # preview replies
coord-tracker-bridge linear-answers --source teams --seed   # adopt existing comments
coord-tracker-bridge linear-answers --source teams --deliver
```

`--source teams` reads the bare `fulcra-workspaces` convention. `--source engine`
reads a [coord-engine](../../packages/coord-engine/README.md) fold instead; every
rule below is identical on both.

## Writes carry a bot identity

Use an OAuth app token, never a personal key. A personal key authors every fleet
action as *that human*, so their own name appears on the cards telling them they
are blocked. Verify what you are: query `viewer` and check the actor is an
`@oauthapp.linear.app` account, not a person.

The return leg depends on this too. The bridge comments on the very cards it
reads, so it must be able to recognise and skip its own comments — otherwise it
feeds its own confirmations back into the workspace forever.

## The rules that cost something

Each of these is here because the cheaper version failed in production.

- **`rc=0` is acceptance, not durability.** The only proof a sync settled is a
  second `plan` returning **0 changes**. A sync once reported `applied: 3` and
  the next plan proposed the same three updates forever.
- **An absence-close needs evidence the row was ever there.** "The enumeration
  was complete" is not "this row was deleted". Identity adopted from a tracker
  proves the card exists, not that a source row ever did — and closing on that
  reads adoption as deletion. A first sync once queued 52 live cards for closing
  that way, 41 of them backlog.
- **A cold start refuses to deliver.** With no state, every historical comment
  reads as new and one run replays months of conversation. `--seed` adopts the
  current comments as the baseline and sends nothing.
- **A dispatch has three outcomes.** The transport can settle an answer and then
  fail to report it, so the attempt is recorded *before* it runs and a retry
  announces **POSSIBLE RE-DELIVERY** — not "new", which under-claims, not
  "repeat", which over-claims.
- **A read failure is UNKNOWN, never "nothing to do".** Exit **3** proves
  nothing; **2** is a deliberate refusal; **0** succeeded.
- **The provider rewrites your text.** Linear turns anything host-shaped into a
  markdown link — `projection.py` becomes `[projection.py](<http://projection.py>)` —
  and a naive comparison then finds drift no write can settle. Normalise before
  diffing.
- **Never let the count you showed differ from the count you apply.** `sync`
  re-plans at execution time, correctly; so a plan approved against a degraded
  read is not the plan that runs. Say what can still move.

## What it deliberately cannot do

- Create work in Linear for agents to pick up.
- Create a task, assign an agent, or originate a directive from a comment. The
  return leg's only reach is settling an ask that was already open.
- Answer an ask whose consumer is unresolved, or deliver to an owner it guessed.
- Close a card for being absent when it has never been seen present.

## Related

- [`fulcra-agent-directives`](../fulcra-agent-directives/SKILL.md) — directed work
  and per-agent inboxes inside the workspace.
- [`fulcra-agent-roles`](../fulcra-agent-roles/SKILL.md) — durable roles and
  vacancy escalation, whose alarms this bridge can project.
- [`packages/coord-tracker-bridge`](../../packages/coord-tracker-bridge/README.md)
  — the implementation, its policy schema, and the run phases in detail.
