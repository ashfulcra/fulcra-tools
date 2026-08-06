# Checkpoint handoff standard

A checkpoint exists so that a SUCCESSOR AGENT WITH ZERO CONTEXT can resume your
duties. A checkpoint that only summarizes your last hour is a diary entry, not a
handoff. This standard is fleet doctrine (Ash, 2026-08-05).

## Two tiers

**Wake snapshot** (every material wake — the checkpoint-on-every-wake directive):
short. `--objective` = state you left, `--next` = one line. Cadence, not handoff.

**Handoff park** (session exit, context nearly full, or any moment you might not
come back from): the FULL form below. If you are unsure which tier applies, write
the full form.

## The handoff form — five sections, all required

Write these into the snapshot fields (`--objective`, `--decision` repeated,
`--next` repeated, `--open-question` repeated, `--artifact` repeated):

1. **objective** — who you are, what the system state IS (proven, not hoped), with
   the evidence one-liner (last acceptance result, current pin, what is healthy).
2. **decisions** — standing law your successor must not re-litigate: authorities
   granted and their EXPIRY, doctrines adopted and why (one clause of why each —
   a rule without its wound gets re-broken). Anything an operator ruled.
3. **next actions** — ordered, concrete, with enough identifiers (slugs, PR
   numbers, exact paths) that each can be started without archaeology.
4. **open questions** — what is genuinely undecided, who owes the answer, and
   what must NOT be re-asked (operator-deferred items, marked as such).
5. **artifacts** — the COLD-START READING LIST, in order: your agent doc, the
   join ceremony doc, the authority documents (adoption pin, channel record, tag
   registry), the live directive/ruling docs, the review register conventions.
   The checkpoint POINTS; the repo and store CARRY. Never inline what a pointer
   can reference — but never point at something that does not exist.

## Agent-doc requirement

Every agent doc under `docs/coord/agents/` MUST carry a "Cold start" section
listing what a successor reads first, so handoff checkpoints can point at one
stable place. If yours lacks it, adding it is part of your next handoff park.

## The test

Before saving a handoff park, ask: "could a fresh agent in my environment, with
only this checkpoint and the artifacts it points to, take over WITHOUT asking the
operator to re-explain anything already ruled?" If no — it is not done.

## Worked example

See `docs/coord/agents/coord-boss.md` (Cold start section) and the 2026-08-05
coord-boss handoff text that seeded this standard.
