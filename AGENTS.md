# Fulcra Tools — agent guide

Your entry point to this repo: the non-obvious environment and the conventions
you can't infer from the source. The [`README.md`](README.md) tells the
top-level story (what each package is, how to install the pieces) — this file
does not repeat it; it covers what an agent has to know to work here safely.

**This file is a ship-gate artifact.** Every PR that changes agent-facing
behavior — CLI verbs, skills, conventions, environment requirements, review
rules — MUST update this file in the same PR. Reviewers: treat a stale
`AGENTS.md` as a blocking finding. If your change doesn't alter what an agent
needs to know, say so in the PR body ("AGENTS.md: no change needed").

## Where to start

Landing cold? Run the probes top to bottom, then jump to the layer you're
touching. First failing probe is where your setup gap is.

| Probe / question | Command | Passes when | Where to go |
|---|---|---|---|
| Engine + auth usable? | `coord-engine doctor <team>` | exits 0 — tooling present, store reachable | fix the reported gap first (auth: `fulcra auth login`; missing/old `coord-engine`: reinstall) |
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
- **`packages/gmail`** (`fulcra-gmail`) — the local Gmail relay (rebuild of
  ArcBot's unrecoverable MVP). Multi-account, read-only (`gmail.readonly`),
  keyed by opaque `account_id` (email is metadata, never a path/key segment).
  Task 1 ships the client + OAuth account registry: `GmailClient` (Gmail
  REST v1 httpx wrapper, refresh-on-401, fail-soft `invalid_grant`) and the
  `AccountRegistry` (keychain secrets via collect's `credentials` helpers +
  a JSON registry doc; B4 single-use OAuth `state`-nonce → `users.getProfile`
  account binding). Task 2 adds the pure-local processing layer (no daemon,
  no network): `rules` (parse rules, server-`q` builder with 24h overlap /
  7d-or-backfill first-run, and the post-filter effective-match decision with
  privacy-safe reason codes — B2: no subject/from/body ever logged),
  `convert` (Gmail `messages.get(full)` payload → deterministic selected-email
  JSON; attachments = metadata only, bytes deferred to v2), and `ledger`
  (append-only per-account JSONL, fsync per append, torn-line tolerance,
  processed-set keyed by `(message_id, rule_id, rule_version)`, deterministic
  relay outbox key). Task 3 ships the daemon layer: `files_writer` (selected
  email → Fulcra Files at `/collect/gmail/<account_id>/<yyyy-mm>/<message_id>
  .json`; same id → same path, post-crash rewrite is a same-content overwrite),
  `relay` (B3 exactly-once-visible bus relay — a byte-stable coord directive
  keyed by the ledger `outbox_key`, emitted + readback-verified via
  `coord-engine tell`/`search` so retries converge on one visible directive;
  the installed engine's deterministic slug + readback satisfy the B3 version
  pin), `cursors` (per-`(account, rule)` contiguous-frontier watermark),
  `pipeline` (the crash-safe poll: fully paginate → refine to effective matches
  → order oldest-first by `(internalDate, id)` → `file → ledger → relay →
  ledger` → advance the watermark only through the contiguous done prefix), and
  `collect_plugin` (scheduled 15-min plugin; Cloud-Console-clickpath +
  repeatable add-account wizard; per-account health; registered via entry point
  + `_bundled_plugins`), and `collect_routes` (the Gmail-specific add-account
  OAuth endpoints: a start endpoint that mints a nonce + PKCE and 302-redirects
  to Google consent, and the `/api/oauth/callback` endpoint that consumes the
  nonce once, exchanges the code, getProfile-binds, and writes the registry row
  + keychain token — a B4 flow the generic single-namespace oauth route can't
  model; wired optionally into `collect/web.py` and linked from the wizard).
  Codex P1 hardening: an unresolved candidate fetch is a frontier hole (cursor
  never advances past a candidate it couldn't look at); `parse_rules` rejects a
  `relay` action with no `relay_to`, and a relay a rule requires is never
  silently completed when no relay backend is configured (the message stays
  incomplete). Only live OAuth-credential verification is deferred to Task 4.
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

## Coordinate on the bus

Durable work — anything another session or agent must see — lives on the coord
bus (Fulcra Files), driven through `coord-engine` and the `fulcra-agent-*`
skills. Subagent-only work stays OFF the bus.

First time on the bus, or joining from a **remote/sandboxed session** (Claude
Code cloud, CI)? Follow [`docs/coord/GET-ON-THE-BUS.md`](docs/coord/GET-ON-THE-BUS.md)
— it covers the egress allowlist (`fulcra.us.auth0.com`, `api.fulcradynamics.com`),
headless device-flow auth (and the `fulcra auth login` HTTPS_PROXY caveat), team
bootstrap from zero, and the join sequence. The canonical invocation is the bare
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
  verdict file exists at `team/<team>/review/<slug>/verdicts/<reviewer>.md`.
  The request is **atomic**: with the doc landed it also delivers one directive
  per required reviewer through the canonical hash-slug path (so a verb-opened
  review fires each reviewer's inbox/`listen` — never hand-send a review tell),
  and a partial notification failure is reported loud (rc 1) naming exactly which
  reviewers were and were not notified — and is **retryable**: re-running the SAME
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
  bus verdict, keyed by agent identity, is the source of truth. **Verdict
  before ack, on the exact slug — never a bare ack.** Full rules and per-harness
  wiring live in [`fulcra-agent-review`](skills/fulcra-agent-review/SKILL.md)
  and [`fulcra-agent-automation`](skills/fulcra-agent-automation/SKILL.md).
- **Engine surfaces a watcher must honor.** Every directive slug now carries a payload hash
  (`<title-slug>-<sha256(payload)[:8]>`), so identical resends dedupe by construction and distinct
  messages can never share (or clobber) a slot; rc 0 `directive <slug> already delivered` is a *deduped
  identical resend*, not a fresh write, and rc 1 `cannot verify delivery, retry` means the slot was
  unreadable — never overwritten, safe to retry. `briefing`/`needs-me` may emit a `review-fold-degraded`
  row when their pending-review scan exceeds `COORD_REVIEW_FOLD_BUDGET` (default 45s, enforced *within* a
  slug too — checked after each verdict/doc read, so one stalled read can't return a clean row) — honor it
  with a per-slug `review status` sweep;
  never read the fold as complete. The team-global **forge-feedback** section is bounded the same way:
  `briefing`/`needs-me` emit a `forge-degraded` row `{scanned, total, skipped}` when the forge fan-out
  exceeds the shared `COORD_BRIEFING_BUDGET` (default 60s, opened once for the whole add-on stack and spent
  cumulatively; a raised feedback listing counts as `skipped`) — honor it with a `forge feedback` sweep,
  never read the section as complete. The `COORD_BRIEFING_BUDGET` deadline now opens at the TOP of
  `briefing` — before the **presence** section — so it bounds the whole add-on stack (presence + forge +
  resume), not just the forge fan-out: the team-global presence-shard reads (one `list_dir` + a read per
  agent) used to run unbudgeted AND before the deadline even opened, hanging the whole briefing under a
  degraded transport. `briefing` now emits a `presence-degraded` row `{scanned, total, skipped}` (same
  shape family, appended to the `presence` section list — json passthrough + one text line) when the
  presence fan-out breaches the budget, a listed shard is unreadable, or the presence listing raises;
  honor it with a `presence show` sweep, never read a partial roster as complete. The `resume`
  (own-continuity) read shares the same deadline and truncates (stderr note) rather than hang the tail.
  That review sweep itself **fails closed**: `review status` returns rc 1
  (`tally unknown, retry`) when the doc, the verdicts *listing*, or any verdict shard is unreadable,
  rather than printing a partial APPROVED (or self-healing away a legitimate `.settled` marker off a
  tally built over an unlistable prefix) — so a degraded transport can never green-light a merge.
- **Canonical surfaces are live, not reconcile-lagged.** Every fold that reads the summaries index
  (`inbox`, `listen`, `briefing`, `needs-me`, `board`, `status`) also runs a **freshness overlay**: when
  the index is present it lists the task dir once and unions in any task/directive doc written SINCE the
  last reconcile (absent from the index), so a directive delivered between heartbeats surfaces THIS read,
  not up to a reconcile-period later (the PR348 false-clear). Indexed docs are never re-read (the index
  row wins — behavior-preserving); a fresh doc that reads fine but won't parse as a Task is
  skipped-not-fatal, while a LISTED fresh doc that can't be **read** degrades the `inbox` source (the
  listing proved it exists — an unreadable read is a transport problem, never a silent vanish), as does
  a failed overlay listing — visible, never silent, with the index rows still served. Cost: one extra
  `list_dir` per row load plus one read per genuinely-new (new-since-reconcile) slug, **capped at
  `COORD_OVERLAY_CAP` (default 16)** so a sustained reconcile outage can't make every surface-read do
  unbounded doc reads fleet-wide; when capped, the served subset is deterministic (sorted by name, so
  every agent converges on the same subset) — the cut is arbitrary with respect to age or priority (it
  does not surface newest- or highest-priority-first), only stable and reproducible — and the truncation
  itself degrades the `inbox` source with `{served, absent_total}` counts — capped-but-visible, never
  silent truncation. The next reconcile folds the whole set back into the index, so the cap only bites
  during a sustained outage. The overlay is also **time-budgeted** — the cap bounds read COUNT, not
  TIME, and slow per-doc reads (each running toward the transport timeout) must not starve a surface
  read or a watcher tick: `COORD_OVERLAY_BUDGET` (default 10s) opens at overlay start and is checked
  after each read; on breach the overlay stops reading, serves everything read so far plus the index
  rows, and degrades the `inbox` source with `served k of n` counts (when both the budget and the cap
  trip, the budget reason wins — it is what actually stopped the read). A fresh team (no summaries yet)
  is unchanged — the overlay only runs once an index exists.
- **`listen` is the engine-owned watcher — don't hand-roll one.** `coord-engine listen <team> --agent
  <you> [--once] [--json]` is the await leg of `tell`: each tick it id-diffs (not counts) three sources
  against a per-agent state file — new inbox directives **plus directives routed to a role you hold a
  fresh lease on** (a strict SUPERSET of the `inbox` fold; role holders are resolved per tick, only for
  role-shaped assignees on unseen directives, so a lease handoff re-routes the very next tick and the id
  is the directive slug regardless of route — a new holder sees it iff it's unseen in THEIR state), new
  **responses to directives you own** (the reply leg `respond` writes but nothing used to surface), and
  new **verdicts on reviews you requested** (`requested_by == you` — the await leg of `review request`,
  bounded: one review-root listing per tick, requester cached; a `.settled` review first emits its
  unseen verdicts plus one terminal `SETTLED <slug>: APPROVED` line, then is dropped so it is never
  listed again — the settling tick is the standard single-reviewer flow, so the final verdict always
  emits). **Role-expansion asymmetry (deliberate — know which verb expands what):** `listen` expands
  role-held DIRECTIVES; `needs-me`/`briefing` expand roles for pending REVIEWS only (a review whose
  `pending_required` names a role you hold); `inbox` and briefing's inbox section expand nothing (agent
  id and `*` only). The verdicts source also classifies **dir-only review slugs** (a `<slug>/` dir with no
  `<slug>.md` doc) by a **tombstone three-way** (one verdicts listing apiece, so zero extra ops):
  a dir with real verdict `.md` shards is an **orphan** — one cached `ORPHAN <slug>` event / a
  `review-orphan` row every `needs-me`/`briefing` pass, visibility only (repair is a human/maintainer
  action, never auto-delete); an **empty** dir (no shards, or only a stale `.settled` marker whose doc
  is gone) is a soft-delete **tombstone** carrying zero information — silently skipped by the fold,
  listen, and `[?]`/orphan emission (an orphan row here is the wrong ontology, not a pending
  obligation); a verdicts listing that **raises** is **unknown** — fail closed and surface it visibly
  (`review-orphan-degraded` / a degraded `verdicts` source), never assume tombstone on transport
  failure. `review status <slug>` on a tombstone stays rc 1 but says *tombstone (archived/deleted
  review) — no doc, no verdicts* instead of the generic "unknown, retry" (a retry never resurrects a
  gone doc). One
  event line per new item (`DIRECTIVE`/`RESPONSE`/`VERDICT`/`SETTLED`/`ORPHAN`), `--json` for
  one object per line; a quiet tick prints NOTHING
  (streaming-consumer friendly). It never advances state over an unread tick (a failed read re-surfaces
  the pending event on recovery) and prints `LISTEN DEGRADED:` to stderr **once per source per streak** —
  the `inbox` (summaries index), `responses` (responses subtree), `orphans` (a response whose owning
  directive won't resolve), `verdicts` (review root / review doc / verdict shard unreadable), and `roles`
  (a role-lease listing unreadable while resolving role-routed directives — you may be missing role
  work; the engine never reads a failed lease listing as "no holders", see `roles status` rc 1) streaks
  are independent, so a permanent orphan can't pin the flag and silence a fresh transport outage. `--once` **always exits 0** (a tick never fails the schedule; no output means
  nothing new, not an error) — run it on a scheduler, or run bare for a poll loop (`--interval`,
  SIGINT-clean). Every send verb arms you: `tell`/`broadcast`/`remind` print `replies: coord-engine listen
  <team> --agent <sender>` and `review request` prints `await verdicts: …` (both only when the sender
  identity is known — `--from` or `FULCRA_COORD_AGENT`, never the bare host tag), and `respond` confirms
  `the owner's listen surfaces it`. The launchd/cron listener, live sessions, Codex, and headless all
  delegate to this one verb (see [`fulcra-agent-automation` §2](skills/fulcra-agent-automation/SKILL.md)).
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
- **ATC (air-traffic control).** On a subscription-cap fleet, consult
  `coord-engine route <team> --needs <tags>` before a dispatch to pick the cheapest
  model that covers the work, and log the outcome after:
  `coord-engine usage log <team> --account <id> --tier <tier> --model <m>
  --task-class <tag> --outcome clean|rework|escalated`. That ledger feeds the
  headroom fold and demotes a model that keeps failing a task class. Rubric and
  routing procedure: [`fulcra-agent-atc`](skills/fulcra-agent-atc/SKILL.md).
- **Timeline projection (opt-in).** `coord-engine annotate resolution <team>
  transitions` (default `off`) makes the heartbeat project task transitions onto
  your Fulcra timeline model-free, right after each reconcile; `annotate status
  <team>` shows the level + cursor. It is the successor to the legacy
  `fulcra-coord annotations` writer — enabling it requires that writer stay off
  (see [Fulcra platform surface](#fulcra-platform-surface--records)). Setup:
  [`fulcra-agent-automation`](skills/fulcra-agent-automation/SKILL.md).

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
  fold** (`coord-engine annotate resolution <team> transitions`); the two write
  the same Agent-Tasks moments to a no-dedup endpoint, so enabling projection
  requires this writer stay off — use projection for timeline annotations, never
  this writer.

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
