# Docs QA — nightly sweep 2026-07-30

Base: `origin/main` @ `a7b637d`. Engine ground truth: `coord_engine.__version__` = **1.10.0**;
current release v1.10.0. Remote coord-engine tags re-verified via `git ls-remote` during review:
`coord-engine-v1.8.0`/`v1.9.0`/`v1.10.0` all landed mid-review (at sweep time tags stopped at
v1.7.2), so the tag-pin install commands work as written; the store BOOTSTRAP
(`team/fulcra/_coord/bus-v3/adopt-latest.sh` + `BOOTSTRAP.md`) remains the live pin authority.

## Findings

| # | File | Finding | Status |
|---|---|---|---|
| 1 | `AGENTS.md` (quick-ref table) | Anchor link `#ci-the-pre-push-hook-and-workspace-membership` pointed at a heading renamed to "CI and workspace membership" — dead same-file anchor | fixed |
| 2 | `README.md`, `docs/coord/GET-ON-THE-BUS.md` (×2 blocks), `skills/fulcra-agent-atc/SKILL.md`, `packages/coord-engine/README.md` | Install pins reference `@coord-engine-v1.10.0` — pins verified against the store authority; per-doc notes point at BOOTSTRAP as the live pin home. (Round 2: the v1.10.0 tag landed mid-review and the authority advanced to the s3 merge — the interim 'tag pending' workaround notes were removed as already-false.) | fixed |
| 3 | `docs/coord/GET-ON-THE-BUS.md` (sandbox fallback recipe) | At sweep time `git clone --depth 1 --branch coord-engine-v1.10.0 …` failed because the tag was unpushed; an interim fetch-by-SHA comment was added. The tag landed mid-review, making the clone work as written — the interim comment was removed (already-false) | fixed (superseded) |
| 4 | `packages/netflix-skill/skills/fulcra-netflix/references/auth.md` | Instructed "Always invoke the CLI as `uv tool run fulcra-api`" and used that form in every command — the repo-wide anti-pattern (canon: installed binaries only; cf. AGENTS.md:191, GET-ON-THE-BUS, coord-DESIGN). Rewrote preconditions to `uv tool install fulcra-api` + installed binary (abs path via `uv tool dir --bin` as the PATH fallback); replaced all invocations | fixed |
| 5 | `packages/netflix-skill/skills/fulcra-netflix/SKILL.md` | Same anti-pattern in the probe table and AUTH state (9 occurrences); also added the `uv tool install fulcra-api` step to the probe-first instruction | fixed |
| 6 | `packages/netflix-skill/README.md` | Doc's `uv tool run` mention describes the *script's actual code fallback* (`netflix_import.py:get_token()` really shells `uv tool run fulcra-api` when the binary is off PATH) — kept the accurate description, marked it as a code-level last resort not a doc-sanctioned invocation. Changing the script itself is out of scope for a docs-only run | fixed (doc); script behavior deferred |
| 7 | `skills/fulcra-agent-reconcile/references/reconcile-cli.md` | `FULCRA_CLI_COMMAND` example was `uv tool run fulcra-api` — the anti-pattern as the suggested override value. Now suggests an absolute installed-binary path and warns off `uv tool run` | fixed |
| 8 | `skills/fulcra-agent-continuity/SKILL.md` (harness table) | Codex/OpenClaw installer paths `scripts/codex/install_codex_watch.py` and `scripts/openclaw/install_openclaw.py` resolve to nothing — they live under `skills/fulcra-agent-automation/scripts/…` (the Claude Code row in the same table already qualifies the skill). Qualified both | fixed |
| 9 | `README.md` (package table, Vault row) | "keep prose memory" — durable-state noun canon violation ("context", not "memory", in positioning prose); the Vault's own README already says "durable context". Now "keep durable context in prose" | fixed |
| 10 | `docs/coord/GET-ON-THE-BUS.md` §6 | rc-semantics currency: "treat exit 3 as DEGRADED (window unknown)" described the pre-v1.10.0 world (rc 3 = only a degraded window). v1.10.0: rc 3 is UNKNOWN *or* INVALID (non-retryable corrupt bytes), and every nonzero exit under `--json` prints one `queue-error` envelope (state `UNKNOWN\|INVALID\|INCOMPATIBLE\|ABSENT\|REFUSED` + `error_code`), plain-mode diagnostic on stderr. Updated with pointer to BUS-V3 | fixed |
| 11 | `skills/fulcra-agent-presence/references/presence-cli.md` | Missing v1.8.0-era engagement surface: `presence beat --engagement resident\|session\|occasional [--until ISO]` and the fourth liveness band (`lapsed`, time-dirty `now >= until` for session shards) — the engine help and AGENTS.md both carry them. Added both | fixed |
| 12 | `docs/coord/agents/coord-boss.md:30` | "(rc 3 = DEGRADED window, fail closed)" — same shorthand as #10 but still *accurate* for the case it names (degraded window ⇒ UNKNOWN ⇒ rc 3), in an agent census doc, not a queue-semantics reference | judgment — left |
| 13 | `README.md:159`, `FULCRA-PRIMITIVES.md:345`, `docs/collect.md:83` | `uvx fulcra-context-mcp@latest` — literal `uvx` for a Fulcra tool, but it is the upstream-published PyPI MCP server and `uvx` is the standard MCP stdio launch convention; "fixing" it would contradict upstream docs | judgment — left |
| 14 | `packages/fulcra-vault/README.md:202` | "durable *context and memory*" retains "memory" alongside the canon noun in a classification sentence | judgment — left |
| 15 | `packages/netflix-skill/docs/design.md` | `uv tool run fulcra-api` ×3 — outside sweep scope (`packages/*/docs/` is not `README.md` or `skills/`), and it is a historical design doc | deferred (out of scope) |
| 16 | `scripts/primitives-maintainer/README.md` | Long pinned-prose file with `uvx` usage (for the *published* PyPI package probe — arguably legitimate) — outside sweep scope (`scripts/`) | deferred (out of scope) |
| 17 | `docs/fulcra-coord-0.13.0-rollout.md:267` | References `docs/superpowers/specs/2026-06-09-read-cutover-flip-readiness.md` which does not exist in this repo — but the doc itself hedges "if present; the inline summary stands on its own", and it is a historical rollout doc for the retired fulcra-coord | judgment — left |
| 18 | `docs/proposals/2026-06-22-reconcile-performance.md` | References `packages/fulcra-coord/CHANGELOG.md` (package no longer in repo) — historical proposal for the retired predecessor | judgment — left |

