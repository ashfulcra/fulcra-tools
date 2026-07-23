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
  the OAuth client is an **External, published-unverified, Desktop-app** client
  (Desktop because the relay IS a local desktop app: Google treats a Desktop
  client's secret as non-confidential, which is what lets ONE shared client ship
  to many installs; the `127.0.0.1` loopback redirect needs no registration), so
  `gmail.readonly` (a restricted scope) carries a **100-account lifetime cap**
  until Google verification + the annual CASA assessment; **no subject/from/body
  is ever logged** (privacy-safe reason codes only). Task-by-task module breakdown, the OAuth clickpath, and the
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
  and the thirteen `fulcra-agent-*` skills under `skills/` are how an agent
  actually drives it. (The `coord2` codename is fully retired — code,
  identifiers, and prose all say coord; installers migrate coord2-era
  on-host artifacts automatically when re-run.)
  The first-generation `fulcra-coord` and `fulcra-coord-files` packages were
  retired after their last live annotations surface moved to `fulcra-common`.
  Their provenance remains in git history; all coordination work uses coord.
- **`packages/coord-tracker-bridge`** — the alpha, provider-neutral projection
  core for reflecting coord work into external trackers. It ships normalized
  snapshots, a full source-identity ledger, versioned policy, a pure diff plan,
  a `coord-engine --json` source, and a paginated/retrying Linear adapter. Run
  `plan` first, one-time `adopt-markers` when migrating v0.25 issues,
  `apply-resources` explicitly, then `sync`; ordinary sync never creates
  labels/projects or infers identity from titles, and a singleton lease rejects
  overlapping source/tracker/policy runs. Adoption resolves a footer slug
  colliding across `tasks` + derived lanes (`threads`, `asks`) to the canonical
  task row, order-independently; derived-only collisions stay fail-closed. Policy v2 has an explicit lane
  allowlist (omission means exclusion), derives `@backlog` proposed/waiting
  rows to `backlog`, and names asks/threads lanes `asks`/`threads-missed`.
  An incomplete capability suppresses destructive closes only for that scope.
  The optional `--source teams` adapter is strict and read-only over typed
  `team/<team>/task/*.md` documents; ambiguous list/read/parse results degrade
  tasks, while unsupported capabilities remain explicitly unsupported. Command
  intake and expectation evaluation remain explicitly out of scope.
  The engine source accepts both JSON documents and JSONL folds (including
  `threads --json`), retains valid JSONL rows around an interleaved prose
  degraded line while keeping that capability degraded, identifies the exact
  degraded marker path/reason in its diagnostics, and gives the intentionally
  slow fleet-health fold a separate six-minute bound while keeping other folds
  at three minutes.
- Coord retention is on by default: terminal and quiet proposed tasks archive
  after 14 days, settled review families after 7 days, and dead presence shards
  are pruned after 7 days. `COORD_RETENTION_DAYS=0` or
  `reconcile --retention-days 0` is the explicit kill switch; invalid values
  fail safe to the enabled default. Hot review folds consult a compact settled
  index instead of repeatedly classifying historical tombstones, and the legacy
  `artifact/` namespace is consolidated into `artifacts/`. UNKNOWN listings stay
  hot, moves are copy-verified rather than destructive-only, and archived work
  reverses through `task restore` or `review restore`.
- The one-shot `migrate` exporter and unused atomic `handoff` convenience verb
  are retired. Reassign live work with `task update --assignee <agent> --next
  "..."`; when another session needs resumable context, write the continuity
  snapshot first and then reassign the task.
- Machine JSON is compact by contract: public non-ATC `--json` documents,
  line-oriented `listen`/`threads` events, and `_coord/summaries.json` omit
  insignificant whitespace while preserving parsed values and degradation
  markers. Tests and consumers compare parsed JSON unless byte layout is the
  explicit contract.
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

- **Named identities** (Tycho = `coord-boss`, …): the registry is
  [`MAINTAINERS.md`](MAINTAINERS.md) — names are personas for human legibility;
  bus routing always uses the functional id.
