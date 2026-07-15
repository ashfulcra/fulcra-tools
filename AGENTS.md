# Fulcra Tools — agent guide

Your entry point to this repo. Fulcra helps agents know their user, know
what's happening in their user's world, work with their user's other agents,
and become more helpful over time — the packages here are working examples of
all four. This file covers the non-obvious environment and the conventions
you can't infer from the source; the [`README.md`](README.md) tells the
top-level story (what each package is, how to install the pieces) and this
file does not repeat it.

**This file is a ship-gate artifact.** Every PR that changes agent-facing
behavior — CLI verbs, skills, conventions, environment requirements, review
rules — MUST update this file in the same PR. Reviewers: treat a stale
`AGENTS.md` as a blocking finding. If your change doesn't alter what an agent
needs to know, say so in the PR body ("AGENTS.md: no change needed").

## Where to start

**Zero state — never installed `coord-engine`, or joining from a fresh / remote /
sandboxed host?** Start at
[`docs/coord/GET-ON-THE-BUS.md`](docs/coord/GET-ON-THE-BUS.md) — install → auth →
(remote egress) → team bootstrap from zero → join. The probe grid below assumes the
engine is installed and a `<team>` exists; if `coord-engine` is `command not found`
or you have no team yet, the grid can't help you — the quickstart is the entry.

Already on the bus? Run the probes top to bottom, then jump to the layer you're
touching. First failing probe is where your setup gap is.

