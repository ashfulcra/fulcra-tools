---
name: fulcra-content-integration
description: "Rework a piece of site content (Recipe, Blog post, docs page, landing section) so that Fulcra is integral to achieving that content's own goal rather than bolted on. Use when reviewing, improving, or drafting any fulcradynamics.com content that mentions Fulcra, or when asked whether a piece of content \"sells\" Fulcra well. Verifies every Fulcra claim against delivered artefacts before writing. (Distinct from fulcra-content-review, which covers prose voice/claims discipline.)"
homepage: "https://github.com/ashfulcra/fulcra-tools"
license: "MIT"
user-invocable: true
metadata: { "openclaw": { "emoji": "🧲" } }
---

# fulcra-content-integration — make Fulcra integral, not bolted on

The test of good Fulcra content: **if you deleted every Fulcra mention, would
the piece fail at its own goal?** If it would still work fine, Fulcra is
bolted on and the piece is an advert wearing a tutorial's clothes. The fix is
never "add more Fulcra" — it is rebuilding the piece so Fulcra is the
mechanism by which the reader gets what they came for.

## Inputs (read before rewriting anything)

- `canonical/FULCRA-FOR-AGENTS-2026-08-18.md` — the canonical statement of
  what Fulcra is for agents; the vocabulary and positioning to write in.
- The docs site as **rendered** (not its source).
- `v2-payloads/DOCS-SITE-REVIEW-2026-08-17.md` and
  `v2-payloads/SURFACE-RECHECK-2026-08-17.md` — verified surface facts:
  which endpoints, pages, CLI verbs, and SDK calls actually exist as
  delivered. Newer verification supersedes these files.
- The `fulcradynamics/agent-skills` repo — what agent-facing skills exist
  and what they actually do.

## Procedure

1. **State the content's own goal in one sentence.** What did the reader
   come for? (A recipe: cook the dish / complete the task. A blog post: the
   insight promised by the title.) Everything else serves this sentence.
2. **Inventory Fulcra mentions.** Classify each: *integral* (deleting it
   breaks step 1's goal) or *bolted-on* (deletable with no loss — trailing
   "learn more about Fulcra" paragraphs, feature lists mid-tutorial,
   superlatives with no task behind them).
3. **Rework.** For each bolted-on mention, either (a) restructure so the
   step the reader must take genuinely runs through Fulcra — a real call, a
   real CLI verb, real data the reader can fetch — or (b) cut it. If the
   content's goal genuinely has no Fulcra-shaped step, say so in the review
   instead of forcing one; a clean piece with one honest link outperforms a
   stuffed one.
4. **Verify every Fulcra claim against the delivered artefact** before it
   goes in the draft: run the CLI verb against the installed CLI, hit the
   endpoint with valid auth, load the rendered docs page, run the skill.
   Never verify against a README, a source repo, or a docs page's
   *description* of behavior — the artefact a reader receives is the fact.
   Two evidence patterns are banned outright: unauthenticated endpoint
   probes (auth precedes routing — real and invented paths both 401) and
   raw-HTML string matching (strip tags, then match rendered text).
   A claim that cannot be verified from this container is cut or flagged
   UNVERIFIED — never shipped hedged.
5. **Record before/after.** Keep the original untouched; work on a copy
   (CMS: a new draft item). Produce a short table: each changed claim →
   the artefact it was verified against → how.

## Site mechanics (standing rules)

- CMS work happens on **new draft items**; originals stay untouched until
  the user swaps them deliberately.
- In Cookbook and Blog rich text, headings are **H3**.
- Every content change gets a `CHANGELOG-SITE.md` entry and
  `node ops/changelog-gate.js` must pass before any publish;
  `node ops/publish-gate.js` too. **Staging only — never publish**;
  publishing is always the user's manual action.
- QA anything touched with `qa-rig/visualcheck.js` (desktop and
  `--mobile`) and `qa-rig/linkcheck.js`.
