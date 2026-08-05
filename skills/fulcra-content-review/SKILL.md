---
name: fulcra-content-review
description: "Review prose meant for humans — proposals, docs, posts — before it ships: measured AI-tell sweep, voice matching against the real author, claims discipline, and a parallel multi-lane review protocol. Born from a live editing session (2026-07-25) where the author-agent's tics were counted, not guessed."
homepage: "https://github.com/ashfulcra/fulcra-tools"
license: "MIT"
user-invocable: true
metadata: { "openclaw": { "emoji": "✍️" } }
---

# Fulcra Content Review

For content a human will put their name on. Code review checks that a thing works;
content review checks that a thing persuades, in the owner's voice, without
overclaiming. Every rule here was paid for in one real editing session — the
provenance notes say where.

## 0. Decide point of view FIRST

Whose voice is this? Decide before any line edit — POV drives everything else.
A document that says "we" (the agents), describes its owner in third person, and
credits itself in the colophon is incoherent the moment the owner shares it as
theirs. If the owner's voice: first person, owner's diction, agents described as
tools they used. (Provenance: the Modest Proposal draft failed exactly this way.)

**Pronoun-referent audit.** Once POV is chosen, extract every "we/us/our"
sentence programmatically and check each referent against the choice. One
document, one "we" — a draft that uses "we" for the authors in one sentence
and for the company being addressed in the next reads as sloppy to exactly
the reader it's trying to persuade. Same failure class as everything else on
this list: two names for one thing, and the text trusts the wrong one.
(Provenance: v3 of the same draft shipped 8 referent slips; the owner caught
them in the first few sentences.)

## 1. Claims discipline — before style, always

- **A single measurement is not a rate.** Label it: "first live wake: 55 s",
  never a bare "55 s" in a stat block. One data point presented as steady-state
  is the most common quiet overclaim.
- **Necessary vs sufficient.** When a test proves half a capability, say which
  half. "A scheduled run reached its tools" does not show it will act on
  instructions it finds elsewhere — design the second test instead of rounding
  up. (Provenance: the ChatGPT scheduled-task tests — the owner caught the
  overclaim, and the honest second test then passed cleanly.)
- **Unforgeable evidence beats self-report.** Verify from the system of record
  (a change feed, a timestamped upload), not the actor's account of itself. Put
  a token in the test that exists only on the path under test — and call it
  read-evidence, not "unguessable"; a short hex nonce proves the file was read,
  it is not cryptography.
- **Preserve every leg of the evidence.** Result files prove the actor did the
  work; only the preserved *instructions* prove the work wasn't smuggled in
  with them. Archive the prompt/charter verbatim next to the result, and state
  preconditions a skeptic would ask about (e.g. a one-time human pre-approval
  that let the run act unattended). (Provenance: second-round review caught
  the missing prompt artifact after the test itself had already passed.)
- **Numbers are measured or labeled estimates.** If the colophon claims "every
  number was measured," make that true.
- **Don't infer intent from your own experience of the surface.** "We didn't
  have to change it" does not mean "it wasn't built for this" — ease of use is
  usually evidence of deliberate design, not of accident. Claims about why a
  product is the way it is belong to its owner; check before printing one.
  (Provenance: a draft claimed the platform "wasn't built for agents" because
  the fleet rode it unmodified; the owner's correction was that the
  agent-facing surfaces are heavily worked precisely so models find them easy.)
- **Enumerate, don't name-match, when auditing claims.** Checking a document
  for "the claims I remember making" has unbounded false negatives — walk every
  number, every stat block, every "we proved" systematically. (Same failure
  class as the credential sweep: four corrections because the search was scoped
  by a name the searcher chose.)

## 2. The measured tic sweep — count, then judge

Run `scripts/tic-count.py` (or the inline equivalent) before and after editing.
Judging your own prose by taste fails; counts do not. Thresholds from a real
8,742-word draft that read as AI-written:

| Tell | Found | Target |
|---|---|---|
| Em-dashes per 100 words | 1.0 | < 0.15 |
| "rather than" | 31 | < 8 |
| ", not X" antithesis | 17 | ≤ 3 that earn it |
| "which is the/what/why…" | 13 | < 4 |
| Ceremony ("worth noting/being clear/saying") | 14 | ~0 — cut the ceremony, keep the point |
| "genuinely / precisely / exactly" | 16 | < 5 |
| Aphorism closers on sections | 9 of 11 | ≤ 2 |
| Bold-lead definition lists | 5 sections | ≤ 2 |

Also hunt, unscored: triads ("cheap, standard, always available"), anaphora runs
("…is fine. …is fine. …is fine."), rhetorical questions answered immediately,
every section shaped as "N things," hedging pairs ("X — and honestly, Y").

