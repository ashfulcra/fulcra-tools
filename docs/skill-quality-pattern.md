# The netflix-skill quality pattern — probes-first preambles + tested skill scripts

Extracted from `packages/netflix-skill` (the reference implementation, praised by the operator as
"really good"), for adoption across every skill in this repo. Two reinforcing pieces:

## 1. The re-entrancy probe preamble

Before a skill instructs the agent to DO anything, its SKILL.md opens with an ordered probe table:

| Probe (run in order) | Command | Passes when | If it fails, enter at |
|---|---|---|---|

(see `packages/netflix-skill/skills/fulcra-netflix/SKILL.md` "Where to start" for the live example)

Rules that make it work:
- **Exact commands, exact pass criteria.** "exits 0 and prints valid JSON", "some line's
  `description` is exactly X" — never "check whether the user is set up".
- **Ordered, first-failure wins.** The probes form a prefix of the skill's state machine; the agent
  enters at the first state whose probe fails. A brand-new context and a returning user run the
  same preamble and land in the right place.
- **Probe-less states are declared.** If a state has no CLI-observable evidence (netflix's SHARE
  happens in a web UI), the table says so explicitly instead of inventing a fake probe.
- **Every state re-enterable.** The preamble only works if re-running any state is safe; the skill
  must make that true (netflix: deterministic record ids make re-imports server-side no-ops) and
  must SAY it, so the agent doesn't fear re-entry.

Why it matters: agents wake up mid-journey constantly (new sessions, compaction, handoffs). A skill
without a probe preamble either restarts from zero (annoying, sometimes destructive) or guesses
(worse). The preamble converts "where were we?" from judgment into evidence.

## 2. Bundled-script-as-module testing

Anything executable that a skill bundles (`skills/<name>/scripts/*.py`, install scripts) is loaded
by a tiny package-level `loader` and unit-tested in CI like any library code:

- netflix: `fulcra_netflix.loader.load()` imports the bundled `netflix_import.py`; tests pin the
  wire shapes, deterministic-id formulas, chunking, idempotency, and API failure modes (303s,
  partial-post accounting) with fixtures — no network.
- The wheel bundles the `skills/` dir (PR #290's fix) so the loader works under `--no-editable`
  CI installs too.

Why it matters: SKILL.md prose describing a script WILL drift from the script (this repo caught
prose/engine drift in review at least four times in two days — humans and reviewers catch it late;
CI catches it at commit time).

## Adoption checklist (per skill)

1. SKILL.md opens with a probe table (or a one-line "stateless — no probes" declaration for pure
   reference skills).
2. Probe commands are copy-pasteable and their pass criteria machine-checkable.
3. Every bundled executable has a loader + tests (wire shapes, idempotency, failure modes).
4. A contract test pins any SKILL.md claim that code could falsify (counts, command names,
   flag spellings) — grep-based is fine, cheap beats clever.

## Adoption surface (initial inventory, 2026-07-05)

| Skill tree | Probe preamble | Script tests | Notes |
|---|---|---|---|
| packages/netflix-skill/skills/fulcra-netflix | YES (reference) | YES | the pattern source |
| skills/fulcra-agent-* (11 coord2 skills) | partial — reconcile/health have doctor; most lack an explicit ordered probe table | engine fully tested (213); the automation skill's bash installers have no CI tests | biggest win: probe tables + bats-or-subprocess tests for install-listener.sh / install-heartbeat.sh |
| packages/csv-importer/skills | audit needed | audit needed | |
| packages/media-helpers/skills | audit needed | audit needed | |

Sequencing: one PR per skill tree, dual-reviewed, starting with the coord2 automation skill (its
installers are the highest-risk untested executables — they write launchd jobs).
