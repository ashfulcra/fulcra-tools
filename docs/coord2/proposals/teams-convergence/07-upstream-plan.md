# Upstream plan — contributing coord2 to Fulcra

**Goal:** get coord2's skills + engine adopted upstream (`fulcradynamics/agent-skills` + the fulcra-api
surface), so the "pro tier of the official skill" stops being a personal fork and becomes the official
capability. Operator (Ash) is at Fulcra — this is an internal champion play, not a cold OSS PR: optimize
for *trivially adoptable*, not *persuasive*.

## What goes upstream (three separable tracks)

### Track 1 — the skills (PR to `fulcradynamics/agent-skills`)
The 11 `fulcra-agent-*` skills (operator was added after this doc's first draft), shape-identical to upstream (`skills/<name>/SKILL.md` + `references/`,
scripts allowed per the `fulcra-dashboard` precedent). Contribution = `git mv` + polish:
- **Wave 1 (low-friction, additive):** `presence`, `roles`, `continuity`, `review`, `directives`,
  `health` — additive derived state and team directories that do not reinterpret existing upstream
  files or change any existing upstream skill's behavior. It still introduces durable `_coord/`
  shards, review/task/presence folders, and generated views, so the pitch must be precise: "no
  semantic change to current files," not "no new semantics."
- **Wave 2 (touches teams' own conventions):** `reconcile`, `tasks`, `forge`, `automation`, `operator`. Reconcile
  makes `task/index.md` **engine-owned**, which contradicts `fulcra-agent-teams`' documented
  "hand-maintain the index" guidance — so wave 2 must include a small **amendment PR to
  fulcra-agent-teams' SKILL.md** ("if fulcra-agent-reconcile is installed, do not hand-edit the index"),
  and that's a conversation, not just a diff.
- Polish before PR: strip coord2-repo-relative links from SKILL.md files; ensure every skill stands alone;
  drop `docs/proposals/` internals (upstream gets a single DESIGN.md summarizing the architecture +
  review lineage instead).

### Track 2 — the engine (decide its home; this is the load-bearing open question)
`coord-engine` is one stdlib-only tool the skills shell to, exactly like they shell to `fulcra-api`.
Initial adoption should minimize Fulcra review burden; the best long-term home does not have to be the
first landing zone.

Three options, in recommended adoption order:
1. **`fulcradynamics/coord-engine` repo + PyPI** under Fulcra's org. This is the preferred initial ask:
   it keeps the tool/skill separation, gives the official skills a Fulcra-owned dependency, and lets API
   reviewers inspect a stdlib-only CLI without also accepting a fulcra-api surface-area change.
2. **Tagged coord2 engine as bootstrap** (`ashfulcra/coord2` tag, already proven at v1.x). This is the
   lowest-friction bridge while Fulcra decides ownership. It must be explicitly temporary and pinned by
   tag/commit in docs so upstream skills do not drift against `main`.
3. **Fold into `fulcra-api`** as a command group (`fulcra-api team …`). Best end-state: one tool, one
   auth, one install; the skills' prose changes from `uv tool run coord-engine X` to
   `uv tool run fulcra-api team X`. Cost: Fulcra owns the code + release cadence and the transport
   rewrite replaces subprocess/text calls with internal APIs, so this is a convergence target after the
   skill contract proves useful, not the first ask.

Decision gates:
- Start with option 1 if Fulcra is willing to host/release a small package.
- Use option 2 for the first upstream skill PRs if repo/PyPI ownership would delay review.
- Revisit option 3 only after wave 1 is accepted, the derived-state contract is stable, and API-team
  sizing says the command group is worth carrying in fulcra-api proper.

### Track 3 — fulcra-api platform asks (filed as issues w/ evidence, independent of 1–2)
Each has a concrete incident/measurement behind it from this build:
- **`--format json` for `file` + `catalog` + `data-type` verbs.** Evidence: coord2 line-parses text
  (two review findings — list-order nondeterminism, minute-granular mtimes); the catalog **shape drift
  in 0.1.35 caused the duplicate-timeline-tracks incident** (9× + 4× dupes) because there is no stable
  structured contract.
- **Per-file version-id (or precise timestamp) in `file list`.** Evidence: incremental reconcile is
  forced to a conservative minute-granularity compare.
- **Batch read (`file download` many / prefix fetch).** Evidence: every fold is list + N×download at
  ~1s/op; the 139-task migration took 12 min of sequential round-trips; reconcile cost scales linearly
  with every new shard dir.
- **`catalog` should hide (or flag) archived data types.** Evidence: `data-type archive` leaves entries
  listed with no marker — we needed per-host pins (0.15.18) purely because archived duplicates still
  match by name.
- **Record-write CLI verbs** (create/correct/delete). Evidence: coord2 deferred timeline-annotate on
  this gap; the prefs backlog (`native record delete/replace`) is blocked on the same thing.

## Sequencing
1. **Package** (me, now): upstream-ready branch — skills polished + standalone DESIGN.md + evidence
   summary (test counts, review lineage, live-migration numbers, incident postmortems).
2. **Internal pitch** (Ash): one pager + demo on the live team (`briefing`, `board`, `health` on real
   data beats any deck). Decide Track-2 home with the API team; hand them Track-3 issues.
3. **PRs**: wave-1 skills PR → teams-amendment + wave-2 PR → engine per the Track-2 decision.
4. **Post-acceptance:** coord2 repo becomes a thin dev mirror or archives; fleet reinstalls from
   upstream (setup script already installs by copy — repointing the clone URL is the whole change).

## Standalone posture if Fulcra stalls
If Fulcra does not accept or host the work within ~3 months, coord2 remains a credible standalone package
rather than an indefinitely personal experiment:
- Release cadence: monthly patch releases while active, immediate patch for data-loss/security bugs, and
  a tagged minor release whenever the OKF/task/review schema changes.
- Compatibility matrix: each release notes the tested `fulcra-api` range, supported upstream
  `fulcra-agent-teams` commit/tag, and any required migration step for `_coord/` or task shards.
- Install docs: keep a forge-neutral install path that does not assume upstream acceptance
  (`uv tool install`, copy/install skills from a tag, verify with `coord-engine doctor/reconcile`).
- Ownership: Ash owns product direction; automation agents can prepare fixes, but security and
  data-loss releases require explicit operator review plus the same independent bus review before merge.
- Exit criteria: if Fulcra accepts wave 1 or hosts the engine, standalone docs switch to "development
  mirror"; if not, coord2 keeps tagged releases and compatibility notes until the bus stack is replaced.

## Road not taken — one bundled skill
One `fulcra-agent-teams-v2` or `fulcra-agent-teams-pro` package would improve one-shot install and make
the feature set easier to discover. It also avoids cross-skill version skew: users would not accidentally
install `review` without the matching `reconcile`/`directives` assumptions.

The plan still keeps 10 sibling skills because it matches upstream's current contribution shape and keeps
review diffs small. The cost is real: the pitch must include an install recipe that enables the common
bundle in one command or checklist, plus compatibility notes that name which skill versions were tested
together. If upstream prefers a bundled pro skill for discoverability, use the existing sibling skills as
source modules and expose the bundle as the packaging surface.

## Explicitly NOT upstreamed
- `migrate.py` + docs 06 (coord-specific, one-shot, done).
- The incumbent (`fulcra-tools-coord`) and its 0.15.x fixes — sunsetting.
- The account-specific canonical pins (host-local cache entries).
- `docs/proposals/` history (summarized into DESIGN.md instead).
- Proposal-only rationale that upstream reviewers need to operate the system. DESIGN.md must carry the
  durable operational pieces: shard/ack garbage collection, stale/grace semantics, lease/session nonce
  rationale, review fail-closed behavior, and health/history retention. Proposal history can stay out of
  tree only after those invariants are summarized in the upstream artifact.

## Risks / open questions (inputs to the debugging pass)
- Upstream conventions may have MOVED since the 2026-07-01 clone (repo is an active alpha).
- Fulcra-api fold (option 3) rewrites the transport layer — scope unknown until their API team weighs in.
- The teams-amendment (engine-owned index) is the one semantic change Fulcra could reject; wave 1 is
  deliberately independent of it.
- `npx skills add ashfulcra/coord2` compatibility assumed, never tested.
- 11 skills at once may be too big a bite for one review — wave split mitigates; be ready to trickle.

---

## Systematic-debugging pass (2026-07-04) — assumptions verified against live reality

| # | Assumption | Verdict | Evidence |
|---|---|---|---|
| A1 | Upstream unchanged since 07-01 clone | **FALSIFIED** (drifted) | Fresh clone: PRs #127 (teams habits+roles prose), #128 (cron optional) merged since |
| A2 | Skill frontmatter shape-identical | VERIFIED | Same fields incl `metadata.openclaw.emoji` |
| A3 | No name/semantic collision | VERIFIED w/ nuance | Upstream "roles" = member `role.md` prose (ask-user-on-join), NOT durable roles; ours stays additive but the PR must position the two explicitly |
| A4 | Scripts-in-skill precedent | VERIFIED | `fulcra-dashboard/scripts/` at HEAD |
| A5 | `npx skills add` compat | UNTESTED (accepted) | CLI provenance unverifiable (npm 403); our copy-install is primary path; note as bonus-not-dependency |
| A6 | Engine installs from git tag | VERIFIED (mechanism) | Proven at v0.4.0; tags v1.0.0/v1.0.1 exist |
| A7 | No PII in upstreamed trees | VERIFIED | Scan clean (skills/, engine/) |
| A8 | Skills stand alone | VERIFIED w/ 1 item | Only repo-relative bit is `homepage:` → flip to upstream URL at merge |
| A9 | Teams still documents hand-maintained index | VERIFIED | Lines 39/55-56 at HEAD → wave-2 amendment still required |
| A10 | Track-3 asks still unmet by fulcra-api | **PARTIALLY FALSIFIED** | 0.1.35 ships a second `fulcra` binary (JSON-default for DATA verbs; catalog JSONL stable across both). BUT: `file` ops still text, no version-id in list, no batch read, no record-write even under `--beta`, archived types still listed → all five asks stand, reworded against 0.1.35 |

### Amendments adopted
1. **Track 3 rebased on 0.1.35**: frame asks as extensions of the new `fulcra` binary's JSON-default
   direction ("finish the job for `file` ops") — an easier internal sell than criticizing fulcra-api.
2. **Wave-1 prose alignment**: upstream teams now mandates check-team-exists-before-create and
   ask-user-your-role-on-join; our presence/briefing/roles SKILL prose must acknowledge both, and
   `fulcra-agent-roles` opens with a "member role.md vs claimable team roles" positioning paragraph.
3. **Polish list**: + homepage flip at merge; + upstream drift re-check IMMEDIATELY before opening each
   PR (the repo moves weekly — A1 falsified in 3 days).
4. **Ops note**: verifying A10 required upgrading this host to fulcra-api 0.1.35 mid-pass; regression
   check confirmed `file` output shape unchanged (coord2 parsers + 0.15.17 matcher unaffected).

---

## Adversarial review (Codex, 2026-07-04) — carry-forward risks

Verdict: APPROVE, no false premises (fresh clone re-verified A1/A3/A4/A9-class claims at review time).
Three non-blocking risks to carry into execution:
1. **Track-2 sizing is not "mechanical" for Fulcra**: the fold into fulcra-api replaces the
   subprocess/text transport with internal APIs — keep explicit sizing for the API team in the pitch.
2. **Reconcile pitch must lead with deterministic derived views**, not change detection — upstream
   teams already ships `data-updates`, so "we notice changes" is table stakes; "two agents always agree
   on the fold" is the differentiator.
3. **Run the documented drift re-check immediately before EACH upstream PR** — open upstream PRs
   (#107/#108 at review time) are old but alive; the repo moves weekly.