Do not sand everything flat. A few contrasts and one good image survive; the
tell is density and symmetry, not existence.

## 3. Voice matching — calibrate on verbatim samples

- Collect 5–10 verbatim quotes from the target author (messages, not their
  formal writing). Match diction, sentence length, and stance — do they ask
  questions straight? use contractions? think out loud?
- **Produce rewrite examples, not diagnoses.** "Too formal" is useless; a
  before/after pair is actionable. Every reviewer returns their worst-five as
  rewrites.
- Test: read a rewritten paragraph next to a real quote. If you can tell which
  is which by rhythm alone, keep going.
  (Example pair that set the bar: "We would rather be corrected than
  convincing." → "If any of this is wrong I'd rather hear it now.")

## 4. Reader-assumption check

Name the coldest intended reader, then verify the document survives them:
define the platform before using it, no internal jargon without introduction,
no "as discussed." If the owner says "close allies," check for facts about
third-party products that are fine internally and awkward forwarded.

## 5. Corrections: visible or smoothed is the OWNER'S call

When review finds errors, there are two honest presentations: corrections left
visible (credibility through shown work) or folded silently into a clean design.
Ask the owner; do not default. Design-history sections ("how we got here,
including what we got wrong") are a different thing from correction scars —
they can earn their place when the journey is the argument.

## 6. Parallel review protocol

- **Freeze the draft** at a pinned copy before fan-out; nobody reviews a moving
  target. One synthesis pass after all reviews land.
- **Independent reviewers, no coordination.** Overlap between findings is
  signal (fix without debate where two agree), not wasted effort.
- **Four lanes, worst-first in each:** technical (every claim vs evidence),
  structural (contradictions, drift between summary and body, inventory
  monotony), security/risk (if the content proposes systems), voice/AI-tells
  (lane 2–3 above).
- The author reviews too, but grounded in counts (§2) — an author's taste pass
  over their own prose finds nothing.
- **Reviews of a superseded draft still pay.** When the draft was rewritten
  while reviews were in flight, triage every finding against the *current*
  text before discarding any: some resolve by rewrite, some survive verbatim,
  some survive reworded. Grep the current draft for each contested passage;
  don't trust memory of what the rewrite fixed.
- **Close the loop with a reconciliation note.** After applying fixes, file a
  map of finding → applied / resolved-by-rewrite / declined-with-reason, and
  point the next review round at it. Without it, round N+1 re-flags round N's
  fixes and burns its budget re-litigating.
- **Meter your own fixes.** Edits made *while applying* review feedback
  reintroduce the tics — twice now, consciously avoiding them. Re-run the §2
  meter after reconciliation, not just after the original edit.

## 7. House style for internal documents — simple and direct (operator-set 2026-07-26)

The Modest Proposal cycle ended with the operator choosing the plain
first-person draft over a longer essay version, then cutting it harder. That
choice is the standing style for internal documents. Review against it:

- **Simple and direct.** Short declaratives, first person, thinks out loud.
  If a passage needs re-reading, it is wrong — the operator's word for a
  dense-but-correct block was "incomprehensible," and the fix he accepted was
  five plain sentences in sequence.
- **The last sentence of a block is usually the good one.** ("Really only the
  last sentence is any good.") Find the sentence that earns its place and
  rebuild the block as the shortest path to it. Additions must clarify, not
  decorate: "you can add more if it is clarifying not obtuse."
- **Put the ask right after the argument** that motivates it, not at the end
  of the document. Conclusions don't need restating; one is enough.
- **Cut on sight:** thesis announcements ("X is the argument"), restatements
  of a point a list already made, second conclusions, slogans that echo the
  numbered structure beside them, go-to-market speculation, and flourishes
  that trade accuracy for rhythm (a "three vendors" line that counted wrong
  shipped three review rounds before dying).
- **Keep charm that is true and the owner's.** The incident-track story, the
  operator metaphor, the before/after tapes, "MVP first, awesome later" —
  reviewer taste does not outrank the owner's voice. When a reviewer flags an
  owner-dictated passage as slop, the reconciliation answer is "declined,
  operator ruling," not a compromise rewrite.
- **Sweat absolutes.** "Can't be done at all" → "we couldn't do it." "Means
  migrating" → "can require migrating." Every absolute is a claim someone at
  the receiving company can falsify; write what was observed. And check the
  same claim's OTHER homes — the fix that lands in one section leaves its
  twin behind (two names for one thing, again).
- **Package for shipping.** The browser-tab title, gloss for any acronym less
  universal than MVP, cross-refs re-checked after every structural move.
  Working-copy labels in shipped metadata are a real blocker class.

## Scripts

- `scripts/tic-count.py <file>` — strips HTML/Markdown, counts the §2 table,
  prints per-100-word rates. Run pre- and post-edit; the diff is the edit's
  receipt.
