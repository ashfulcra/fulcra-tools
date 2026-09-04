# Re-engagement brief: coord-boss resumes Linear duty

Ash stood coord-boss down from Linear to avoid two writers on one board while
the bridge was being repaired. This is what changed while it was off, and what
it must and must not do on resume.

Paste the section below the line into coord-boss.

---

## Linear duty resumes, under a changed contract

You were stood down from Linear duty to avoid write conflicts while the
`coord-tracker-bridge` lane was repaired. The repair is done and measured. Resume
under these rules; several of them are new, and two reverse what was true before.

### The board is now a projection, not a place work is created

`coord-tracker-bridge` owns every write to the `ash-agent-coordination` BUS team.
One engine row projects to exactly **one** card. Do not create, retitle, relabel,
reassign, or close cards there by hand — the next sync will revert you, and the
revert will look like a bug to whoever reads it.

If a card is wrong, the row is wrong. Fix the row on the bus. The board follows.

### What was actually broken, so you can recognise a relapse

1. **The bridge wrote to a project; Ash reads a saved view.** A Linear saved view
   filters on **labels** — a project is not reachable from one. Cards were
   correctly projected into "Blocked on Ash" for six weeks while his bookmarked
   view stayed empty. The bridge now writes the `blocked-on-ash` label as well.
2. **One row became two cards.** `SourceIdentity` put the engine *fold* in the
   namespace, so a row blocked on a human — which the engine surfaces both as a
   blocked task and as an ask, correctly — projected twice. Measured: 30 cards
   for 15 rows, 12 doubled. Answering one left its twin looking unanswered. The
   source adapter now collapses a row to one card, `asks` winning because it is
   the fold `coord-engine answer` is defined over.
3. **Closing a card left its label on.** His view is set to show completed
   issues, so 12 correctly-closed cards stayed in it. A consumer label is a claim
   about the present and now comes off on close, and converges for cards closed
   earlier.
4. **The reply reader did not match the view.** It read the asks fold while the
   view showed everyone blocked on him, so two replies he left sat unread. A card
   is answerable because it **names a human**, not because of the fold it came
   from.

### The return leg, and its hard limit

Ash replies in a Linear comment. `coord-tracker-bridge linear-answers --deliver`
carries that reply to the bus via `coord-engine answer`, and only that verb. It
cannot create a task, assign an agent, or originate a directive from a comment.

**It can only settle rows the engine holds as waiting-for-operator asks** —
measured as `blocked_on: user:<handle>` **and** `assignee: human`. Rows blocked
on him but assigned to an agent are in his view and are **not answerable**; a
reply there is reported as a refusal, not delivered, and not silently dropped.

At the time of writing that is 3 rows, assigned to coord-boss (2) and
arc-maintainer (1), one of them the 409A row. **If you own a row that is blocked
on Ash, either its assignee should be `human` so he can answer it, or you should
not be modelling it as blocked on him.** Pick one deliberately; leaving it as-is
puts a card in his queue that he cannot clear.

### 409A specifically

The 409A row carries a standing law: *do not re-surface, wait for Ash to raise it
himself*. He raised it himself, in Linear, on 2026-09-03: **"decision made. 409a
in progress."** That reply could not be delivered — the row is assigned to you,
not to `human`, so `coord-engine answer` refuses it. Act on his answer and clear
the row. Passive visibility in a view he opens was always permitted; this is him
speaking first, which discharges the deferral outright.

### Writes carry a bot identity

The bridge writes as the "Coord Bridge" OAuth app, never as Ash. If you ever find
yourself writing to Linear with a personal key, stop: every fleet action would be
authored as *him*, including the cards telling him he is blocked.

### Nothing runs on a cadence

Ash's standing instruction to the Linear agent was **do not tick**. Every sync so
far has been run by hand. If Linear duty now includes a cadence, that is a change
he has to make explicitly — do not add one on your own initiative, and do not
assume the board is fresh because it was correct an hour ago.

### The three numbers that tell you it is healthy

- A second `plan` immediately after a `sync` returns **0 changes**. `rc=0` is
  acceptance, not durability; this is the only proof a sync settled.
- Distinct source rows carrying `blocked-on-ash` **equals** the number of open
  cards carrying it. Any gap is duplication returning.
- `coord-engine asks fulcra --human ash` and the reply reader see the same rows.
  If the view is larger than the reader, replies are going unread again.
