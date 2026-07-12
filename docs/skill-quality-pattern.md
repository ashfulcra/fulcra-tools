# The netflix-skill quality pattern — probes-first preambles + tested skill scripts

Extracted from `packages/netflix-skill` (the reference implementation), for adoption across every
skill in this repo. Two reinforcing pieces:

## 1. The re-entrancy probe preamble

Before any STATE instructions (framing prose first is fine — netflix leads with its pitch/contract),
the SKILL.md presents an ordered probe table:

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
prose/code drift in review repeatedly within two days — e.g. commits 7f99b2a, e20bf71, 7b70683 —
humans and reviewers catch it late; CI catches it at commit time).

## Adoption checklist (per skill)

1. SKILL.md presents an ordered probe table before any state instructions (or a one-line
   "stateless — no probes" declaration for skills with no cross-session state — pure
   reference skills, or per-invocation idempotent pipelines).
2. Probe commands are copy-pasteable and their pass criteria machine-checkable.
3. Every bundled executable has a loader + tests (wire shapes, idempotency, failure modes).
4. A contract test pins any SKILL.md claim that code could falsify (counts, command names,
   flag spellings) — grep-based is fine, cheap beats clever.

## Adoption surface (inventory, 2026-07-12)

> **References-vs-engine freshness (2026-07-12).** Beyond the SKILL-body probe
> tables (item 4), the coord skills' `references/*.md` command docs are now part of
> the drift surface. This pass re-verified every coord reference command against the
> live engine (`coord-engine <verb> --help`) and corrected the ones that had lagged
> the SKILLs — the atomic `review request` flow (review-cli.md), `intent` + `threads`
> (directives-cli.md), and role dormancy (roles-cli.md). Treat a reference that
> narrates a command the engine no longer exposes (or a superseded flow the SKILL
> forbids) as blocking drift, same as a bad probe verb.


| Skill tree | Probe preamble | Script tests | Notes |
|---|---|---|---|
| packages/netflix-skill/skills/fulcra-netflix | YES (reference) | YES | the pattern source; item 4 landed (tests/test_skill_contract.py) — and caught real drift (--no-verify undocumented) on its first run |
| skills/fulcra-agent-* (12 coord skills) | ALL 12 covered: 11 have ordered probe tables (automation, presence/roles/tasks, continuity/review/directives, reconcile/forge/operator, atc — every probe verb pinned to cli.py by tests/test_skill_probes.py) + health, which is itself the `doctor`/fleet-fold probe surface (no table of its own needed) | engine folds fully unit-tested; automation installers now have CI tests (install-listener.sh / install-heartbeat.sh, PATH-shim harness) | coord adoption complete |
| packages/csv-importer/skills/fulcra-csv | YES — ordered authed→def→dry-run→landed probe table | YES (covered by the package's `tests/` suite: parser/export/confidence) + contract test (tests/test_skill_contract.py) | flag drift clean in both directions (every documented flag is a real click.option and vice-versa); source-id-prefix default pinned |
| packages/media-helpers/skills/fulcra-media | YES — ordered authed→bootstrapped→check-only→watermark probe table, consolidating the pre-existing `--check-only`/heartbeat probe concepts | YES (covered by the package's large `tests/` suite: importers, CLI, dedup, watermarks) + contract test (tests/test_skill_contract.py) | contract test pins importer roster names, state.json keys, and an exact set of import-path envelope stages (webhook's bind/ready/shutdown excluded); flags intentionally delegated to `--help` are held in an explicit UNDOCUMENTED_OK allowlist (mostly per-importer flags, plus 6 that belong to the non-importer `webhook`/`reset` commands) so a new CLI flag forces a document-or-allowlist decision |
| packages/fulcra-prefs/skill/SKILL.md | YES — ordered authed→onboarded→prefs-present→hooks probe grid ABOVE the pre-existing `## Pick your path` capability-tier menu (the two answer different questions: how-far-did-this-user-get vs what-can-my-runtime-do) | YES (the package's `tests/` suite) + probe contract test (tests/test_skill_probes.py) pinning the heading and that every `fulcra-prefs <verb>` in the grid is a real cli.py subcommand | hybrid: probe grid + tier menu coexist; onboarded probe uses `compile` (idempotent), hooks probe greps the managed `fulcra-prefs-hooks` marker |
| packages/fulcra-vault/skill/SKILL.md | YES — ordered authed→initialized→content-present→hooks probe grid ABOVE `## Pick your path` | YES (the package's `tests/` suite) + probe contract test (tests/test_skill_probes.py) | hybrid, same shape as prefs; initialized probe uses `map --check` (read-only, exit 2 on missing `/vault/meta.json`), hooks probe greps `fulcra-vault-hooks` |
| skills/fulcra-fde | YES — ordered authed→engine→engagements→resume probe grid; engagements are durable server-side state, so the grid routes a fresh session into `resume`/`status` for the right phase | tests/test_skill_probes.py (fde-engine package) pins the probe heading + every verb, phase names, and references | stateful (7-phase engagement machine) |
| skills/fulcra-lab-results | N/A — sanctioned **stateless — no probes** declaration | fulcra-labs engine unit tests | per-invocation idempotent pipeline: no cross-session journey to resume; intermediate pass_a/pass_b/agreed JSON is ephemeral scratch, an interrupted run restarts from Intake on the same PDF |

Sequencing: one PR per skill tree, dual-reviewed, starting with the coord automation skill (its
installers are the highest-risk untested executables — they write launchd jobs).