- **Durable tooling stash.** An agent's operational bundle (scripts, loops,
  config templates) survives ephemeral machines via
  `coord-engine stash push/pull/list` against
  `team/<team>/_coord/agents/<agent>/stash/`: push refreshes a `manifest.json`
  (per-file sha256 + exec bit), pull restores from it and **fails loud on
  checksum drift** rather than handing back a silently-diverged file. Push runs
  a **fail-closed secrets guard** — secret-shaped names (`.env`, `*.key`,
  `*token*`, …) and credential-shaped content (`lin_oauth_…`, `sk-…`, PEM
  headers) are refused with the tripped rule named; `--unsafe-allow-secrets`
  is for false positives only, because `team/<team>/**` is readable by every
  agent on the bus. Procedures: [`fulcra-agent-durable-state`](skills/fulcra-agent-durable-state/SKILL.md).
- **On wake, `coord-engine briefing <team> --agent <you>` is THE entry fold.**
  One call surfaces your identity, your roles' inboxes, and everything that
  needs you including reviews you owe. Start there — never watch a narrower
  surface (a bare inbox or a single view file misses role-addressed work and
  pending reviews).
- **Quiet listeners must stay model-free.** Use one `coord-engine listen` owner
  per agent identity and wake a model-backed harness only for a new event or a
  newly reported degradation. The bundled scheduled tick emits nothing on a
  healthy quiet pass; `COORD_LISTENER_VERBOSE=1` is diagnostics only. Never
  suppress `LISTEN DEGRADED`: degradation is actionable, does not clear the
  queue, and the awakened session must apply the targeted fallback before it
  reports quiet. Host listeners should use the bundled adaptive cadence: poll
  frequently while events are arriving and through a configurable hot tail,
  then back off locally to a longer idle interval. A skipped tick must not call
  the bus or a model; without source-side push, idle cadence is maximum pickup
  latency. Model-backed harness automations that cannot reschedule themselves
  retain a coarse safety net instead of emulating adaptation in prompt text.
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
  The direct `forge feedback` fallback also has one cumulative
  `COORD_FORGE_SWEEP_BUDGET` (default 60s) spanning review/watch discovery and
  the per-PR three-surface sweep; a cut returns non-zero with a
  `forge-sweep-degraded` marker rather than hanging or reporting a clean partial.
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
  stderr notice in text mode, while retaining any partial rows. When one bounded pass encounters
  multiple independent failures (for example, an unreadable fresh doc followed by overlay-cap
  truncation), `reason` preserves both facts instead of letting the later bound clobber the earlier
  transport failure. This is the README's *"fails loud, never silent"* property; `threads` is the
  reference implementation. The hazard it closes: a silently-empty
  task fold that reads "all clear" while a live unacked directive is merely unreadable. **This contract
  is a ship-gate: a new aggregate-backed read consumes `_load_rows_status` (never `_load_rows`) and
  surfaces the marker on `ok is False`, with a red-first test asserting no clean-empty under a degraded
  transport.**
- **`--json` purity: stdout is ALWAYS one parseable value.** Under `--json`, NO prose ever reaches
  stdout — every degraded/notice line becomes a JSON row or a reserved key (the `_read_degraded_row`
  family), or goes to `file=sys.stderr`; there is no third option. Each fold verb's `--json` branch is
  exactly one `json.dumps` of the single result (`status`/`board`/`digest` embed the marker under a
  reserved key; `needs-me`/`inbox` carry it as a list element; `briefing` uses a bundle key). `threads`
  emits a single JSON **array** — the dropped list plus a trailing `threads-degraded` element — NOT
  JSON-Lines (the leak this closed: streaming one object per line made `json.loads(stdout)` raise on the
  trailing data whenever 2+ threads dropped). **Ship-gate: a new `--json` path is one `json.dumps`, with a
  red-first test that `json.loads(stdout)` yields exactly one value on every degraded path.**
