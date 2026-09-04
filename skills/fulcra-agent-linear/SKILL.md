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
  "consumer_label":   "blocked-on-{consumer}",
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
- `consumer_lanes` is **only** about that triage case. A row reaches a person's
  view by naming them, in whatever lane it sits — listing the asks lane here
  just says "an ask with no human named is still somebody's to triage, not an
  ordinary backlog row". Do not read it as the rule that selects the view.

**Set both the project and the label.** They reach different surfaces, and only
one of them reaches a saved view:

| | reaches |
|---|---|
| `consumer_project` | the project page, and grouping in board views |
| `consumer_label` | **saved custom views**, which filter on labels |
| `linear_user` | My Issues, inbox, notifications, mobile |

A Linear saved view filters on labels; **a project is not reachable from one**.
This is not theoretical — an operator kept reporting an empty "blocked on me"
view while the project sat there holding 27 correctly-projected, correctly-
assigned cards. His bookmark was a custom view keyed on the label
`blocked-on-ash`, which the bridge had never written. Two objects, nearly the
same name, and every "it's fixed" report measured the one he wasn't reading.

The label renders from the **handle** (`blocked-on-ash`), not the display name:
a label is an identifier. Ask which URL your consumers actually have open before
believing a view is populated.

A bot token is often forbidden from *creating* labels (`CreateLabel: FORBIDDEN`),
and one uncreatable label fails the whole resource plan. Either create the
per-consumer labels once by hand — the consumer has usually already made theirs,
which is why their view exists — or grant the bot the scope. The triage
destination deliberately has **no** label for this reason: unresolved rows reach
the triage *project*, which is what triage reads.

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

## Where a reply lands

The bare workspace is the assumption, and it has no `answer` verb — its whole
coordination primitive is `member/<name>/inbox/`, where others drop work for a
member to pick up. So on a plain space the return leg **is an inbox drop**:

```
team/<team>/member/<owner>/inbox/<stamp>_linear-bridge_answer-<ask>.md
```

`owner` is why the task document needs that field. It is a different thing from
the consumer: the **consumer** is the human who decides, the **owner** is the
agent who gets unblocked by the decision. An ask naming no owner is refused
rather than delivered to a guessed inbox.

Where coord-engine *is* installed, the same reply goes to `coord-engine answer`
instead and the engine hands the ask back to its owner. Nothing else about the
loop changes — same reader, same state file, same three outcomes.

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

- **The reader must match the view.** Whatever rule decides that a card
  appears in someone's "blocked on me" view must be the same rule that decides
  the bridge reads their reply on it. These were two rules here — the view
  filtered on the person, the reader on the asks fold — so the view held 27
  cards and the reader saw 13, and two replies the operator left sat unread on
  cards he was looking at. A card is answerable because it **names a human**,
  not because of the fold it came from.
- **The view is wider than the verb that settles it.** Matching the reader to
  the view is right, and it means the reader now sees rows your settle-verb has
  no answer for — a directive that names the same human is in their view but is
  not an *ask*. Measured live: 30 cards in the view, 11 the engine would accept.
  So a reply there is **refused**, and a refusal is its own outcome: decided
  before anything is written, so it leaves no mark, does not halt the replies
  behind it, and exits **2** rather than 0. Decide it from a pre-flight *read*
  of what the verb accepts, never by parsing a failure message — and a failed
  read must raise, because an empty set would refuse everything and read as
  "no verb reaches these rows".
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
- **Changing the policy orphans the ledger.** State is filed under a hash of
  the policy document, so editing the policy silently starts a fresh ledger.
  The bridge self-heals it from the provider metadata on the cards, and the
  absence-close gate above is what makes that safe: a healed entry has never
  been *observed*, so nothing gets closed for being missing from the first
  snapshot after the change. Expect one run that rewrites the hash marker on
  every card — a real cost, and the reason a policy edit is not a free change.
- **A crashed run leaves its lease held.** The lease has a 30-minute TTL, which
  is correct for a run that might still be alive and wrong for one you watched
  die. Check whether the recorded owner is still running before clearing it —
  and never clear it because a re-run was inconvenient.

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