| Probe / question | Command | Passes when | Where to go |
|---|---|---|---|
| Engine + auth usable? | `coord-engine doctor <team>` | exits 0 — tooling present, store reachable | never installed / `command not found` / no team yet → [`docs/coord/GET-ON-THE-BUS.md`](docs/coord/GET-ON-THE-BUS.md) (install → auth → bootstrap → join). Otherwise fix the reported gap (auth: `fulcra auth login`; missing/old `coord-engine`: reinstall) |
| On the bus? | `coord-engine briefing <team> --agent <you>` | prints your identity, role inboxes, and everything that needs you | [Coordinate on the bus](#coordinate-on-the-bus) — that fold IS your work queue |
| Own worktree? | `git worktree list` | your cwd is a dedicated worktree, not a shared checkout (no conflict markers or foreign staged files) | [Working tree](#working-tree) — carve your own before committing |
| Touching Collect / the daemon? | — | — | [The daemon (Collect)](#the-daemon-collect) |
| Touching coord conventions? | — | — | [Coordinate on the bus](#coordinate-on-the-bus) |
| Touching the platform surface? | — | — | [Fulcra platform surface & records](#fulcra-platform-surface--records) |
| Touching CI / hooks? | — | — | [CI, the pre-push hook, and workspace membership](#ci-the-pre-push-hook-and-workspace-membership) |

## Layout

uv-workspace monorepo, macOS-first. Packages under `packages/`, agent skills
under `skills/`, each package with its own README, build, and tests.

- **Collect** — the local ingest side: `collect` (the daemon: control socket +
  FastAPI onboarding wizard + worker subprocesses), `menubar` (the macOS
  menu-bar app, PyObjC / rumps), `fulcra-common` (shared API client + ingest
  pipeline), plus the importer packages (`dayone`, `csv-importer`,
  `media-helpers`, `attention`, `netflix-skill`, …).
- **`packages/gmail`** (`fulcra-gmail`) — the local, read-only (`gmail.readonly`)
  Gmail relay: multi-account, keyed by opaque `account_id` (email is metadata,
  never a path/key segment), crash-safe (append-only per-account ledger + a
  contiguous-frontier watermark), landing selected emails in Fulcra Files and
  relaying matches over the coord bus. The load-bearing agent-facing facts:
  the OAuth client is **External / unverified**, so `gmail.readonly` (a restricted
  scope) carries a **100-account lifetime cap** until Google verification + the
  annual CASA assessment; **no subject/from/body is ever logged** (privacy-safe
  reason codes only). Task-by-task module breakdown, the OAuth clickpath, and the
  ledger/relay/pipeline design live in
  [`packages/gmail/README.md`](packages/gmail/README.md) — read it before touching
  the relay.
  Rule authoring is an in-plugin example-first builder (`rules_routes` + `rules_derive`
  + `rules_preview` + opt-in `rules_ai`, UI at `/api/gmail/rules/ui`): search a bound
  account, mark ✓/✗ examples, derive → preview → save; rules persist to
  `plugin_settings.gmail.rules` (the store the engine already reads). The `long_text`
  rules setting stays as a power-user escape hatch.
- **coord** — the agent-coordination layer. In prose it is **coord**; the
  engine is `packages/coord-engine` (a **stdlib-only** CLI, `coord-engine`),
  and the twelve `fulcra-agent-*` skills under `skills/` are how an agent
  actually drives it. (The `coord2` codename is fully retired — code,
  identifiers, and prose all say coord; installers migrate coord2-era
  on-host artifacts automatically when re-run.)
  `packages/fulcra-coord` and `packages/fulcra-coord-files`
  are the **first-generation, LEGACY** layer — kept for provenance and the
  annotations helper only. **Don't build anything new on them.**
- Other agent-facing layers (Continuity, Prefs, Vault, FDE, ATC) are described
  in the README; their skills and READMEs carry the detail.

## Setup & tests

- One command: **`bash scripts/setup.sh`** — installs the right Python + `uv`
  extras + the `fulcra` CLI, then runs the suite to verify (macOS-first; the
  menubar's PyObjC deps are macOS-only).
- The manual equivalent is **`uv sync --all-packages --all-extras`**. Bare
  `uv sync` is NOT enough — pytest lives in each package's `dev` extra and
  PyObjC/rumps in the `macos` extra, so a bare sync fails tests with
  `Failed to spawn: pytest` and the menu-bar can't import. Any sync must keep
  `--all-extras` or it prunes pytest + PyObjC back out.
- Run tests: `uv run pytest packages/ -q` (~4700 tests, a couple of minutes,
  and must NOT hit the network — a network-bound run is the bug, not slowness).
- Editable install: the `.venv` imports the live workspace source, so a code
  change is picked up by **restarting the daemon**, not re-syncing.
- Pull latest into a checkout with `bash scripts/update.sh` (git pull +
  `uv sync --all-packages --all-extras` + restart daemon/menubar).
- PyObjC-free logic is split into its own modules so tests run on Linux CI;
  macOS view-layer tests are marked and skipped off-darwin. Keep new PyObjC
  imports lazy (inside functions), never at module import time.
- Date/clock tests: a module that fixes a top-level `NOW` for its data must also
  **pin the clock** — an autouse `monkeypatch.setattr(cli, "_now", ...)` to a
  `PINNED_NOW` at/just after `NOW` (template: `tests/test_threads.py`), deriving
  relative ages from `PINNED_NOW`, never asserting against the real clock.
  Otherwise the suite flips red once wall-clock passes `NOW + window`. Enforced
  by `tests/test_clock_pin_convention.py`.

## Coordinate on the bus

Durable work — anything another session or agent must see — lives on the coord
bus (Fulcra Files), driven through `coord-engine` and the `fulcra-agent-*`
skills. Subagent-only work stays OFF the bus.

First time on the bus, or joining from a **remote/sandboxed session** (Claude
Code cloud, CI)? Follow [`docs/coord/GET-ON-THE-BUS.md`](docs/coord/GET-ON-THE-BUS.md)
— it covers the egress allowlist (`fulcra.us.auth0.com`, `api.fulcradynamics.com`),
headless device-flow auth (and the `fulcra auth login` HTTPS_PROXY caveat), the
human-free token-refresh grant, team bootstrap from zero, the join sequence,
role-takeover continuity (`continuity resume` at claim time), and the ephemeral-host
doctrine (survival invariant + heartbeat duty for long-lived remote sessions). The canonical invocation is the bare
`coord-engine` binary after `uv tool install` — `uvx`/`uv tool run` cannot resolve
it (not on PyPI).

- **On wake, `coord-engine briefing <team> --agent <you>` is THE entry fold.**
  One call surfaces your identity, your roles' inboxes, and everything that
  needs you including reviews you owe. Start there — never watch a narrower
  surface (a bare inbox or a single view file misses role-addressed work and
  pending reviews).
- **Review handshake.** Nothing lands without an independent review by a
  *different agent identity* than the author — that review is the control, not
  who clicks merge. Where a forge exists the change goes through a **PR, never
  a direct push to `main`**. The handshake rides the bus, not the forge:
  `coord-engine review request <team> <slug> --of <artifact> --reviewer <role>`
  opens a durable obligation that sits in the reviewer's `needs-me` until their
  verdict file exists at `team/<team>/review/<slug>/verdicts/<role>.md` (the
  filename stem is the `required` token — the role passed to `--reviewer` — not
  the holder's own name; that stem is what the tally credits).
  The request is **durable-first, not atomic**: the review doc lands FIRST (that
  doc IS the obligation the tally reads), then the verb delivers one directive
  per required reviewer through the canonical hash-slug path (so a verb-opened
  review fires each reviewer's inbox/`listen` — never hand-send a review tell),
  and a partial notification failure is reported loud (rc 1) naming exactly which
  reviewers were and were not notified — and is **idempotently recoverable**: re-running the SAME
  request (same `of`/`--reviewer` set/`--from`) is idempotent recovery, re-notifying
  only the reviewers a prior partial failure dropped (the doc is left byte-unchanged,
  already-delivered directives dedupe rc 0), so no reviewer is stranded by the
  exists-guard; a re-request with a *different* `of`/required-set/requester is a
  loud rc 1 conflict (a changed required set re-opens only via a new slug), and a
  present-but-unreadable doc fails closed (rc 1, never overwritten);
  `coord-engine review status <team> <slug>` computes APPROVED/CHANGES/PENDING
  and gates the merge. The `<artifact>` is an opaque ref (PR#, branch, commit
  SHA, URL, or a non-code deliverable), so the handshake works with any forge
  or none. A GitHub-only "Approve"/comment does NOT count — co-located agents
  (and Codex) often share one GitHub account, so a forge verdict can no-op; the
  bus verdict, keyed by agent identity, is the source of truth. **The verdict
  FILE discharges the obligation** (write it at the review slug's verdict path,
  then verify `review status` clears you); the ack is inbox hygiene and targets
  the review-request *directive* by its inbox id
  (`review-request-<review-slug>-<hash>`), never the bare review slug. Full rules
  and per-harness wiring live in [`fulcra-agent-review`](skills/fulcra-agent-review/SKILL.md)
  and [`fulcra-agent-automation`](skills/fulcra-agent-automation/SKILL.md).
- **Park a role, don't mute the sweep by hand.** Deliberately leaving a role unattended (a reviewer on leave, seasonal on-call) is an ENGINE fact, not an agent-side convention: set `dormant_until: <ISO>` in `team/<team>/roles/<role>.md`, and while that date is future the mechanical `escalate` sweep suppresses the role's vacancy escalation on every heartbeat host and `roles status` reports `DORMANT (until <ts>)`; escalation resumes automatically past the date, a live lease still shows HELD, and a garbage `dormant_until` fails OPEN (noted on stderr, escalation still fires) so a typo can't silently mute a role — see [`fulcra-agent-roles`](skills/fulcra-agent-roles/SKILL.md).
- **Fold text is capped; the task doc is the payload's home.** Summaries rows bound
  `title`/`description` to `COORD_SUMMARY_TEXT_CAP` (default 280 chars, ellipsis-marked),
  so `inbox`/`briefing`/`board` show enough to triage, never the full body of a long
  directive — read the task doc (`team/<team>/task/<slug>.md`) before acting on one.
- **Engine surfaces a watcher must honor.** Two invariants a watcher lives by:
  - **Slug dedup + delivery rc.** Every directive slug carries a payload hash
    (`<title-slug>-<sha256(payload)[:8]>`), so identical resends dedupe by construction and distinct
    messages can never share or clobber a slot: rc 0 `directive <slug> already delivered` is a *deduped
    identical resend*, and rc 1 `cannot verify delivery, retry` means the slot was unreadable — never
    overwritten, safe to retry.
  - **Honor every degraded row; never read a bounded fold as complete.** `briefing`/`needs-me` bound
    each section under `COORD_BRIEFING_BUDGET` (default 60s, opened once at the TOP of `briefing` and
    spent cumulatively across presence + forge + resume) and emit a `{scanned, total, skipped}`
    degraded row per section — `review-fold-degraded` (also bounded per-slug by
    `COORD_REVIEW_FOLD_BUDGET`, default 45s), `forge-degraded`, `presence-degraded` — plus the
    public-read `read-degraded`/`inbox-degraded` markers below. On ANY of them, fall back to the
    section's direct sweep (`review status` per slug, `forge feedback`, `presence show`) — see
    [`fulcra-agent-review`](skills/fulcra-agent-review/SKILL.md) and
    [`fulcra-agent-automation`](skills/fulcra-agent-automation/SKILL.md) for the per-section fallbacks.
    The review sweep itself **fails closed**: `review status` returns rc 1 (`tally unknown, retry`) when
    the doc, the verdicts *listing*, or any verdict shard is unreadable, rather than printing a partial
    APPROVED — so a degraded transport can never green-light a merge.

  These budgets rest on **hard per-op boundedness**: every transport subprocess runs in its own process
  group and is SIGKILLed whole on timeout (a hung child can't leak a pipe-holding tree past the bound).
  The per-op bound is `COORD_TRANSPORT_TIMEOUT` (float seconds, default 30; unparseable/≤0/NaN/inf →
  default) — **run it TIGHT on a watcher (e.g. 8s)** so the fold budgets above buy real responsiveness.
  Every `COORD_*` tuning knob (default, unit, what it bounds), the shared positive-finite parse policy,
  and the `FULCRA_COORD_*` legacy-prefix rule are catalogued in one place:
  [`packages/coord-engine/README.md` → Environment / tuning](packages/coord-engine/README.md#environment--tuning).
  The *mechanics* that spend those budgets live in one place too — `coord_engine/budget.py`
  (`Deadline.open/expired/reserve` for the absolute-`monotonic()` deadline + reserved sub-budget,
  `degraded_row`/`fold_degraded_line` for the `{scanned, total, skipped}` marker and its renderer).
  **This is a ship-gate: a NEW bounded fan-out uses `budget.Deadline` for its deadline check (never a
  hand-rolled `time.monotonic() >= deadline`) and `budget.degraded_row` for its marker**, so the whole
  family keeps one `>=` boundary and one degraded shape (`config.py` = the env parsers; `budget.py` = the
  deadline/degraded mechanics — import both).
- **The public-read failure contract — UNKNOWN is loud, never a clean-empty.** Every aggregate-backed
  public read (`status`, `board`, `needs-me`, `search`, `inbox`, plus the `agents`/`digest`/`asks`/
  `briefing` bundles) folds the summaries index via `_load_rows_status`, whose `ok` bit is **False when
  the index/listing is UNKNOWN** — an unreadable/corrupt index, a read that failed under a degraded
  transport, or a degraded freshness overlay — as distinct from a genuinely-ABSENT index (a fresh team,
  no reconcile yet), which is a real, readable **empty** (`ok` True). A read whose `ok` is False must
  **NEVER return a clean-empty result**: it emits the one shared marker `_read_degraded_row(reason)` =
  `{"type": "read-degraded", "reason": …}` (family-consistent with `review-fold-degraded` /
  `forge-degraded` / `presence-degraded` / `threads-degraded`; `inbox` stamps its named
  `inbox-degraded` type), carried IN the `--json` result (a list element, or a reserved
  `read-degraded` key on the counts/board/digest objects, so stdout stays one parseable value) and as a
  stderr notice in text mode, while retaining any partial rows. This is the README's *"fails loud, never
  silent"* property; `threads` is the reference implementation. The hazard it closes: a silently-empty
  task fold that reads "all clear" while a live unacked directive is merely unreadable. **This contract
  is a ship-gate: a new aggregate-backed read consumes `_load_rows_status` (never `_load_rows`) and
  surfaces the marker on `ok is False`, with a red-first test asserting no clean-empty under a degraded
  transport.**
- **The rc / error register a watcher parses.** Machine `type` fields ride the degraded **fold rows**
  (`*-degraded`); the **single-slug verify** paths are prose at **rc 1**, where the convention is
  load-bearing: the prose ends in **"…, retry"** iff the failure is retryable (a transient
  unknown — e.g. `review status` `tally unknown, retry`, `roles status` `lease state unknown … retry`,
  `tell` `cannot verify delivery, retry`) and names a **tombstone** iff terminal (a `review status` on a
  soft-deleted review — a retry never resurrects it). An **UNEXPECTED** exception is neither: the
  top-level guard emits a registered envelope `coord-engine: error: command=<cmd> type=<Exc>: <msg>`
  (rc 1) — the `error:` token distinguishes an engine fault from a retryable degrade. The load-bearing
  `listen` daemon wraps each tick in a guard: an unmodeled tick fault emits `LISTEN DEGRADED: tick
  raised …` and the daemon **continues** (one degraded tick, never a dead watcher); `--once` stays
  unguarded so a scheduled run surfaces its failure.
- **Views never lie past the current read — the index-freshness invariant.** Two mechanisms keep
  `status`/`board`/`inbox` honest between heartbeats, so a same-minute close or a between-tick directive
  can't leave a surface stale:
  - **Same-minute-touched docs are reparsed, not reused.** Because the store `file list` mtime is
    minute-granular, reconcile reuses a prior summaries row only when the doc is unchanged by mtime AND
    byte size AND its mtime-minute is provably closed before the last reconcile read — so a doc touched
    twice in one clock-minute is reparsed, never trusted stale. (The honest narrow guarantee; not a
    general sub-minute exactness claim.)
  - **A freshness overlay surfaces new docs THIS read.** Every summaries-index fold (`inbox`, `listen`,
    `briefing`, `needs-me`, `board`, `status`) lists the task dir once and unions in any doc written
    since the last reconcile, so a directive delivered between heartbeats surfaces now, not a
    reconcile-period later. It is bounded (`COORD_OVERLAY_CAP` reads, default 16; `COORD_OVERLAY_BUDGET`
    time, default 10s) and **degrades the `inbox` source visibly** when capped, budget-breached, or a
    listed doc is unreadable — capped-but-visible, never silent truncation. A fresh team (no index yet)
    is unchanged.

  Mechanics (stamping, deterministic cut, the reconcile reuse anchor) live with the engine —
  [`fulcra-agent-reconcile`](skills/fulcra-agent-reconcile/SKILL.md) and
  [`packages/coord-engine`](packages/coord-engine/README.md).
- **`listen` is the engine-owned watcher — don't hand-roll one.** `coord-engine listen <team> --agent
  <you> [--once] [--json]` is the await leg of `tell`: each tick it id-diffs (not counts) three sources
  against a per-agent state file — new **inbox directives plus directives routed to a role you hold a
  fresh lease on** (a strict superset of the `inbox` fold; a lease handoff re-routes the very next tick),
  new **responses to directives you own** (the reply leg of `respond`), and new **verdicts on reviews you
  requested** (the await leg of `review request`, including the terminal `SETTLED <slug>` line). One event
  line per new item (`DIRECTIVE`/`RESPONSE`/`VERDICT`/`SETTLED`/`ORPHAN`; `--json` = one object per line);
  a quiet tick prints NOTHING. It never advances state over an unread tick (a failed read re-surfaces the
  pending event on recovery) and prints `LISTEN DEGRADED:` to stderr **once per source per streak** across
  five independent sources (`inbox`, `responses`, `orphans`, `verdicts`, `roles`) — so a permanent orphan
  can't pin the flag and silence a fresh outage. `--once` **always exits 0** (no output = nothing new, not
  an error) — run it on a scheduler, or bare for a poll loop (`--interval`, SIGINT-clean). Every send verb
  arms you with the exact `listen` line to run for replies. The deeper mechanics — role-expansion
  asymmetry (which verb expands roles for directives vs reviews), the orphan/tombstone/unknown
  classification of dir-only review slugs, and the classify budgets (`COORD_LISTEN_CLASSIFY_BUDGET`) —
  live in [`fulcra-agent-automation` §2](skills/fulcra-agent-automation/SKILL.md), the one skill the
  launchd/cron listener, live sessions, Codex, and headless all delegate to. (`review status` on a
  tombstone slug is terminal rc 1 — see [`fulcra-agent-review`](skills/fulcra-agent-review/SKILL.md).)
- **Delivery rule.** The human-visible report is a turn's (or tick's)
  **terminal output** — composed last, after every tool call. Text followed by
  more tool activity may never render ("sent" is not "delivered"), so anything
  that MUST reach a recipient (human or agent) goes on the bus as a durable
  artifact (ask, review doc, snapshot), never only in session text.
- **Backlog.** A "do later" item goes ON THE BUS:
  `coord-engine later <team> "<title>" -s "<context>"` parks it on the `@backlog`
  audience (durable, visible on the `board`, spams no inbox); route it later
  with the ordinary assignment verbs. Backlog in session memory alone dies at
  compaction.
- **Intent-capture doctrine — a spoken commitment is filed the SAME turn.** When
  Ash states an intent to ANY agent ("later today", "I'll enumerate that list",
  any commitment he owns), that agent captures it immediately with
  `coord-engine intent fulcra "<text>" --for ash [--by <when>]` — an uncaptured
  commitment is the drop nobody can see. Two surfaces back this:
  - **`coord-engine intent <team> "<text>" --for ash [--by <when>]`** — sugar over
    the directive path (writes an `intent:ash` item, `intent_by` frontmatter,
    hash-slug delivery + read-back inherited). Identity is **text + assignee
    only** — `--by` is EXCLUDED. So an identical restatement dedupes (rc 0 `intent
    already captured`), while a restatement with a DIFFERENT `--by` is a verified
    in-place window update on the same doc (rc 0 `intent window updated`,
    read-back-checked; unverifiable → rc 1, retry — never a stale deadline, never
    a forked item). A relative `--by` (`5d`/`36h`/`10m`) re-resolves from now on
    each restatement.
  - **`coord-engine threads <team> --for <principal> [--json]`** — the dropped-work
    fold, three mutually-exclusive modes (first match wins):
    **started-then-silent** — an item Ash owns/last-touched whose activity is older
    than `--silence-days` (default 3); **blocked-on-ash** — progress waits on Ash
    (`assignee: ash`, `blocked-on:ash` tag, or a `needs:human` block naming him),
    surfaced immediately, no aging; **intent-never-started** — an `intent:ash` item
    past its window (`intent_by` if declared, else capture + `--intent-grace-hours`,
    default 48) and not followed up (status advanced, a response shard, or a
    `followed-up-by:` tag each discharge it). Windows: `--silence-days` /
    `--intent-grace-hours`, env `COORD_THREADS_SILENCE_DAYS` /
    `COORD_THREADS_INTENT_GRACE_HOURS`. A **terminal item (`done`/`abandoned`) is
    NEVER a dropped thread** in any mode — the fold refuses it and the adapter reads
    the authoritative status from the task doc, not the summaries index (a same-minute
    close can leave the index stale-`proposed`). The fold's aggregate read deadline is
    `COORD_THREADS_FOLD_BUDGET` (default 30s). A **`threads-degraded` row** (a JSON
    object under `--json`; a stderr notice in text mode) means the fold saw only PART
    of the store (budget breach or an unreadable shard) — sweep or wait, **never trust
    it as complete**. coord-boss runs
    `threads fulcra --for ash --json` in its loop and owns the curation/push call.
- **ATC (air-traffic control).** On a subscription-cap fleet, consult
  `coord-engine route <team> --needs <tags>` before a dispatch to pick the cheapest
  model that covers the work, and log the outcome after:
  `coord-engine usage log <team> --account <id> --tier <tier> --model <m>
  --task-class <tag> --outcome clean|rework|escalated`. That ledger feeds the
  headroom fold and demotes a model that keeps failing a task class. Rubric and
  routing procedure: [`fulcra-agent-atc`](skills/fulcra-agent-atc/SKILL.md).
- **ATC coordinator joins.** Declare `team/<team>/atc/bindings.json`
  (agent/role -> account/tier[/model/task_class]); then `coord-engine atc
  harvest <team>` folds settled review families into outcome shards (idempotent,
  zero-unit — feeds demotion, not headroom), and `route --needs ... --for-role
  <role>` filters to the role's bound account and reports lease liveness so
  dispatch never routes into a void. See [`fulcra-agent-atc`](skills/fulcra-agent-atc/SKILL.md).
- **Timeline projection (opt-in).** `coord-engine annotate resolution <team>
  transitions` (default `off`) makes the heartbeat project task transitions onto
  your Fulcra timeline model-free, right after each reconcile; `annotate status
  <team>` shows the level + cursor. It is the successor to the legacy
  `fulcra-coord annotations` writer — enabling it requires that writer stay off
  (see [Fulcra platform surface](#fulcra-platform-surface--records)). Projection
  needs the typed-record writer (`fulcra-common`) installed *beside* coord-engine
  (`uv tool install … --with fulcra-common`); without it the step is a silent
  exit-0 no-op. Setup + install recipe:
  [`docs/coord/GET-ON-THE-BUS.md`](docs/coord/GET-ON-THE-BUS.md#enable-timeline-projection-recommended)
  and [`fulcra-agent-automation`](skills/fulcra-agent-automation/SKILL.md).

## Operator knowledge: vault + prefs

Long-running work with the operator accumulates knowledge about them. Store it
on Fulcra **the turn it is stated** (the same capture doctrine that applies to
intents), split by kind:

- **Facts about the operator's world** — people, companies, places, pets,
  routines, the semantics of their Fulcra data ("a hike on X road is always a
  dog walk") — go in **[`fulcra-vault`](packages/fulcra-vault/README.md)**: a
  markdown knowledge vault in Fulcra Files (live today; it also hosts the
  meeting CRM). Write an owned section under your agent id, add a log line,
  `reindex`; give entities their own wikilink-able notes. The CLI works from
  any authed host.
- **Preferences** — how the operator wants to be served: tone, format, defaults,
  per-platform overrides — are **[`fulcra-prefs`](packages/fulcra-prefs/README.md)**
  signals (typed, decaying, confidence-weighted, deterministically compiled).
  If `fulcra-prefs capture` fails (the layer is alpha), do NOT drop the signal:
  store the statement as a vault note and queue the signal keys there for
  capture once the layer is up.
- **Retrieval, today**: both layers inject at session start — vault
  `install-hooks` loads `HOT.md`, prefs compiles per-platform docs into the
  boot context. Beyond the hot set, the convention is judgment-based: **when a
  person, company, place, or project is named in your work, consult the vault**
  (`fulcra-vault read MAP.md`, `backlinks`) before asking the operator or
  guessing. Deeper retrieval (search/MCP) is future work — storage now is what
  makes it possible later.

## Working tree

Prefer a **per-agent git worktree**, not a shared checkout — concurrent
sessions sharing one working tree clobber each other's index/`HEAD`
(interleaved commits, orphaned merge conflicts). Each session gets its own tree
(and its own per-cwd identity): `git worktree add ../<repo>-<purpose> -b
<vendor>/<purpose> origin/main`. Conflict markers or staged files you didn't
create mean you're sharing a checkout — move out before committing.

## Commits

Author commits as `ashfulcra
<114089064+ashfulcra@users.noreply.github.com>` and end the message with the
trailer `Co-Authored-By: <your model> <noreply@anthropic.com>` (e.g.
`Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`).

## Documentation rules (standing, operator-set)

The docs' primary reader is an **agent**; the showcase test is the goal: a
founder drops this repo's link to their agent asking "anything useful here?"
and the docs get it to "yes, and here's how" unaided. Standing rules, each
earned by an incident:

- **Truth over aspiration.** Document live, verified behavior; stamp
  verification dates and drift headers where the platform moves faster than
  the repo (the FULCRA-PRIMITIVES pattern). Doc claims get adversarially
  reviewed like code.
- **Exact commands, exact paths, canonical form.** Bare `coord-engine`, never
  `uv tool run coord-engine` (not on PyPI). One wrong documented filename made
  role-gated reviews structurally unapprovable (v1.6.4 fix) — path precision
  is correctness, not style.
- **No dead or broken references** (operator rule, 2026-07-14): relative
  links must resolve, referenced files/commands/sections must exist, and
  mentions of real repo things should BE links. Sweep on every docs QA pass.
- **Teach fail-loud, never fail-quiet.** No documented pattern may swallow
  errors; if a leg degrades to a no-op without its backend, the doc says so
  in bold (the silent-writer darkness).
- **Docs ship with the change, same PR, dual-green.** Docs debt is in scope,
  never a follow-up. This file is the ship-gate.
- **One canonical home per fact**; everything else links to it. Scattered
  version pins and duplicated doctrine are drift bombs.
- Historical docs (proposals/, superseded designs) carry a **historical
  banner** instead of being rewritten; broken references get fixed even there.

### Writing for upstream (issues & PRs to fulcradynamics/*)

Upstream engineers read none of this repo (operator-relayed feedback, 2026-07-14).

- **Succinct.** First sentence states the bug. Repro, expected, actual, one
  self-contained piece of evidence (a curl, a traceback). Ten lines.
- **Their vocabulary only.** No fulcra-tools terms, codenames, or links —
  evidence must reproduce from their code alone.
- Everything else — discovery story, fleet impact, workarounds — stays here.

## CI, the pre-push hook, and workspace membership

- **macOS CI is path-filtered and bills at 10×**, so it only runs on
  macOS-relevant changes (`packages/fulcra-menubar/**`, `packages/coord-engine/**`,
  `skills/fulcra-agent-automation/**`, and the macOS-touching `fulcra-coord`
  modules). Everything on Linux (`uv-workspace.yml`) runs on every push/PR to
  `main`. The upshot: for anything the macOS job skips, the **local gate is the
  real one** — run the relevant suite before you push.
- **Pre-push hook.** A shared `pre-push` hook in `.githooks/` runs the LEGACY
  `fulcra-coord` suite before any push that touches
  `packages/fulcra-coord/(fulcra_coord/|tests/|pyproject.toml)` — that package
  is the one with no full server-side gate. It's version-controlled but
  `core.hooksPath` is per-clone, so **enable it once in every clone you push
  from:** `git config core.hooksPath .githooks`. Bypass a single push with
  `git push --no-verify`; needs `uv` on PATH. (`coord-engine` is CI-gated on
  both runners, but still run its pytest suite locally before pushing.)
- **Workspace exclude.** Any directory under `packages/*` that is NOT a uv
  member (no `pyproject.toml`) must be added to `[tool.uv.workspace] exclude`
  in the root `pyproject.toml`, or it breaks `uv sync`/`uv run`/`uv tool
  install` for everyone (the `uv-workspace` CI guards this). `packages/web-ui`
  (a frontend, no `pyproject.toml`) is excluded for this reason.

## Fulcra platform surface & records

[`FULCRA-PRIMITIVES.md`](FULCRA-PRIMITIVES.md) is the field guide to the whole
platform surface (auth, files, annotations, queries, MCP), organized by agent
capability tier — CLI/lib, raw HTTP, or MCP-only. Read it before re-researching
anything about the platform, and **check the installed `fulcra-api` version,
not the repo** (the CLI ships ahead of its git main on PyPI).

- **Spec-backed raw endpoints are first-class.** Anything in the published
  Fulcra OpenAPI (`api.fulcradynamics.com`) is fair game when it makes the work
  easier — a documented raw REST call is a legitimate tool, not a last resort.
  Still prefer the `fulcra` CLI / Python lib when you have a shell and a verb
  exists; the MCP server is read-only.
- **Records are write-via-ingest.** Two write paths, both in the OpenAPI spec
  (spec-verified 2026-07-08):
  - **Typed (preferred, new):** `POST /ingest/v1/record/{data_type}` takes an
    **unwrapped** record payload for that data type, and accepts jsonlines for
    batch (one record per line). Discover types via `GET /data/v1/catalog`
    (`recordable`/`api_version` fields) and the record shape via
    `GET /data/v1/catalog/{data_type}/{api_version}/schema`. Caveat: custom
    data types still reference the annotation id in the record's `sources`.
  - **Legacy:** `POST /ingest/v1/record` with a wrapped `DataRecordV1`
    (`data_type` rides in `metadata`) — published in the spec. The old JSONL
    batch path `POST /ingest/v1/record/batch` is **NOT in the published
    OpenAPI** (works in production; treat as retirement-eligible) — prefer the
    typed endpoint's jsonlines mode for new code.

  There is **no record-level delete/replace and no `fulcra` record-write/delete
  CLI verb yet** (the CLI verbs will be built on the typed endpoints) — model
  corrections as new (superseding) records. When the CLI record verbs land, the
  primitives doc gets a full re-verification, not a patch — flag it on the bus.
- **The legacy `fulcra-coord annotations` writer must stay OFF on every host.**
  It defaults to off (inert); leave it there — an accidental `on` has caused
  duplicate-record proliferation. Its successor is the heartbeat **projection
  fold** (`coord-engine annotate resolution <team> transitions`). Note the
  duplicate risk is TWO WRITERS minting *different* ids for the same logical
  moment: the typed ingest endpoint **upserts records with matching explicit
  ids** (live-verified 2026-07-14), so the projection fleet's deterministic
  ids converge — but the legacy writer generates its own ids and would still
  duplicate alongside it. Use projection for timeline annotations, never this
  writer.

## The daemon (Collect)

- Run it durably as a **launchd** agent, NOT a backgrounded shell process — a
  foreground/`&` daemon dies when its terminal or session ends. Install + load:
  `uv run fulcra-collect install`, then `launchctl bootstrap gui/$(id -u)
  ~/Library/LaunchAgents/com.fulcra.collect.plist`. Restart: `launchctl
  kickstart -k gui/$(id -u)/com.fulcra.collect`. Stop: `launchctl bootout
  gui/$(id -u)/com.fulcra.collect`. Logs: `~/Library/Logs/fulcra-collect/`.
- Subcommands: `daemon install status run enable disable set-credential
  set-interval plugin doctor`. There is **no `start`**; `doctor` runs the
  pre-flight diagnostic.
- Config dir `~/.config/fulcra-collect/`: `control.sock` (the UDS the menu-bar
  + CLI use), `web-url` (default `http://127.0.0.1:9292`), `web-token` (Bearer
  for the web API).

### launchd PATH gotcha

launchd runs the daemon with a restricted PATH
(`/usr/bin:/bin:/usr/sbin:/sbin`) and does NOT source your shell profile — so
`~/.local/bin` (where `uv tool install fulcra-api` puts the `fulcra` CLI) is
invisible. Any code shelling out to the `fulcra` CLI must resolve it via
`credentials._find_fulcra_cli()` (PATH → `~/.local/bin` → homebrew), **never**
bare `shutil.which("fulcra")`.

### Keychain

- User secrets (the Fulcra `bearer-token`) live in the OS keychain via
  `keyring`, service `fulcra-collect:user`. A read can block on a macOS ACL
  confirmation dialog; `credentials._keyring_get` times out after 5s and the
  daemon degrades to "Fulcra not authenticated".
- Sign in **through the daemon's web wizard** (`open "$(cat
  ~/.config/fulcra-collect/web-url)"`) so the daemon — not a one-off script —
  owns the keychain item. If the "Python wants to use your confidential
  information" prompt repeats, click **Always Allow** (not "Allow"). If it still
  repeats, the item is owned by a stale binary: `security
  delete-generic-password -s "fulcra-collect:user" -a "bearer-token"`, restart
  the daemon, re-sign-in.

### Menu-bar app

- Launch from a GUI (Aqua) session: `uv run --package fulcra-menubar python -m
  fulcra_menubar`. Not from SSH/detached shells, or the status item won't
  appear. Under Homebrew Python the bundle id is `org.python.python` (use that
  for computer-use / TCC grants, not `com.apple.python3`).
- It talks ONLY to the daemon over the control socket; it never reads the
  keychain. Auth state, tracks, and plugin status all come from the daemon — a
  stale UI usually just needs a relaunch / reopened popover.
- Bundle-requiring macOS APIs (`UNUserNotificationCenter`, etc.) raise an
  **uncatchable** NSException when run unbundled (`python -m` from a venv) —
  `try/except` can't recover it. Guard with
  `_notify_macos.running_in_app_bundle()`. The shipped app is bundled via
  Briefcase.

### Sign-in & first run

Full first-run walkthrough + troubleshooting: [`docs/TESTING.md`](docs/TESTING.md).
Diagnose a live install with `uv run fulcra-collect doctor`.

## Repo homes

This monorepo is **only for things that make Fulcra useful for other people.**
Fulcra-related infra that isn't useful-to-others enough → its own
`ashfulcra/<repo>`; personal/unrelated projects → their own `reversity/<repo>`.
Ask the operator when unsure.