- **Head-of-line: a budget cut may only ever truncate the TAIL — never the head.** The work-discovery
  folds do live per-op transport at query time over an unbounded population; under budget pressure the cut
  must land on the *lowest-priority* tail, so an agent's OWN assigned work and any decision parked on a
  human can never be the thing that goes invisible. Two structural heads enforce this:
  - **Blocked-on-human is the reserved FIRST section, and it is FREE.** `briefing` and `needs-me` render
    open rows blocked on a human before presence/board/inbox, computed by `query.blocked_on_human` PURELY
    from the aggregate rows already in memory — **zero extra transport ops** (the classifier takes no
    `transport`; assert it against a counting fake). Free is what makes it un-starvable: a section that
    spends no budget cannot be cut by one. `--on-user` **TYPES** the block as `blocked_on: user:<name>`
    (additive — legacy plain values still parse) so the human case classifies at zero cost; a legacy plain
    `blocked_on` resolves human-vs-agent against the caller's already-loaded identity set (row
    assignees/owners + held roles), and **ambiguity resolves toward SURFACING** — a value that is not a
    known agent/role is shown (with a degraded note when the identity set itself is UNKNOWN), because a
    hidden human-blocked item is the incident and a false positive is only noise.
  - **The caller's own reviews are the review-fold head, on a budget earlier legs cannot have spent.**
    `_pending_reviews_for` derives the caller-assigned review slugs for free from the review-request
    directive rows (`REVIEW REQUEST: <slug>`, assignee = the reviewer) and scans them FIRST under a
    DEDICATED `deadline_seconds`, NOT the shared briefing budget's drained remainder. This is the fix for
    the live `scanned 0/207`: the review leg used to inherit only what presence + role-fold + inbox left of
    the shared budget, so on a busy board it started already expired and never scanned even a three-day-old
    review the caller owed. The tail keeps the shared (clamped) budget; truncating it is expected and
    reports `review-fold-degraded`. A head that STILL cannot complete is UNKNOWN and gets its OWN loud,
    DISTINCT marker `review-head-degraded` — never conflated with the expected tail truncation, never a
    silent skip. **A head slug is UNKNOWN on ANY non-complete outcome — a budget cut, an unreadable review
    doc, a per-slug `TransportError`, OR a caller-directive slug absent from the listing (fail closed;
    negative membership in a listing is not proof the obligation is gone) — and every one produces
    `review-head-degraded` (the missing-from-listing slugs named in a `missing` field so the caller can
    act). Only the caller's OWN head owes this; a clean head with a merely truncated tail must NOT raise a
    false head alarm.** **The two markers carry PHASE-LOCAL counts and never borrow each other's numbers:
    `review-head-degraded`'s `scanned`/`total`/`skipped` summarise HEAD work alone — and `total` counts
    EVERY caller head obligation including the missing-from-listing slugs (so an UNKNOWN reads `0/1`, never
    `0/0` or `1/1`, which would imply nothing-to-scan or fully-scanned) — while `review-fold-degraded`
    counts TAIL work alone and is emitted ONLY on real tail degradation (a budget cut mid-tail or an
    unreadable TAIL slug). A HEAD-only incident emits `review-head-degraded` and NOTHING else — never a
    phantom tail marker with no tail behind it. The head-degraded LINE is cause-neutral (it does NOT say
    "before budget" for an unreadable/missing/transport cause) and appends the specific causes the marker
    carries.** **Ship-gate: a new bounded work-discovery fold puts blocked-on-human and caller-assigned
    work at the head (free where the data is already loaded; a dedicated budget where it is not), proves
    the head completes under a spent shared budget on a live-shaped fixture, and gives "head could not
    complete" a marker distinct from "tail truncated."**
  - **Every marker must RENDER, not just exist: `briefing` and `needs-me` type-dispatch every review row
    type they can receive (`review-pending`, `review-orphan(-degraded)`, `review-role-degraded`,
    `review-fold-degraded`, `review-head-degraded`) through ONE shared helper (`_review_row_line`), so an
    identical row type can never diverge between the two verbs.** An unknown/typeless row must NEVER reach
    the generic task line (`_line`), whose `priority`/`status`/`title` lookups print `[ ?] ? None` on a
    marker shape; a degraded/UNKNOWN marker (head or tail) is always shown and NEVER counted as a pending
    item. **Ship-gate: a new review row type is added to the shared dispatch with a red-first test that the
    text output shows its real line (never `[ ?]`/`None`) in BOTH verbs, and that a UNKNOWN marker is not
    tallied as a pending item.**
