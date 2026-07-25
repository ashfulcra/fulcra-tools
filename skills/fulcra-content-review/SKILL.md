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
  an unguessable token in the test so the result can only have come from the
  path under test.
- **Numbers are measured or labeled estimates.** If the colophon claims "every
  number was measured," make that true.
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

## Scripts

- `scripts/tic-count.py <file>` — strips HTML/Markdown, counts the §2 table,
  prints per-100-word rates. Run pre- and post-edit; the diff is the edit's
  receipt.