## Sweep coverage

1. **Broken relative links (file-to-file + anchors)** — scripted scan of all 104 in-scope md files
   (markdown links, cross-file anchors GitHub-slugified, same-file anchors). 1 real break (#1, fixed);
   2 false positives (inline-code `<name>.md` template placeholders in
   `docs/coord/proposals/teams-convergence/02-L1-coord-reconcile.md`).
2. **Nonexistent files / commands / anchors** — backticked repo-path scan (`packages|docs|skills|scripts|tools/**.{md,py,sh,json,toml,yml}`)
   → #8 fixed, #17/#18 historical; every documented `coord-engine <verb>` token checked against the
   real v1.10.0 parser verb set (only nonexistent verb found: `migrate`, future-tense in a proposal —
   legitimate); `fulcra-api` subcommands (file upload/download/stat/restore/list/delete, auth
   login/print-access-token, get-records, record, catalog, user-info, share, data-updates) verified
   against the installed CLI's `--help`.
3. **Version-pin freshness** — every `coord-engine-v1.X.Y` / commit-pin string grepped; remote tags
   listed via `git ls-remote` (newest at sweep time: v1.7.2; v1.8.0–v1.10.0 landed mid-review —
   see header). All four primary-install docs pinned v1.10.0 → #2/#3. Historical version mentions (v1.6.x/v1.7.x prose,
   pitch/rollout docs, BUS-V3's vendored `coord-engine-v1.7.2` test tag — which exists) left as-is.
   `packages/coord-engine/tests/test_docs_install_pin.py` run and green after the edits (2 passed).
4. **Command/flag accuracy** — engine help captured for the top level and `queue`, `queue commit`,
   `task` (all 9 subverbs), `review` (request/status/restore), `presence` (beat/show), `remind`;
   every documented invocation of those verbs (30 code-block invocations across scope, plus the
   usage-notation lines in tasks-cli/review-cli/presence-cli/directives-cli) parse-checked against
   `coord_engine.cli.build_parser()` — all real forms parse, including repeatable `-w`/`--reviewer`
   and `queue commit TEAM --token`. `coord-engine obligations`: **zero references in scope** (clean).
   Note: `python3 -m coord_engine.cli` itself trips a circular import — the docs already warn about
   exactly this (GET-ON-THE-BUS sandbox recipe); verification used the console entry point.
5. **`uv tool run` anti-pattern** — full grep; violations #4–#7 fixed; #13 (MCP `uvx` convention),
   #15/#16 (out of scope), and explicitly *historical* docs (`docs/coord/COORD2-README.md` — marked
   "Historical document" — and `docs/coord/proposals/teams-convergence/*`) left.
6. **Terminology canon** — grepped banned framings ("memory layer", "your whole life",
   category-list product definitions, memory-as-durable-state). PR 497's pass held almost everywhere;
   #9 fixed, #14 judgment. Technical uses (session memory, upstream `MEMORY.md`, sleep/location API
   helper names in FULCRA-PRIMITIVES/capability-mapping) verified as sanctioned technical identifiers.
7. **rc-semantics currency** — v1.10.0 ground truth read from `cli.py:_queue_failure` + BUS-V3.md:
   every nonzero queue/`queue commit` exit emits one `queue-error` JSON object under `--json`
   (state + `error_code`), prose to stderr in plain mode; rc 3 = fail-closed (UNKNOWN/INVALID/
   INCOMPATIBLE gates), rc 2 = refusal-class (REFUSED usage/results/stale-token, and ABSENT
   config). BUS-V3.md, AGENTS.md, `packages/coord-engine/README.md` already current (same-slice
   updates); #10 fixed; no doc found still describing an rc-2/absent-shaped or prose-only
   discrimination as THE contract.

## Could not verify (fail-closed)

- **Store-side artifacts**: `team/fulcra/_coord/bus-v3/adopt-latest.sh` and `BOOTSTRAP.md` live in
  the Fulcra File Store, not the repo — this run has no store credentials, so the claim that the
  authoritative pin lives there (asserted by `docs/coord/agents/coord-boss.md` and now referenced by
  the pin notes) is taken on the repo's word, not verified.
- **`fulcra-context-mcp`** resolvability on PyPI (network policy) — #13 left on convention grounds,
  not verified against the published package.
- **Upstream repo paths** (`fulcradynamics/agent-skills` — `skills/fulcra-agent-teams/SKILL.md`
  referenced in `docs/coord/proposals/teams-convergence/00-…`): external repo, not fetched.
- **Runtime behavior of documented commands** (network-touching verbs like `queue`, `doctor` against
  a live store): parse-level verification only; no live team was exercised.
- **`docs/coord/pitch/wave1-pr-draft.md`** claims "refreshed to coord-engine v1.6.3" — a snapshot
  claim about an external PR draft; cannot verify the draft's true state, left untouched as
  historical.
- Shell scripts: none were modified, so no `bash -n` runs were required.