- **Role routing is the same contract, one layer in — a role you hold is an address.** A directive
  assigned to a ROLE is directed at whoever holds a fresh lease on it, so `briefing`, `inbox`,
  `needs-me`, and `listen` all fold role-routed work into the holder's queue (that is what makes
  role-based identity outlive a session). `roles claim <team> <role> -s/--summary <text>` records the
  holder's current role-work summary on the lease, parallel to `presence beat --summary`. ONE resolver:
  `cli._held_roles_for_rows` — never resolve
  roles a second way, or the folds silently disagree about a lease. It returns `(held, unresolved)`,
  and **`unresolved` is the load-bearing half**: a role whose lease state is UNKNOWN (transport
  failure, unreadable lease shard, a role doc that is listed but missing/truncated/**unparseable**, an
  **explicitly invalid `sla_hours`**, or a **budget cut** leaving a candidate unscanned or scanned
  partway — see `_role_fresh_holders`) is neither held nor not-held. Folding it into an empty held-set renders a clean, role-blind queue that
  is **indistinguishable from "you have no role work"** — the same silent failure as a clean-empty
  read, and worse, because the doc promise above would then be true-except-when-it-silently-isn't.
  Every caller surfaces it as `_role_degraded_row` = `{"type": "role-degraded", "roles": […]}` (a
  `role_degraded` key on the `briefing` bundle; a list element on `inbox`/`needs-me`) plus the text
  line. **Ship-gate: a new fold that answers "what needs this agent" resolves roles through that one
  helper and surfaces `unresolved`, with a red-first test proving a failed lookup is visible.**
  **Only a complete, successfully parsed listing is negative membership evidence.** A failed read and
  a failed parse are the same fact — we don't know what that document says — so neither may answer
  "is this a role" in the negative once the `roles/` listing has said it IS one. The one non-degraded
  absence is a doc miss for a name that listing affirmatively does not contain (the literal-agent-id
  case). The same rule reaches one level further in, to the FIELD: an **explicitly invalid** value is
  UNKNOWN, and a default is never a substitute for a value someone set and got wrong (`sla_hours: abc`
  fed `roles.parse_sla_hours`'s predecessor a 24h window nobody asked for, and every surface then
  answered confidently off it). An **absent or blank** optional field is the opposite case — the
  default IS the stated intent, and treating it as UNKNOWN would degrade every well-formed doc. Fold
  that distinction ONCE, in `roles.py`, and let the callers fail closed on `None`.
  Grep any new fold for a `parse`/`read` failure **or an unusable explicit value** that returns
  something comparing equal to a legitimate state; that is the whole bug class, and it has now hidden
  in this fold four times — the fourth being the WRITE path (`continuity park` via `_held_roles`),
  which the read-fold sweep left behind. There it is worse: `park` runs as a session EXITS, so a
  swallowed listing that read as "you hold no roles" printed *"nothing to park"* and exited 0, silently
  discarding the checkpoint the next session resumes from, with nobody watching. `_held_roles` now
  returns `(held, ok)` and delegates per-role state to `_role_fresh_holders`; on `ok is False` park
  fails **non-zero** and says the checkpoint was NOT written, so the operator can retry while the
  context is still alive. **Ship-gate extends to write paths: a command that ACTS on the roles you hold
  (not just reports them) resolves through the one helper and refuses to act on UNKNOWN rather than
  treating it as "nothing to do".**
  Cost per pass is **`1 + Σ(2 + L_r)` ops** over the roles the open work references (`L_r` = that
  role's lease shards — one per agent that claimed it and never `roles release`-d, so it tracks
  lifetime churn and is unbounded in principle: a role with ten shards is 13 ops, measured). ONE
  `roles/` listing settles which assignees are roles at all, so the literal-agent-id majority costs
  zero reads and a team with no role-addressed open work pays nothing; the prefilter is per-pass,
  never cached across passes (leases change, and a newly-registered role must route on the very next
  fold). Because no op count bounds LATENCY when each op can burn a transport timeout, the pass also
  runs under one cumulative `COORD_ROLE_FOLD_BUDGET` (default 20s) opened ahead of that listing — a
  cut marks every unfinished candidate `unresolved`, never "not held".
- **Presence engagement is an inert, defensively-parsed schema (wake-router W1).** A presence shard MAY
  carry an `engagement` object with exactly four qualified names:
  `engagement.mode` (`resident|session|occasional`), `engagement.until` (`iso8601Z|null`),
  `engagement.state` (`active|lapsed`), `engagement.lapsed_at` (`iso8601Z|null`). **Absent `engagement`
  reads as `resident` + `active` — today's exact behavior**, so every legacy shard is unchanged and a
  `presence beat` with no `--engagement` flag writes NO engagement field (byte-identical legacy shard —
  pinned). A NEW `--engagement session` defaults `until` to beat time + 8h; `--until` is meaningful
  ONLY for `session` (given with any other mode, or with no `--engagement` at all, or in a non-ISO form,
  it is a validation error at rc 2 and nothing is written). **A beat is REFRESH-SAFE and must never
  manufacture liveness.** `presence beat` is called repeatedly (the launchd heartbeat re-beats), so a
  session beat reads its own prior shard first and: (r3 contract) an ABSENT shard (existence
  disproven by one parent listing — the transport's read is None-on-any-failure, so a listing is
  the disambiguator) is a legitimately fresh session; a LISTED-but-unreadable shard, or a failed
  listing, is an UNKNOWN prior and the engagement-carrying beat FAILS CLOSED (rc 1, nothing
  written, "…retry") — a transient read failure must never let fresh active engagement replace a
  sweep-marked lapsed session; a READABLE prior with malformed engagement degrades in
  `parse_engagement` and is treated as fresh (deliberate self-heal). Then: (a) **preserves a continuing session's resolved `until`**, recomputing `beat+8h` ONLY for
  a genuinely new session (no prior session, or a mode change *into* session) — an explicit `--until`
  always wins. Sliding `until` forward on every beat would make a session never lapse, recreating the
  dead-session-looks-alive bug this schema exists to prevent. (b) **never writes `engagement.state` /
  `engagement.lapsed_at` to a non-default value** — those two names are written ONLY by the W3 sweep; a
  beat continuing an existing engagement object carries its prior `state`/`lapsed_at` forward untouched
  (no `lapsed→active` recovery in W1 — that is W2/W3) and initializes them to `active`/`null` only for a
  brand-new session. In W1 both names are otherwise PARSE-ONLY. **The whole schema is inert in W1: every
  fold PARSES engagement but NONE acts on it** — no liveness/vacancy/roster/broadcast decision changes;
  a shard whose engagement says `session` past its `until` yields the IDENTICAL liveness verdict as one
  with no engagement field (the field is carried additively into fold rows, surfaced under `--json`,
  never consulted by `classify`). There is ONE parse seam, `presence.parse_engagement(fm)`, and it is
  **DEFENSIVE by contract**: a non-dict engagement, an unknown `mode`/`state`, an unparseable
  `until`/`lapsed_at`, **or a `session` with no resolved `until`** (a session with no expiry is
  malformed — the write path always resolves one — never a valid never-expiring session) degrades to the
  legacy `resident`/`active` default AND sets a visible `_engagement_degraded` marker — it NEVER raises,
  so one malformed shard cannot break the fold for every other agent. **Ship-gate: any code that reads
  engagement goes through `parse_engagement` (never a raw `fm["engagement"]` dict-walk), and any new
  bad-input class it must survive gets a red-first test proving it degrades-with-marker instead of
  raising. Until the W3 sweep ships, no write path may set `state`/`lapsed_at` to a non-default value.**
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
    general sub-minute exactness claim.) A row projected by an older `row_from_frontmatter` (stamp
    `sv` != current `ROW_SCHEMA_VERSION` — e.g. a pre-text-cap row) is likewise reparsed once, so a
    projection change (like the summaries text cap) self-heals the whole index within one full pass
    rather than waiting for each task to organically change.
  - **A freshness overlay surfaces new docs THIS read.** Every summaries-index fold (`inbox`, `listen`,
    `briefing`, `needs-me`, `board`, `status`) lists the task dir once and unions in any doc written
    since the last reconcile, so a directive delivered between heartbeats surfaces now, not a
    reconcile-period later. It is bounded (`COORD_OVERLAY_CAP` reads, default 16; `COORD_OVERLAY_BUDGET`
    time, default 10s) and **degrades the `inbox` source visibly** when capped, budget-breached, or a
    listed doc is unreadable — capped-but-visible, never silent truncation. A fresh team (no index yet)
    is unchanged.
  - **Acks are folded change-driven, and reuse needs positive evidence.** Listing every ack dir every
    pass costs one op per dir (~280 on the live bus), so reconcile asks the store what changed
    (`/input/v1/file/recent_changes`) since the instant it last provably folded acks through — the ack
    fold's OWN anchor (`acks_folded_through` in summaries.json), not `generated_at` — and re-folds only
    those slugs. A prior `acked_by` is reused ONLY when the store answered and did not name that slug;
    every unknown — no change query, a query error, no anchor, a slug the prior aggregate never carried,
    a changed slug that wouldn't list — falls back to the full fold and logs why. **No false advance:** a
    fold that couldn't read what it meant to leaves the anchor where it was, so the change it missed is
    still inside the next pass's window instead of consumed by this one; a failed listing preserves the
    prior `acked_by` rather than un-acking the task; and the whole-pass fast path declines while that
    anchor is behind `generated_at`, so a quiet beat can't skip the fold that still owes a read. A forced full fold every `COORD_ACKS_FULL_EVERY`
    passes (default 72, ~daily on a 20-min heartbeat) bounds anything the query could miss, and carries the
    orphan-shard GC.
  - **summaries.json is one shared doc written by many hosts at many versions — a top-level key added
    in version N is wiped by any host older than N.** The whole fleet reconciles ONE index, and an older
    host rebuilds the document from the key set it knows and writes it over everyone else's. This is not
    theoretical: it is why `acks_folded_through` (added in v1.6.8) does not survive on the live bus while
    any pre-1.6.8 host still reconciles — its passes delete the anchor, so the change-driven fold above
    silently degrades to a full fold every pass. Since v1.6.9 `build_aggregate` carries unknown top-level
    keys through, which stops the next occurrence of this class but cannot fix a host that predates the
    passthrough. **A new top-level key is live only once the whole fleet is upgraded** — check
    `fleet health` before assuming a fold-state key is doing anything, and never rebuild the aggregate
    from a fixed key set.

  Mechanics (stamping, deterministic cut, the reconcile reuse anchor) live with the engine —
  [`fulcra-agent-reconcile`](skills/fulcra-agent-reconcile/SKILL.md) and
  [`packages/coord-engine`](packages/coord-engine/README.md).
- **`listen` is the engine-owned watcher — don't hand-roll one.** `coord-engine listen <team> --agent
  <you> [--once] [--json]` is the await leg of `tell`: each tick it id-diffs (not counts) three sources
  against a per-agent state file — new **inbox directives, role-routed ones included** (the SAME fold
  `inbox`/`briefing` now show — a lease handoff re-routes the very next tick), except self-authored
  unscheduled rows: self-tells and your own broadcasts do not wake you; `remind` yourself does, at WHEN,
  new **responses to directives you own** (the reply leg of `respond`), and new **verdicts on reviews you
  requested** (the await leg of `review request`, including the terminal `SETTLED <slug>` line). One event
  line per new item (`DIRECTIVE`/`RESPONSE`/`VERDICT`/`SETTLED`/`ORPHAN`; `--json` = one object per line);
  a quiet tick prints NOTHING. It never advances state over an unread tick (a failed read re-surfaces the
  pending event on recovery) and prints `LISTEN DEGRADED:` to stderr **once per source per streak** across
  five independent sources (`inbox`, `responses`, `orphans`, `verdicts`, `roles`) — so a permanent orphan
  can't pin the flag and silence a fresh outage. `--once` exits **3** when its tick captured degraded
  sources; exit 0 means clean/nothing-new — run it on a scheduler, or bare for a poll loop (`--interval`,
  SIGINT-clean). Every send verb
  arms you with the exact `listen` line to run for replies. The deeper mechanics — the
  orphan/tombstone/unknown
  classification of dir-only review slugs, and the classify budgets (`COORD_LISTEN_CLASSIFY_BUDGET`) —
  live in [`fulcra-agent-automation` §2](skills/fulcra-agent-automation/SKILL.md), the one skill the
  launchd/cron listener, live sessions, Codex, and headless all delegate to. (`review status` on a
  tombstone slug is terminal rc 1 — see [`fulcra-agent-review`](skills/fulcra-agent-review/SKILL.md).)
- **Idle-listener reaping (standing, operator-set 2026-07-20).** An agent whose
  listener has run **2 days (48h) with no work** — no events, directives,
  reviews, or responses surfaced or handled in that window — **parks a
  continuity checkpoint to the bus and stands down its listener**:
  `coord-engine continuity park <team> --agent <self> --objective "<what you
  watch>" --next "resume on directed wake or new assignment"`, then stop the
  poll loop. A directed wake or a new assignment resumes it
  (`continuity resume`). Dormant watchers must not burn compute indefinitely;
  the parked checkpoint loses nothing. Applies to every agent, coord-boss
  included.
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
    `COORD_THREADS_FOLD_BUDGET` (default 30s). A **`threads-degraded` row** (a trailing
    element of the single `--json` array; a stderr notice in text mode) means the fold
    saw only PART of the store (budget breach or an unreadable shard) — sweep or wait,
    **never trust it as complete**. `--json` is ONE array (dropped items + the optional
    degraded element), not JSON-Lines — see the `--json` purity doctrine above. coord-boss runs
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
  first-generation annotations writer, which is now retired (see
  [Fulcra platform surface](#fulcra-platform-surface--records)). Projection
  needs the typed-record writer (`fulcra-common`) installed *beside* coord-engine
  (`uv tool install … --with fulcra-common`); without it the step is an
  explicit exit-0 no-op. Setup + install recipe:
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

## CI and workspace membership

- **macOS CI is path-filtered and bills at 10×**, so it only runs on
  macOS-relevant changes (`packages/fulcra-menubar/**`, `packages/coord-engine/**`,
  and `skills/fulcra-agent-automation/**`). Everything on Linux
  (`uv-workspace.yml`) runs on every push/PR to
  `main`. The upshot: for anything the macOS job skips, the **local gate is the
  real one** — run the relevant suite before you push.
- **Local verification.** `coord-engine` is CI-gated on both runners, but still
  run its pytest suite locally before pushing.
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
- **Records have CLI verbs as of 0.1.37** (2026-07-15) — `fulcra record
  DATA_TYPE [VALUE]` and `fulcra delete DATA_TYPE [RECORD_ID]`, both with
  `-f/--file` and JSON/JSONL on stdin for batch; `fulcra catalog
  --recordable-only` lists the types they accept, and the lib gained
  `record_data_type`/`validate_records`. Use them when you have a shell rather
  than hand-rolling ingest POSTs. The raw ingest endpoints
  (`POST /ingest/v1/record/{data_type}`, typed and preferred; the wrapped
  `DataRecordV1` legacy path; the unpublished `/batch`) are still first-class
  when you need them — the primitives doc covers all three and the custom-type
  `sources` caveat.
- **Records are still append-only. `delete` is a tombstone, not an erasure** —
  the CLI implements it by recording a `DeletedRecord` through the same ingest
  path, and there is no record-delete lib method. There is no hard delete and no
  update/replace verb, so corrections are modeled as new records, not edits:
  write a superseding record, or delete-then-re-record. What 0.1.37 changed is
  availability, not semantics.
- **Projection is the sole timeline-annotation writer.** Use the heartbeat
  projection fold (`coord-engine annotate resolution <team> transitions`). Its
  deterministic ids converge because typed ingest upserts matching explicit
  ids (live-verified 2026-07-14). The retired first-generation writer minted
  different ids and was removed after causing duplicate-record proliferation.

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
- Plugin authors needing independent durable cursors/state use RunContext's
  `kv_get` / `kv_set` / `kv_update` / `kv_delete` API. It is isolated by
  plugin ID and backed by `state.db`; values must be JSON (64 KiB maximum,
  256 UTF-8-byte keys). Use `kv_update` only for quick, side-effect-free atomic
  transforms because it holds SQLite's writer lock while the callback runs.

### launchd PATH gotcha

launchd runs the daemon with a restricted PATH
(`/usr/bin:/bin:/usr/sbin:/sbin`) and does NOT source your shell profile — so
`~/.local/bin` (where `uv tool install fulcra-api` puts the `fulcra` CLI) is
invisible. Any code shelling out to the `fulcra` CLI must resolve it via
`credentials._find_fulcra_cli()` (PATH → `~/.local/bin` → homebrew), **never**
bare `shutil.which("fulcra")`.

**Second-order gotcha (bit the gmail relay twice):** resolving your OWN binary
to an absolute path is not enough if that binary shells out further. The gmail
relay resolves `coord-engine` to an absolute path (`relay.resolve_coord_binary`),
but `coord-engine` ITSELF execs `fulcra-api` by bare name for its bus transport —
so under the daemon's PATH the tell fails `TransportError: … No such file or
directory: 'fulcra-api'` and every relay emit silently no-ops (5-day outage,
07-16→07-21). When you shell out to a tool that itself shells out, pass an
`env` whose `PATH` includes the install dirs (`relay._subprocess_env`), and set
`EnvironmentVariables.PATH` in the daemon's launchd plist. Either fixes it;
keep both so a plist regeneration can't silently reintroduce the outage.

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
