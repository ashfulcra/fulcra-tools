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
| On the bus? | `coord-engine queue <team> --agent <you>` ([bus v3](docs/coord/BUS-V3.md); raw `get-records` if the engine predates v1.7.0), then `coord-engine briefing <team> --agent <you>` for the durable board | queue read completes — and if it stages a `queue-delivery` token (cursor schema v2), the probe passes only after every record is classified and `queue commit` succeeds; briefing prints your identity, role inboxes, reviews owed | [Coordinate on the bus](#coordinate-on-the-bus) — events are the wake surface, the fold is the full picture |
| Two identities joined end to end? | `coord-engine acceptance pair <team> --agent <A> --peer <B>` | every timed hop prints `HOP N PASS` and the command ends `PASS pair A<->B`; any bad nonce, missing checkpoint, stale resume, degraded queue, or failed write exits at `FAILED AT HOP N` with raw evidence | [`GET-ON-THE-BUS.md`](docs/coord/GET-ON-THE-BUS.md#prove-a-two-identity-join-end-to-end) |
| Own worktree? | `git worktree list` | your cwd is a dedicated worktree, not a shared checkout (no conflict markers or foreign staged files) | [Working tree](#working-tree) — carve your own before committing |
| Touching Collect / the daemon? | — | — | [The daemon (Collect)](#the-daemon-collect) |
| Touching coord conventions? | — | — | [Coordinate on the bus](#coordinate-on-the-bus) |
| Touching the platform surface? | — | — | [Fulcra platform surface & records](#fulcra-platform-surface--records) |
| Touching CI / hooks? | — | — | [CI and workspace membership](#ci-and-workspace-membership) |

## Layout

uv-workspace monorepo, macOS-first. Packages under `packages/`, agent skills
under `skills/`, each package with its own README, build, and tests.

- **Collect** — the local ingest side: `collect` (the daemon: control socket +
  FastAPI onboarding wizard + worker subprocesses), `menubar` (the macOS
  menu-bar app, PyObjC / rumps), `fulcra-common` (shared API client + ingest
  pipeline), plus the importer packages (`dayone`, `csv-importer`,
  `media-helpers`, `attention`, `netflix-skill`, …).
- **`fulcra-media webhook` readiness is a synchronization contract.** Its
  JSON `{"stage":"ready"}` lifecycle line is emitted only after the local
  `/health` endpoint has served a request, not merely after bind/listen. A
  parent may therefore send SIGINT/SIGTERM immediately after reading that
  line; the command must emit its shutdown line and exit cleanly. Do not move
  readiness ahead of the serve-loop health probe — `BaseServer.shutdown()`
  can deadlock if it races `serve_forever()` initialization.
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
- **`packages/purpleair`** (`fulcra-purpleair`) — a `scheduled` / `live_polled`
  Collect plugin polling PurpleAir air-quality sensors (10-min default). Two
  sources: the PurpleAir cloud API (`mode=api`, needs an `api_key` credential +
  `sensor_index`) or a sensor on the LAN (`mode=local`, needs `sensor_ips`, no
  key). Each reading fans out to six per-measure custom **NumericAnnotation**
  tracks (PM2.5, PM10, EPA AQI, Temperature, Humidity, Barometric Pressure); AQI
  is **derived locally** from PM2.5 (EPA piecewise breakpoints, truncate-not-round,
  capped at 500 — neither source reports AQI). Load-bearing facts: definitions
  are found-or-created **per measure** — `resolved_definition_id` caches a single
  id in `state.definition_id`, so the plugin drives it once per measure by
  presetting that slot from its own plugin-KV cache (`definition_ids`).
  Idempotency is **per-reading** via the daemon `claim_dedup_keys` on the
  sensor's own observation timestamp (the typed-ingest endpoint does no
  server-side dedup); a failed POST unclaims so the reading retries. `api_key`
  is an **optional** credential (`Credential(required=False)`) so `mode=local`
  runs without it — the worker only hard-blocks a run on a *required* missing
  credential (`required=True`, the default).
- **Shipping a new plugin in the frozen macOS app** — adding it to the menubar
  Briefcase `requires` is NOT sufficient on its own, but it IS now the only
  list you edit. A monorepo package isn't on PyPI, so the release build must
  also build it a local wheel into `wheelhouse/` and then *prove* it landed
  (Briefcase can exit 0 with an empty `app_packages`). Those two steps derive
  from `packages/menubar/scripts/bundle_manifest.py`, which reads the Briefcase
  `requires` and resolves each workspace package's real import name from its
  own `[tool.hatch.build.targets.wheel] packages` — the mapping is not
  mechanical (`fulcra-media-helpers` → `fulcra_media`, `fulcra-csv-importer` →
  `fulcra_csv`). Do not reintroduce a hand-written package list in
  `build_macos_app.sh`; `test_registry_manifest.py` fails if you do. (This
  drift shipped once: PurpleAir was in `requires` while the wheel-build loop
  and presence guard kept their own lists — caught in PR #455 review.)
- **coord** — the agent-coordination layer. In prose it is **coord**; the
  engine is `packages/coord-engine` (a **stdlib-only** CLI, `coord-engine`),
  and the fourteen `fulcra-agent-*` skills under `skills/` are how an agent
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
  reverses through `task restore` or `review restore`. Note the age thresholds
  above are necessary, not sufficient: tasks and reviews share ONE per-pass
  archive cap and tasks are swept first, so a large task backlog defers review
  archiving indefinitely. When that happens the pass now warns `retention: cap
  reached ... N review slug(s) not examined this pass` — treat that warning as
  "review retention is not running", not as routine throttling.
- The one-shot `migrate` exporter and unused atomic `handoff` convenience verb
  are retired. Reassign live work with `task update --assignee <agent> --next
  "..."`; when another session needs resumable context, write the continuity
  snapshot first and then reassign the task.
- Machine JSON is compact by contract: public non-ATC `--json` documents,
  line-oriented `listen` events, the single-array `threads` result, and
  `_coord/summaries.json` omit insignificant whitespace while preserving parsed
  values and degradation markers. Tests and consumers compare parsed JSON unless
  byte layout is the explicit contract.
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
- No team-particular identity in engine logic. `review restore` gated a whole
  code path on `files != ["codex-reviewer.md"]`, so the verb worked for exactly
  one agent on exactly one team and told everyone else "unexpected archived
  verdict shape". Predicates belong on the SHAPE (how many shards, is there a
  doc), never on whose name is in the filename — the repo generalizes, and the
  team's particulars live on the team's store.
- `continuity snapshot` exits 3 and says so when the write did not persist.
  `transport.write` returns **False** on a transport failure rather than
  raising, and the snapshot path used to capture that into a local, spend it on
  a cosmetic side-effect, and still print `snapshot <id>` and return 0. Found
  live during a store outage. Continuity is the durability mechanism: a park
  reporting success without reaching the store leaves a successor resuming from
  the PREVIOUS checkpoint believing it is current — and that happens exactly
  when the host is in trouble, which is when parking matters most. **Any caller
  of `transport.write` must treat `False` as failure**; it is not a
  Falsy-but-fine return.
- Audit every existing READER before you change what a marker MEANS. A
  marker's meaning is fixed by everything that acts on it, not by the writer's
  intent, so a new state or a new field is a change to every consumer at once.
  This is the twin of the deleter rule that came out of the `.settled` work:
  there, four sites could remove one marker and two greps each found half. Do
  the reader sweep in the same change, and say in the PR which readers you
  checked — "I did not audit the others" is a finding, not a footnote.
- `health`'s continuity audit reads ONE pointer per agent, not every snapshot.
  `continuity snapshot` writes `continuity/LATEST.json` after the snapshot
  itself persists (never before — a pointer to a checkpoint that does not exist
  is worse than no pointer). The audit reads that. It does this because a
  directory listing carries no mtime (upstream register U8) and task dirs are
  unordered slugs, so finding the newest snapshot otherwise costs one read per
  task: measured 203 reads / ~149s for three agents, and `health` was killed at
  240s and again at 590s. **A missing pointer is UNKNOWN, never stale** — it
  means pre-pointer history or a failed pointer write, neither of which is
  evidence an agent stopped checkpointing. One extra listing (no reads)
  separates "has snapshots we cannot date" from "has never checkpointed", and
  only the latter is a finding; `continuity_unknown` reports the rest.
  **A failed pointer update REMOVES the old pointer.** An update that fails
  leaves the previous file in place, and the audit then reads a stale timestamp
  as authoritative and calls an agent who just checkpointed stale — the exact
  false finding the design exists to prevent. With no conditional write, the
  only way to stop a stale cache being believed is to delete it; the pointer is
  a cache, so nothing recoverable is lost. If it can be neither updated nor
  removed, the verb exits 3 and says a wrong answer is queued up. Pointer
  updates are also monotonic (best-effort read-then-write): an older snapshot
  must never move an agent's reported age backwards.
- The `.settled` marker's MERGE EVIDENCE is never overwritten by the tally
  cache. 572 stopped `review status` DELETING it and left the WRITE path
  unguarded, so a settleable tally stamped `state: APPROVED` over
  `state: MERGED` + `merge_sha` — found in production, where the very
  `review status` run to CHECK a closure destroyed it. Refusing costs nothing:
  the cache exists so the fan-out fold can skip the slug, and a MERGED marker
  already makes it skip. **Both the delete AND the write need the guard**;
  fixing one is the neighbour trap. And the write overwrites ONLY a positively
  identified CACHE or a positively ABSENT marker — an UNKNOWN one (unreadable,
  unrecognised `state:`, or a FUTURE schema) is preserved and reported, because
  a marker this build cannot classify is not a marker it can prove is its own
  disposable cache. A `review-settled/v2` marker written by a newer build must
  survive an older build's refresh.
- Date/clock tests: a module that fixes a top-level `NOW` for its data must also
  **pin the clock** — an autouse `monkeypatch.setattr(cli, "_now", ...)` to a
  `PINNED_NOW` at/just after `NOW` (template: `tests/test_threads.py`), deriving
  relative ages from `PINNED_NOW`, never asserting against the real clock.
  Otherwise the suite flips red once wall-clock passes `NOW + window`. Enforced
  by `tests/test_clock_pin_convention.py`.
- `escalate` never addresses a role's vacancy notice to the party who lapsed.
  When a role's registered `maintainer:` is also one of its own lease holders,
  the alarm about an absence lands in the absent one's bucket with no exit —
  observed live as three daily ROLE VACANT directives nobody could receive. The
  notice is still written and still counted; it is REPORTED (stderr, and in the
  directive body so whoever eventually reads the bucket sees it) and never
  rerouted. Rerouting was tried and did harm: it moved a notice off a real
  operator onto the bare `human` default. The engine does not know a better
  addressee than the registry does — fix the role doc's `maintainer:` field.
  The undelivered count is computed from the closed-loop condition on EVERY
  sweep, not only when the directive write is new — otherwise the second run
  finds the existing document, skips the branch, and reports clean while the
  notice is still undeliverable.
  It is also **not** recorded as a delivery: the daily marker is a SUPPRESSOR,
  so writing it for a notice that reached nobody would silence the only
  mechanism that would try again. A closed-loop role re-surfaces every sweep
  (one stderr line and a "suppressed" note — the directive write is guarded, so
  no new document per run), `escalate` reports `undelivered=N` in its envelope,
  and the verb exits 3. There is no carve-out for the configured human: the
  engine only knows a string matched, and flagging is safe in a way rerouting
  was not.
- The `no-team-internals` CI guard PROVES it can fail before it reports clean.
  `scripts/no-team-internals.sh` runs `--self-test` first: it stages a fixture
  carrying a public IP and a session ref, asserts the scan flags both, and only
  then scans the tree. This is not ceremony — the guard's first version wrote
  its IP pattern with `\b`, which POSIX ERE does not support, so `git grep -E`
  matched nothing and the check went green on every PR while being structurally
  incapable of finding the leak class it was written for. **Never use `\b` in a
  `git grep -E` pattern.** A guard's green is only evidence when its red is
  reachable.
- Environment hermeticity: the suite's answer must not depend on **who** runs
  it. `cli.INHERITED_ENV` maps each ambient variable the suite must neutralise
  to a representative value (identity: `FULCRA_COORD_AGENT`,
  `FULCRA_COORD_HUMAN`; channel: `COORD_RECORDS_TYPE`) — ONE mapping, so the
  fixture that clears the keys and the wall that populates them cannot drift.
  The coord-engine conftest clears them for every test, and
  `tests/test_env_hermeticity.py` re-runs the affected files under both an empty
  and a populated environment and requires the same outcome. This is not hypothetical — 25 tests failed **iff** an identity was
  exported, which is line one of every agent's wake prompt, so a green tree
  reported failures for anyone following the documented procedure. A test that
  needs a specific identity or channel sets it in its own body. If you add a
  variable to `INHERITED_ENV`, the wall covers it automatically. A variable
  belongs there when the suite reading it makes the ANSWER depend on who ran it;
  it does NOT belong there when the variable legitimately changes behaviour a
  test is about — record those in `NOT_YET_WALLED` with the measurement, as
  `COORD_TRANSPORT_HTTP` is now. Measure siblings rather than guessing:
  `COORD_RECORDS_API_VERSION` looks like it belongs and leaks nothing.

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
- **Wake router (decision plane — W4/W5).** `coord-engine router run
  <team> [--once]` is the fleet's model-free wake policy: a cursor-based scan
  (deliberately NOT the `listen` fold — structurally immune to the 2026-07-22
  listen-starvation class) with a tie-safe inclusive `>= watermark` rescan +
  durable processed ledger, per-agent policy (interrupt floor, debounce, busy
  deferral, LAPSED reduced-cadence check-ins), and durable state ONLY under
  `team/<team>/_coord/router/`. Candidate source is **feed-first** (E3): the
  `data-updates` feed's `uploaded` events under `task/` (second-granular), with
  the task-directory listing kept as the fail-closed fallback on any feed doubt;
  the cursor/ledger/decide seams are identical across both sources. Enablement is
  explicit per agent in `config.json` — an unconfigured agent is observe-only;
  invalid config routes to the fail-visible `unroutable` lane. The decision plane
  **executes** cloud-reachable adapters (`managed-agents-message`,
  `routine-align`) directly — claim → invoke → delivered/dead-letter with bounded
  retry, at-least-once and safe by the keyed-nudge content rule; host-local
  adapters are enqueued with an `executor` id for the W5.5 thin executor. A
  missing/corrupt cursor restarts observe-only. **Shadow mode (W7):** `router
  shadow arm <team>` writes the `shadow-window.json` marker (recording
  `started_at`, activating the fleet-wide delivery probes); `router run --shadow`
  is read-only — it logs AND persists a decision per directed item to
  `shadow-decisions/` and enqueues/executes nothing, while the live delivery
  paths (listener tick, adapter execution) write `shadow-evidence/` shards at
  delivery success. The acceptance report correlates the two on the idempotency
  key over a ≥48h window (duty-cycle gated). Resident router loops use an
  anchored fixed-rate cadence; externally scheduling per-tick `--once` runs
  inherits that scheduler's throttle semantics and is not the duty-gate
  deployment. **Router state-prefix override.**
  `router run`/`router execute`/`router shadow report` accept `--state-prefix
  <name>` (env `COORD_ROUTER_STATE_PREFIX` is the launchd-friendly fallback; the
  flag wins). Absent both it is BYTE-IDENTICAL to today — the router's own state
  stays at the canonical `team/<team>/_coord/router/`. With an override, the
  router's own cursor-tracked state moves to the SIBLING
  `team/<team>/_coord/router-<name>/` (cursor.json, queue/, delivered/,
  delivered.json, dead-letter/, shadow-decisions/, shadow-marks/) while
  `config.json` (shared enablement policy) and `shadow-evidence/` +
  `shadow-window.json` (the live delivery paths' shared correlation surface, and
  directed-item reads under `task/`) stay CANONICAL. This is what lets one host
  run live delivery at the default prefix and a W7 shadow measurement at an
  override prefix in parallel without a shared-cursor collision (whichever pass
  marks a directed item `processed` first would otherwise starve the other — a
  dropped live wake or a blind measurement). A shadow pass under an override
  reads delivered recency and writes W7 evidence at the CANONICAL prefix (it
  maintains no namespaced delivered view of its own); topology A — shadow on the
  canonical prefix with no override, the running W7 window — is unaffected by
  either the namespaced-evidence or namespaced-delivered concern, since both only
  ever arise in the namespaced live+shadow pairing. Every router path composes
  through the single `router.router_prefix(team, state=…)` resolver; the name is
  charset-validated (`[A-Za-z0-9_.-]+`, rc 2 on violation) so it cannot escape
  the namespace. Contract:
  [`wake-router-PLAN.md`](docs/coord/wake-router-PLAN.md) §2/§2.5 +
  [`wake-router-SPEC.md`](docs/coord/wake-router-SPEC.md) §4 +
  [`wake-router-ADDENDUM-1-event-substrate.md`](docs/coord/wake-router-ADDENDUM-1-event-substrate.md) §3.3.
- **Executor queue reads are CONCURRENT (bounded).** A pass prefetches candidate
  queue-entry bodies through `_read_queue_entries` with a bounded pool
  (`QUEUE_PREFETCH_WORKERS`, 8); only the READ phase is parallel, and
  claim/invoke/write stay strictly serial in listing order. This is not a
  micro-optimisation: serial reads made pass time scale with FLEET-WIDE queue
  depth, not with this host's share of it, so the resident executor overran its
  own 60s cadence and skipped every other tick — measured on the VPS as
  `cadence overrun by ~22s — skipped 1 tick(s)`, and against the live store as
  112 entries × ~0.9s = 103.5s serial vs 14.3s prefetched. A raising entry read
  still fails closed (degraded, nothing executed, wakes stay visibly queued);
  the pool must never swallow it into a clean "0 delivered".
- **AN ADAPTER'S SUCCESS PROVES THE ADAPTER RAN — never that the AGENT ran.** The
  router calls an adapter, it exits 0, the pass records `delivered`. Whether that
  constitutes a wake depends on TWO independent questions, and conflating them is
  the error this section exists to stop (codex-reviewer caught the first version of
  this table making exactly that conflation).

  **Axis 1 — what does adapter success actually prove?**

  | class | adapters | success proves |
  |---|---|---|
  | DIRECT INVOKE | `managed-agents-message`, `codex-exec-resume` | the model session was re-entered |
  | INDIRECT (queued / alignment) | `queued-wake-file`, `routine-align` | a nudge landed or a schedule was aligned — **not** that the model ran |
  | NOTIFYING | `macos-notify` | a human was shown a banner. Never a wake. |

  `queued-wake-file` writes a file consumed only at a later `SessionStart`
  (`queue_wake_file` and `consume_wake_files` are separate legs). `align_routine`
  records `no_session_created: true` in its own result — it aligns an already
  self-armed Routine and creates nothing. **An INDIRECT adapter is a viable wake
  chain only when its independent consumer — the session loop, the Routine, the
  thread heartbeat — exists AND is verified running.** Delivery evidence covers the
  first hop only.

  `codex-exec-resume` is DIRECT by its specified semantics: it invokes
  `codex exec resume <thread-id>` and re-enters the exact persisted thread, with no
  independent consumer standing between delivery and execution. **Classify axis 1 by
  what the adapter's contract does, never by whether it is wired up here.** That it
  currently ships no host-local script is an axis-2 fact and belongs in the table
  below — an unconfigured DIRECT adapter cannot execute today, which does not make
  its behaviour indirect. Collapsing those two questions is precisely the error this
  section exists to prevent, and it is easy to make in the direction of whichever
  axis you have the louder evidence for.

  **Axis 2 — is there an implementation on the named executor?** Registration and
  implementation are separate, and either half can be missing:

  | adapter | registered in router | host-local script | result |
  |---|---|---|---|
  | `macos-notify` | yes | yes (`SCRIPT_ADAPTERS`) | executes |
  | `codex-exec-resume` | yes | **no** | executor returns `unconfigured`; 60 wakes sat enqueued |
  | `openclaw-post` | yes (+ `endpoint_name`) | **no** | same class, currently un-executable |
  | `opencode-wake` (PR 482) | **no** | yes (script shipped) | `validate_config` would reject the route |

  Three of the five host-local adapters cannot currently execute. A route can
  therefore look configured, validate, enqueue forever, and never deliver — which
  is what the shadow-window audit measured as 112 unattempted wakes.
- **EVERY HARNESS MUST NAME THE THING THAT RE-ENTERS THE AGENT.** Per
  [`docs/coord/HARNESS-MAP.md`](docs/coord/HARNESS-MAP.md), each harness row owns
  a wake mechanism, and it is always the mechanism that *runs the model* — not a
  script that runs beside it:
  - **Claude Code, local desktop** — the wake is a **session-level recurring
    prompt** (the harness's own scheduled-prompt tool). A `launchd` job CANNOT
    wake this harness: it runs shell, not the model, so at best it posts a
    notification or drops a `queued-wake-file` for the session loop to collect.
    `claude -p` can invoke headlessly ONLY where the CLI is authenticated —
    verify with a real `claude -p` before designing on it; unauthenticated it
    fails after minutes, which reads as a hang, not a refusal.
  - **Claude Code, remote/CCR** — a CCR **Routine** on cron; `managed-agents-message`
    resumes the session. Durable across the operator's machine being closed.
  - **Codex CLI** — the codex **thread heartbeat**; `codex-exec-resume`.
  - **OpenClaw** — the OpenClaw **heartbeat**; `openclaw-post`.
  - **Headless launchd/cron hosts** — the schedule IS the agent; there is no
    model to re-enter, so a notifying adapter is meaningless here.

  Doctrine, learned the expensive way: **deleting a working wake to save budget
  costs more than it saves.** coord-maintainer removed its standing session loop
  on 2026-07-24 and replaced it over two weeks with a notifier that woke a human,
  a headless invoke that could not authenticate, and a `--peek` read that never
  consumed — three mechanisms, none of which ran the agent, while every layer
  reported success. Before retiring a wake, name its replacement and prove the
  replacement RUNS THE AGENT.
- **Thin host executor (W5.5).** `coord-engine router execute <team> [--host
  <id>] [--once] [--dry-run]` is the SOLE executor for host-local adapters
  (`codex-exec-resume`, `openclaw-post`, `macos-notify`, `queued-wake-file`). It
  is **policy-free and has no config authority**: it makes no wake decisions (W4
  did, stamping `executor`/`adapter` into the queue entry — the trusted routing
  source) and re-runs no policy — an entry present in the queue executes
  regardless of priority; it reads `config.json` only for the host-resolved
  `adapter_args` routing target. Per pass it drains exactly the queue entries
  whose `executor` matches this host's id (default `_host()`), skips any whose
  delivery-record already exists (**idempotency-keyed skip — never re-invoke**),
  skips a fresh claim held by another process, then **persists its own claim to
  the entry BEFORE invoking** (claim-then-invoke: `claimed_by` names the claiming
  process, `claimed_at` stamps the window) — a claim that does not persist blocks
  the invoke and leaves the entry visibly queued (**no wake without a persisted
  claim**), so the side-effect window is always claimed and the claim-holder owns
  the delivered/dead-letter transition; the claim stays advisory (a stale claim
  never wedges an entry — a resident process still retries its own). It fires the
  sanctioned host-local adapter through one invoker seam, and records the outcome:
  success ⇒ an idempotency-keyed `delivered/` shard
  (the `fold_delivered` view reads it), failure ⇒ bounded retry (`attempts` ≤
  `MAX_DELIVERY_ATTEMPTS`) then a **dead-letter transition** (`{attempts,
  last_error, gave_up_at}` under `dead-letter/`, owned as claim-holder); a
  non-host-local or unknown adapter dead-letters immediately. Delivery is
  **at-least-once and safe by the keyed-nudge content rule** — the wake carries
  no per-event command, so executing the same entry twice converges to one bus
  check (the §2 acceptance test). **Read-contract, fail-visible:** a queue or
  `delivered/` listing that RAISES is UNKNOWN-degraded — reported loudly, no
  execution, wakes stay VISIBLY queued and the command exits non-zero (a dead
  executor never reports a clean "0 delivered"); a per-entry read that is
  None/unparseable is SKIPPED (never invoke on an UNKNOWN entry). The default
  invoker runs a host-provisioned adapter SCRIPT (below); on an un-provisioned
  host it reports `unconfigured` and the command wakes nothing —
  **DEPLOYMENT (provisioning the scripts and scheduling the poller on a host) is
  a separate Ash-gated step.**
  Contract: [`wake-router-PLAN.md`](docs/coord/wake-router-PLAN.md) W5.5 + §2.
- **Host-local wake adapters are provisioned SCRIPTS behind one invoker seam.**
  `_default_host_adapter_invoke` → `wake_adapters.run_script_adapter` resolves
  `$COORD_WAKE_ADAPTER_DIR/<adapter>.sh` and returns
  **`delivered` (exit 0) | `failed` (non-zero, un-spawnable, or timeout) |
  `unconfigured`**. Three rules are load-bearing:
  - **Nudge-only content (plan §2).** The adapter receives EXACTLY
    `--agent <id> --key <idempotency-key> --reason <wake_adapters.NUDGE_REASON>`
    — the reason is a module constant, and no other field of the invocation is
    read. No per-event command, shell, URL or payload can reach an adapter, so
    at-least-once delivery converges to one bus check. An agent id or key
    outside the accepted charset is refused **before** the script runs.
  - **Bounded.** The script runs under `COORD_WAKE_ADAPTER_TIMEOUT` (default
    10s) via `transport.run_bounded`, which SIGKILLs the whole process group at
    the bound; a hung adapter is reported `failed` and can never wedge the
    executor.
  - **Absent script ⇒ `unconfigured`, never a silent drop.** Env unset, no
    `<adapter>.sh`, or present-but-not-executable all leave the wake VISIBLY
    QUEUED with no retry burned. `COORD_WAKE_ADAPTER_DIR` is unset by default,
    which is why an installed engine fires nothing.
  The first (and currently only) script is
  [`skills/fulcra-agent-automation/scripts/wake/macos-notify.sh`](skills/fulcra-agent-automation/scripts/wake/macos-notify.sh):
  it posts ONE desktop notification via `osascript`, handing the text to a fixed
  `on run argv` AppleScript program as ARGUMENTS (nothing is interpolated into
  source), exits 127 with a clear message where `osascript` is unavailable, and
  **displays text and starts nothing** — no session spawn, no network, no
  interpreter. Every other adapter in `router.ADAPTERS_HOST_LOCAL`
  (`codex-exec-resume`, `openclaw-post`, `queued-wake-file`) still reports
  `unconfigured` until its script lands (W6). Adding this adapter **enables no
  agent, authors no `config.json`, installs nothing and schedules nothing.**
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
- **On wake, read your event queue first — bus v3.** One bounded
  `get-records` query against the team's coordination annotation
  ([`docs/coord/BUS-V3.md`](docs/coord/BUS-V3.md)): dedupe by record id, keep
  `v:1` payloads addressed to you or `all`, fetch documents by `ptr`, fail
  closed on any error or truncation (an unreadable window is UNKNOWN, never
  empty). **Terminal read states are DATA / CLEAR / ABSENT / UNKNOWN /
  INVALID — and INVALID is now in code, end to end.** A read that succeeds
  at transport level but yields malformed bytes (corrupt records config,
  partially versioned authority, unparseable cursor) classifies INVALID:
  human-fixable, fail closed, rc 3 with `error_code=*-invalid`. INVALID is
  never ABSENT (the engine refuses to auto-recreate over a corrupt document
  — the bytes are the evidence) and never UNKNOWN (a retry will not fix a
  corrupt file; `*-read-failed` means retry). Under `--json`, success is
  exactly one `queue-result` object (state DATA|CLEAR) and EVERY nonzero
  exit of `queue`/`queue commit` exactly one `queue-error` object (state
  INVALID|UNKNOWN|INCOMPATIBLE|ABSENT|REFUSED) — same `type` discriminator,
  so empty stdout never means anything (sole exclusion: argparse's own
  usage exits, which fire before queue code runs). A deliberate
  `queue --consume` takeover of another agent's cursor writes a durable
  audit doc under `_coord/audit/consume/` BEFORE reading, and is refused if
  that write fails; plain reads and `--peek` write nothing. The read is
  cheap enough to ride every wake you already have — **do
  not run a polling loop or resident listener for it.** Keep `fulcra-api`
  current whenever you touch coord tooling (same pass, standing rule).
  The durable-obligation fold is **opt-in** (`--obligations`) and reports
  through an additive `obligations` field on that successful envelope: fold
  UNKNOWN/INVALID is a report at rc 0 when the event window itself read
  cleanly; **the fold never changes a successful read's rc**. rc 3 is NOT
  reserved for the read path — nonzero queue-family failures (read and `queue
  commit` alike; commit returns rc 3 for INCOMPATIBLE, stale-token REFUSED, and
  unsupported CAS) keep their own `state`/`error_code` contract. A skip is never
  silent — every machine-readable success envelope that did not fold carries
  `"obligations": {"state": "not-checked"}`, which no caller may map to CLEAR.
  `--no-obligations` stays accepted as a no-op alias for the default; the
  standalone `obligations` verb keeps its own rc 3/4 contract. Queue reads
  also emit `DELIVERY WARNING` lines naming attributed legacy writers whose
  control-looking prose cannot parse as bus-v3 events — those writers believe
  they sent messages that are invisible to the fleet and must adopt latest.
  Prove the write path after any install/upgrade, and whenever a recipient says
  it did not hear you, with `coord-engine doctor <team> --delivery --agent
  <you>`; rc 0 means the stamped probe was written, ingested, and parsed, rc 2
  means the write was refused, and rc 3 means it was written but not proven
  fleet-readable before the deadline.
  Every queue read also compares this engine against the authority's
  `current_engine_version` — the fleet **MINIMUM engine**, not "the pin" — for
  free (the config was already loaded), and prints `queue: ENGINE STALE` when
  the runtime is older; a restored environment snapshot reinstalls old engines
  whose writes modern readers skip.
  **Two different authorities, and they are not the same object.** "The pin" is
  the COMMIT in `_coord/bus-v3/adopt-latest.sh` — what to install.
  `current_engine_version` in `_coord/bus-v3/records.json` is a SEMVER FLOOR —
  the minimum engine the fleet accepts, compared at-or-above. Adoption moves the
  first and cannot touch the second. Until 2026-08-09 both were rendered as "the
  pin", and it cost a real exchange: a pin was cut and adopted, `doctor --self`
  still read `v1.10.0`, and the obvious inference — that the pin had not taken —
  was wrong. Renderings now say "minimum engine" and name the field.
  **PIN-MOVE PROTOCOL (coord-boss, 2026-08-09):** every pin move that ships
  BEHAVIOURAL change evaluates a floor raise **in the same pass**. Otherwise the
  floor silently trails the shipped engine — it sat at `1.10.0` while three
  consecutive pins carried `1.11.0`, and because the check is at-or-above, every
  host rendered `current` and nothing surfaced the drift. Raising the floor is
  also the mechanism that reaches a dark host: it ages into `stale` and adopts
  on its next wake.
  **A FAILED ADOPT MUST NEVER LEAVE THE HOST WORSE THAN IT FOUND IT.** `fulcra-api` is the store
  client — every read and write on the bus goes through it — and `uv tool install --force` DELETES the
  tool environment before reinstalling. On macOS that delete can fail with `Directory not empty (os
  error 66)` AFTER `bin/` is gone, leaving a dangling shim, a directory `uv tool list` calls malformed,
  and no executable: the host is off the bus, and `doctor` then reports the adoption authority
  unreadable, aiming the next diagnosis at the store rather than at the script that just broke it.
  Measured 2026-08-10 on a host running this script as a pin's own acceptance test, and NOT exotic —
  every macOS uv host runs that leg on every NEW pin, because the sentinel fast path only skips when
  the host is already AT the pin being adopted, which is never true the moment a pin moves. So: never
  force-reinstall a capability that works (upgrade in place, where failure costs nothing because the
  working copy survives), force-install only when there is nothing to lose, self-heal the
  half-removed directory once rather than cascading into fallbacks that cannot help, and still FAIL
  when the retry fails — a rescue that claims success it did not achieve is the worse bug.
  **TWO-OS ACCEPTANCE RUN, alongside the floor-raise step (coord-boss ruling, 2026-08-10):** before any
  pin broadcast, the candidate script is run end to end on at least one macOS host and one Linux host,
  publisher and delegate splitting the work as needed. The prior publish ran only on Linux, where this
  bug cannot fire; the run-before-publish is what caught it, and it is now the named standard rather
  than a habit. One OS is not coverage when the failure is OS-specific — and you cannot know which
  failures are OS-specific in advance, which is the whole argument.
  **A PRE-PUBLISH RUN CANNOT EXIT 0, AND rc 4 IS ITS EXPECTED GREEN.** The claim gate greps `doctor`
  for `matches the fleet pin (<candidate>`, and `doctor` compares the installed build's PEP 610
  `commit_id` against the STORE authority — which correctly still names the OLD pin, because nothing
  but the published script defines "current". So the gate is unsatisfiable until publication, by
  design, and both acceptance hosts hit it. Found independently on both OSes on the first run of this
  protocol (2026-08-10). **The pre-publish PASS criterion is all three of:**
  1. every install leg green, with the build verified by MEASURING
     `…/coord_engine-*.dist-info/direct_url.json` → `vcs_info.commit_id` == the candidate — not by
     reading `doctor`'s prose, which is a different claim;
  2. for a host with a working store client, the never-strip line printed and the client still working
     afterwards;
  3. rc **4** at EXACTLY the currency gate, with no `step FAILED` lines and **no claim written** to
     `_coord/bus-v3/adopted/` — verify the absence in the store, do not infer it from the message.
  Anything else — rc 4 with a failed leg, or a claim that appeared anyway — is a real failure. Read the
  expected rc 4 as green ONLY when the other two legs are independently verified; a run that fails
  early also exits nonzero, and the whole point of this criterion is that those two look alike from
  the exit code alone. Both acceptance hosts end up AHEAD of the published pin, which is expected and
  resolves at publish; do not diagnose them as skewed.
  `coord-engine doctor <team> --self` is the same check on demand and
  is TRI-STATE: rc 0 `current` only when the floor exists, parses, and this
  engine meets it; rc 3 `stale` (run the store's adopt-latest, then re-run);
  rc 2 `unknown` when the config is unreadable or the floor is absent or
  malformed — comparison impossible is not current, so never read rc 2 as
  green. Prefer it to an unconditional restore-and-adopt preamble: repair
  only when it exits nonzero.
  `coord-engine briefing <team> --agent <you>` remains the fold over durable
  state — identity, role inboxes, reviews owed — for when you need the full
  board; honor every degraded row it prints as UNKNOWN.
- **If your harness truncates output, read the verdict off stderr.** `needs-me`
  and `briefing` print an unbounded row list to stdout with their degraded and
  source markers inside it, so a truncating reader can lose exactly the part
  that says whether the read is trustworthy. Both now also emit one compact
  envelope line to **stderr** — `needs-me: N item(s), forge=…, source=…,
  degraded=N, rc=N` — which survives stdout truncation. `needs-me
  --envelope-only` gives you that verdict with no records at all, same rc.
  Trust the envelope's `degraded` and `rc` over a payload you cannot see the end
  of; `degraded>0` or `rc=3` means UNKNOWN, never clear.
- **Bus-v3 convergence is authority-gated, not a rollout convention.** The
  shared `_coord/bus-v3/records.json` atomically declares protocol and cursor
  schema versions, minimum safe reader/writer engine versions, cursor
  generation/activation, and — since 2026-08-07 — the CHANNEL every writer
  resolves. `fulcra-common`'s annotations writer reads `data_type` from it at
  write time and REFUSES rather than falling back to a name lookup: the
  superseded definition is still named `Agent Tasks` and still reads as live
  (its retirement is prose in its description), so a by-name resolve silently
  writes where nobody reads. Never resolve a channel by name. `queue` warns on legacy or mixed writer evidence and
  refuses an unknown/old reader or writer before cursor mutation. Run
  `coord-engine doctor <team>` for the fleet census: presence means actively
  running; a stamped claim means adopted, not necessarily active. Cursor v2
  is physically isolated at
  `_coord/bus-v3/cursors/v2/generation-<N>/<agent>.json`; an old binary may
  continue writing the legacy `_coord/agents/<agent>/records-cursor.json`, but
  can never mutate v2. After activation, legacy activity is a loud health
  signal and never authoritative coverage. Full authority and activation
  contract: [`docs/coord/BUS-V3.md`](docs/coord/BUS-V3.md).
- **Legacy Bus-v3 migration is authority/cursor-only.** Run
  `coord-engine bus-v3 migrate <team> --dry-run` first, then `--apply` only
  after every cursor is `readable-legacy` or `absent`; use repeatable
  `--agent` flags to include known identities with no cursor document.
  Malformed or unreadable state blocks. Apply is idempotent, writes only the
  complete schema-v1 authority, and never rewrites legacy cursors. Task and
  role documents remain intentionally backward-compatible and are not a
  migration target. Its JSON contract is documented in BUS-V3.md: never branch
  on rc alone; read `state` + `error_code`, and treat
  `writes.authority: "ISSUED-BUT-UNPROVEN"` as a write whose read-back did not
  prove the resulting store state.
- **Cursor v2 is transactional: read → process → commit.** Under an activated
  schema-v2 authority, `queue` CAS-stages one pending batch and prints a
  `queue-delivery` token; it does **not** advance coverage. Process every
  surfaced event to a durable terminal classification, then run
  `coord-engine queue commit <team> --agent <you> --token <token> --result
  <record-id>=<completed|blocked|superseded|ignored>` (repeat `--result` for
  every staged event). The classifications are persisted in the bounded cursor
  history; an incomplete or extra set is refused. A crash,
  processing failure, or missing commit replays the identical token and batch;
  a stale token is rejected and a repeated successful commit is idempotent.
  Concurrent wakes serialize at the staged batch instead of racing a
  last-writer-wins cursor. The current Fulcra File Store transport exposes no
  conditional write, so schema v2 remains fail-closed until a transport
  provides a proven `compare_and_swap`; a write/read-back imitation is not
  CAS. Keep the authority on schema v1 until both `doctor` proves every active
  writer is compatible **and** the transport CAS gate passes.
- **The Codex safety-net watch checks its literal inbox before briefing**
  (PR 484). On Codex hosts, the managed heartbeat runs one direct
  `inbox --json` read and then one authoritative `briefing` read; it never
  treats briefing's inbox subsection as a substitute for the direct read, and
  if either surface degrades it uses the documented direct-listing fallback
  before reporting quiet. Deliberate redundancy against a stale or unreadable
  summaries index, kept alongside the v3 queue read as that harness's
  fail-closed backstop.
- **Retired (2026-07-27, operator-ordered): the `listen` watcher as the wake
  surface.** The per-agent `coord-engine listen` loop and its adaptive-cadence
  host listeners existed because discovering work meant walking the file tree;
  the folds compensating for that degraded ~9 ticks in 10 at fleet scale and
  hid work. The v3 queue read (`coord-engine queue`, cursored and fail-closed)
  replaces them. The `listen` verb and its fold machinery (head/tail budgets,
  the feed-first cursor) were REMOVED from the engine on 2026-08-03 (PR #523) —
  invoking the verb is an argparse error, and any surviving
  `team/<team>/_coord/agents/<agent>/listen-state.json` shard is historical
  residue, not a thing to resume. Presence stays **time-dirty** rather than feed-cached: each briefing
  evaluates the bounded roster against the current clock, so an unchanged
  session shard still becomes `LAPSED` when `now >= until`.
- **Review handshake.** Nothing lands without an independent review by a
  *different agent identity* than the author — that review is the control, not
  who clicks merge. Where a forge exists the change goes through a **PR, never
  a direct push to `main`**. The handshake rides the bus, not the forge:
  `coord-engine review request <team> <slug> --of <artifact> [--head <exact-sha>]
  --reviewer <role>`
  opens a durable obligation that sits in the reviewer's `needs-me` until their
  verdict file exists at the exact path the command echoes (the required token
  is the role passed to `--reviewer`, not the holder's own name; that token is
  what the tally credits).
  **One PR has one review slug (`pr-N`), across every push.** Pass the PR URL as
  `--of` and the full 40- or 64-hex commit id as `--head`. Re-requesting that
  same slug/PR/requester/required-set with a NEW head advances the same review
  doc to the next round; verdicts append at
  `verdicts/<head>--<required-token>.md`, and the verdict frontmatter must repeat
  that exact `head`. `review status` folds ONLY the active head, reports its
  `head` + `round`, and ignores superseded-head verdicts without deleting them.
  This keeps exact-head rigor without `pr-N-r2`/`r3` slug ceremony. Legacy or
  non-code reviews may omit `--head` and retain `verdicts/<required-token>.md`.
  **A verdict that cannot be counted is REPORTED, never dropped, and the verb
  exits 3.** Two ways a real verdict goes uncounted, both of which happened:
  a filename whose pre-`--` prefix is not a well-formed head (e.g.
  `2026-08-08--alice.md`) names no round that could ever exist; a KEYED-looking
  shard on a HEADLESS review names a round that cannot exist *there* (the
  predicate takes the review into account, not the filename alone); a `verdict:` token outside
  `review.accepted_vocabulary()` normalises to nothing; and a shard at the
  CURRENT round's path whose own frontmatter attests a DIFFERENT head is
  refused (correctly — otherwise a copied round-1 verdict discharges round 2)
  but must say so. Each message names the rule that skipped it. Either way the old
  behaviour reported `pending_required: [alice]` — not "a file here is
  unreadable" but the affirmative claim that alice had not voted, with her
  verdict sitting in the directory. A superseded-head shard stays silent on
  purpose; making every `--` noisy would train everyone to ignore the warning
  that matters.
  The request is **durable-first, not atomic**: the review doc lands FIRST (that
  doc IS the obligation the tally reads), then the verb delivers one directive
  per required reviewer through the canonical hash-slug path (so a verb-opened
  review fires each reviewer's inbox/`listen` — never hand-send a review tell),
  and a partial notification failure is reported loud (rc 1) naming exactly which
  reviewers were and were not notified — and is **idempotently recoverable**: re-running the SAME
  request (same `of`/`--head`/`--reviewer` set/`--from`) is idempotent recovery, re-notifying
  only the reviewers a prior partial failure dropped (the doc is left byte-unchanged,
  already-delivered directives dedupe rc 0), so no reviewer is stranded by the
  exists-guard; a re-request with a *different* `of`/required-set/requester is a
  loud rc 1 conflict (a changed required set re-opens only via a new slug), while
  a different valid `--head` is the sanctioned next round, and a
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
- **Park for a successor only on pushed-and-verified state.** A continuity
  checkpoint is a promise: never park asserting repo/artifact state you have
  not pushed AND independently verified — `git ls-remote` the exact ref and
  compare the exact hash (a push `--dry-run` only proves write permission,
  not that the remote has your state; the memory of having pushed proves
  nothing). If a migration/import
  is still pending at park time, the snapshot says **`IMPORT NOT DONE`** plus
  the exact recipe and the access prerequisites; it names ONE canonical home
  per artifact, never candidates. The parking agent also writes the role doc
  if it's missing and carries an operator pre-flight checklist (egress, auth
  tap, repo scoping, write perm) so the successor's setup is one pass, not a
  serial discovery-by-failure. Full doctrine + checklist template:
  [`fulcra-agent-continuity`](skills/fulcra-agent-continuity/SKILL.md)
  "Parking for a successor"; cloud repo-scoping wall:
  [`HARNESS-MAP`](docs/coord/HARNESS-MAP.md) wall 11.
- **A lapsed lease is not proof a role is unattended.** `roles status` classifies
  from lease timestamps alone, so its predicate is *"has a lease been renewed"*
  while the alarm reads *"is anybody doing this job"* — those diverged for four
  days (codex-reviewer's lease went stale while it filed verdicts hourly, and
  the sweep filed a false P1 per role per day). Escalation now takes a TRI-STATE
  `attended`: True suppresses it as `LEASE LAPSED, ROLE IS BEING SERVED`, False
  escalates as `UNATTENDED`, and the default None still escalates but must say
  **attendance not checked** rather than assert absence. Pass
  `roles status --check-attendance` (opt-in: one listing per review, budgeted and
  reported as `scanned N/M`) before calling any role unattended.
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
  - **The review/forge legs are projection-first, and they SAY so.** `briefing`/`needs-me` serve the
    reconcile-built `reviews`/`forge` sections of `_coord/summaries.json` in zero extra ops when fresh,
    and emit a trailing `review-source`/`forge-source` row disclosing it (`source: projection` + `as_of`,
    or `source: raw-scan` + the `reason`: stale / incomplete / malformed — duplicate slug rows and
    impossible `settled` combinations are malformed — / unrecognized). A projection that cannot be
    served falls back to the full raw scan LOUDLY; it is never silently served as current. **The
    caller's OWN head slugs are always raw-tallied** regardless of the projection (see the head-budget
    rule below) — the projection answers the tail, never "does this agent still owe a verdict". No
    source row at all means the aggregate carries no projection: the pre-projection raw scan.
    Contract for readers: [`docs/coord/BUS-V3.md`](docs/coord/BUS-V3.md) → "Where a fold's answer came
    from". A `needs-me` raw fallback that cannot finish emits `forge-degraded`, preserves its partial
    rows, and returns rc 3; rc 0 therefore means the forge leg is complete even when its disclosed
    source is `raw-scan`. **Ship-gate: any new projection-served fold emits a source row through the
    shared renderer.**
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
  red-first test that `json.loads(stdout)` yields exactly one value on every degraded path.** The rule is
  now enforced for the whole class by PARSER DISCOVERY, not by a hand-kept list: `test_json_purity.py`
  walks the real parser for every path that accepts `--json` (28 today) and fails until each is either in
  `_JSON_PINNED` (smoke-run under a corrupt index AND all fold budgets squeezed to nothing) or in
  `_JSON_EXEMPT` with a stated reason — and `_JSON_EXEMPT` is **empty today**: all 28 are pinned, with the
  mutating paths driven through their own `--dry-run`/`--once`/`--shadow` modes against an in-memory
  transport. An exemption is a claim to justify in review, not a parking space for a path that was awkward
  to invoke. **A new `--json` path fails the suite until you represent it** —
  and the pinned paths must print SOMETHING, since a verb regressing to silence would otherwise pass a
  parses-if-non-empty check while emitting no result. A hand-kept list is how six pinned verbs and fifteen
  unpinned ones coexisted for weeks, and the widened sweep immediately found a live leak (`headroom --json`
  printed prose on its no-accounts early return).
- **`.settled` carries TWO things and only one of them is disposable.** `state: APPROVED` is a tally
  CACHE (`_write_settled_marker`) — cheap to recompute, safe to drop. `state: MERGED` + `merge_sha` is
  merge EVIDENCE written by `review close`, and **nothing recomputes it**: a merged-but-never-verdicted
  review tallies PENDING forever, because nobody reviews a PR that landed weeks ago. `review status`
  therefore deletes a stale cache but **never** a MERGED marker, and an unreadable marker is left alone
  and an unclassifiable marker is PRESERVED, reported on stderr, and returns **rc 3** — TRI-state, because
  two states conflated "known cache" with "cannot tell" and only a POSITIVELY identified cache may be
  deleted. An unrecognised `state:` from a future writer counts as unclassifiable, not as a cache. Before this guard,
  running `review status` on a retroactively-closed review silently destroyed its closure — a read-only
  diagnostic erasing history. **If you add a writer to a shared marker path, audit every existing
  deleter of it in the same change.**
- **A cache may only bind to evidence it can actually fingerprint, and BOTH readers apply the same
  rule.** The `.settled` tally cache carries an `evidence` digest over the verdict shard NAMES, and a
  reader recomputes it from the current listing — so a cache written from a stale snapshot is ignored by
  construction, whenever it was written, and no delete-ordering is needed. But a name digest can only
  see a change a NAME shows. The plain `<head>--<reviewer>.md` form is permanently supported and
  hand-writable, so it can be **rewritten in place**: APPROVE becomes CHANGES and the name does not
  move. This store exposes no etag and no content hash in a listing (`file stat` carries a `Version:`,
  but that is one op per file — exactly what the cache exists to avoid), and listing mtimes are
  minute-resolution on a 12-hour clock. So **a directory holding any plain shard gets no cache at all**;
  it is folded for real, every time, and append-only directories keep the fast path. Both writers refuse
  to stamp a marker no reader may honour, and both readers — `projection._scan_review_slug` and the
  `needs-me`/`briefing` fan-out — go through ONE decision function, `review.settle_shortcircuit`. The
  fan-out used to skip on marker PRESENCE alone: the same unvalidated short-circuit was fixed in the
  projection and left standing one reader away, where a stale cache hid a pending reviewer's obligation
  entirely. A settled slug now costs ONE read (the marker) rather than zero — what the budget needs is
  that the cost stays O(1) rather than growing with the verdict count. MERGED markers are untouched:
  merge evidence is not a recomputable tally, so it short-circuits unconditionally.
- **The projection's ZERO-OP settled carry must prove its evidence was bindable — the tier ABOVE the
  readers is a reader too.** `build_review_projection` consults `_settled_carry_safe` BEFORE it lists
  the verdicts directory, and that carry used to accept any prior `settled: true` row whose review DOC
  mtime+size were unchanged, on the argument that a settled round is immutable and re-opening rewrites
  the doc. That holds for re-opening at a new head and fails for an in-place rewrite of a plain shard,
  which touches neither the doc nor its metadata — so production reconciliation served a stale APPROVED
  forever without ever reaching `review.settle_shortcircuit`. Rows therefore record `ev_bindable`, and
  only a row carrying `True` may take the zero-op tier; everything else — including every row written
  by a build before the key existed — is demoted to **tier 3's one listing, not a full rescan**, whose
  fingerprint compares name+size+mtime per shard and whose `_shards_minutes_closed` refuses any unclosed
  minute. MERGED rows keep the zero-op tier whatever their shards look like. **When you add a validation
  rule to a reader, enumerate every tier that can answer BEFORE it** — three rounds of this PR each
  fixed one layer and left a sibling one step away.
- **`escalate` attendance: one shared scan, and partial coverage is NOT an incident.** The vacancy
  sweep answers "did a holder file a verdict recently" from ONE `_verdict_activity_index` pass built
  before the role loop, not per role — it used to rebuild a 41-listing scan for every acting role
  (measured 2026-08-08: 47.3s of a 98.2s run, pure transport). The scan is bounded by BOTH a count
  (`budget`, 40) and a wall clock (`COORD_ATTENDANCE_SCAN_BUDGET`, 30s); a count alone cannot bound
  time, which is how it reached 170s+ and timed the watchdog out. The register holds ~412 review dirs,
  so **coverage is always partial by design** and the stderr envelope reports it as `attendance=40/412`
  rather than raising an alarm. **rc 3 is reserved for a WALL-CLOCK cut** — a real anomaly. Durable fix
  for full coverage is projection-side (reconcile already pays the listing cost); it does not carry
  per-reviewer verdict recency today.
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
    `review-fold-degraded`, `review-head-degraded`, `review-source`) through ONE shared helper (`_review_row_line`), so an
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
  context is still alive. A complete fold that proves the agent holds zero fresh roles also exits
  **rc 2** and says `CHECKPOINT NOT WRITTEN`: park success now certifies that at least one checkpoint
  was actually written, so an `&&` chain cannot broadcast a false "parked" result. **Ship-gate extends
  to write paths: a command that ACTS on the roles you hold
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
- **Activity implies liveness (wake-router W1.5).** Every engine bus **WRITE** verb refreshes the
  **actor's** presence timestamp, so a *working* agent is provably live — distinct from a dead session
  whose launchd beat still ticks (W2 consumes this as liveness proof). Membership is a **DENYLIST**:
  every verb refreshes EXCEPT the declared reads (`status`/`board`/`search`/`needs-me`/`briefing`,
  `presence show`, `review status`, `queue`, `health`, `doctor`, `obligations`, `roles status`,
  `continuity resume`) and the W1 `presence beat`. It was an ALLOWLIST of thirteen functions until
  2026-08-09, which could not keep this paragraph's promise — a verb added later was simply absent, and
  absence there is indistinguishable from "this agent is not working". Twenty write verbs had drifted
  outside it (`review close`, `escalate`, `continuity snapshot`/`park`, `roles claim`/`release`,
  `answer`, `bus-v3 send`, `stash push` …), so an agent whose job IS reviewing rendered `stale — nudge`
  while working: measured that day, codex-reviewer showed `stale 6d` having filed a verdict 3.5h earlier,
  and the roster attaches an imperative to that judgement, so it dispatches people, not just labels them.
  **The work axis (2026-08-09).** Verb coverage alone still cannot see an agent whose work never
  passes through a verb — **filing a verdict is not a verb at all** (`review request` prints "file
  verdict at …" and the reviewer writes the shard itself), and report docs are written straight to the
  store. So `presence.liveness` takes a third input: `work_ts` (newest work-artifact time, READ-DERIVED
  by the caller) plus `work_scan`, which is **three-valued** (`WORK_SCAN_NONE|COMPLETE|PARTIAL`):
  **NONE** → byte-identical to before the axis existed, because muting the nudge for callers that never
  opted in would trade a false positive for total signal loss; **COMPLETE** → absence is a real finding,
  so an agent with no artifact is nudged and the row says `no work found`; **PARTIAL** (attempted,
  ran out of budget or hit an unreadable listing) → no imperative at all, not even on a stale finding,
  because the artifact that would refute it may be in the unscanned part. Two states were not enough:
  a partial scan reported as NONE reverts to the legacy nudge, which is the exact false nudge the axis
  prevents. `_work_evidence_index` returns `(index, ok)` and takes an optional deadline — only a scan
  that COMPLETED licenses an absence reading, since `list_dir` cannot tell an empty directory from an
  unreadable one. Both facts always render separately; they are never fused.
  **Who measures (coord-boss ruling 2026-08-09):** `presence show` and `briefing` DO, and BOTH are bounded —
  briefing feeds dispatch and a false nudge is what it must never emit, so its scan shares the add-on
  deadline; `presence show` is a direct command with no add-on stack to borrow from, so it opens its own
  (`COORD_PRESENCE_WORK_BUDGET`, default 20s). Unbounded it listed every review's `verdicts/` directory —
  438 on the live store — synchronously, to decorate a roster (codex-reviewer, 591 r3).
  **Scan order is load-bearing:** agent reports (35 listings) run BEFORE the review sweep (438). With
  reviews first the sweep consumed the entire budget every time — measured after 591 shipped, a 120s
  budget (6x the default) was still incomplete with work attributed to THREE agents, and since PARTIAL
  withholds the nudge that turned the signal off fleet-wide. Reordering took the same 20s budget from
  2 agents to 11. The scan is still PARTIAL at this store size, and it is now the POINTER-LESS FALLBACK only.
  **Per-agent work EVENTS** (`_coord/agents/<agent>/work/<iso>-<digest>.json`, coord-boss GO
  2026-08-09) answer the read-side question without a sweep: one listing plus one read per agent.
  IMMUTABLE ON PURPOSE. The first cut was a single mutable `LATEST-work.json` with a
  read-compare-write monotonic guard, and codex-reviewer reproduced two races (594 r1): an OLDER stamp
  landing last overwrote a newer one, and — worse — the failed-write branch deleted the shared path
  unconditionally and could ERASE a newer pointer another host had just written. The store has no
  conditional or versioned write, so a shared mutable path cannot be defended; the fix is not to have
  one. A writer only CREATES its own event, "newest" is a deterministic fold over ISO-led names, and a
  failed write leaves prior events intact and still true (slightly stale, never wrong). NEVER add a
  delete-on-failure here. Four rules, each a test:
  **(1) ONE write site** — stamped from the 590 activity chokepoint, never per-verb, so pointer coverage
  INHERITS the classification and a newly added write verb stamps by default; **(2) 585/588 refusal
  semantics** — stamped only after the command succeeded (`rc == 0`), a missing/unreadable/corrupt
  pointer is UNKNOWN and never "did nothing", the stamp is monotonic, and a FAILED update DELETES the
  pointer rather than leaving a superseded value to be believed; **(3) transitional** — a pointer-less
  agent reads UNKNOWN and falls back to the sweep, which shrinks toward zero as the fleet writes
  pointers; **(4) attributable** — `kind` + `path` mean a row can say "verdict, 20h" instead of naming
  whichever artifact the scan happened to reach.
  NB the pointer is keyed by the RAW agent name, NOT `tasks.agent_key()`: `_coord/agents/<agent>/` uses
  raw names on the live store (`coord-maintainer` exists, `coord-maintainer-f68406` does not), and
  keying it by the hashed form would file every pointer where no reader lists — a silent no-op that
  fixtures would happily agree with. Either scan
  degrades to PARTIAL on expiry rather than reverting to a nudge. NB `env_float` is a POSITIVE-finite
  knob, so setting a budget to `0` falls back to the default rather than disabling the scan. The **continuity audit deliberately does NOT**: its product
  is checkpoint staleness, not activity, and "working but not snapshotting" is precisely its finding —
  work evidence would mask it. That asymmetry is chosen, not an oversight.
  **Classification is PER-OPERATION, not per-function.** Several handlers serve both a read and a write:
  `queue TEAM` vs `queue commit TEAM` (and `--consume`, which advances ANOTHER agent's cursor),
  `inbox TEAM` vs `inbox --ack`, `digest TEAM` vs `digest --store`. Keyed on the function alone these
  were wrong in BOTH directions at once — `queue commit` recorded durable classifications without
  counting as activity, while `inbox` and `digest` refreshed presence merely by being VIEWED
  (codex-reviewer, 590 r2). `_MIXED_MODE_ACTIVITY` maps such a handler to a predicate over the PARSED
  ARGS, and `_is_activity_invocation(args)` — not the function-only helper — is what dispatch calls.
  **Verdict shards are APPEND-ONLY (coord-boss ruling b99fb8da, 2026-08-10).** Two forms are
  first-class, permanently: `<head>--<reviewer>.md` (hand-writers, unchanged, no migration) and
  `<head>--<reviewer>--<iso>-<digest>.md` (the verb). The verb uses the unique form because this store
  has no create-if-absent and no versioned write, so writing a SHARED name is check-then-write and
  cannot protect evidence — codex-reviewer reproduced a concurrent CHANGES overwritten by APPROVE at
  rc 0 (595 r2). A unique name touches no existing file, closing verb-vs-verb AND verb-vs-hand races
  without a store primitive. **Every register reader folds newest per (head, reviewer)** — `review
  status`, the projection, and anything built on `_tally_from_verdict_entries`; the projection built
  one entry per file and would have let a superseded CHANGES block a review forever. Ties break on the
  name so two hosts folding the same directory always agree. **Supersession is never silent**: the
  fold reports `superseded_verdicts`, because a reader told APPROVED while shards were quietly
  discarded has the same affirmative falsehood everything here is about. A correction is a NEW shard;
  the original evidence stays on disk, which is also the same-head correction path. **The settle CACHE IS BOUND TO ITS EVIDENCE** — it carries a digest of the shard names it folded,
  and every reader recomputes that from the CURRENT listing before honouring it. Deleting a stale
  cache cannot stop another writer recreating it: a `review status` that read the old tally, paused,
  and resumed AFTER a correction landed rewrote `.settled` from its stale snapshot, and readers then
  answered APPROVED while the newest verdict was CHANGES (codex-reviewer, 595 r4). Validation replaces
  ordering — a cache built from different evidence is ignored by construction, whenever it was
  written. A marker with no digest is pre-binding and is not trusted; a `state: MERGED` marker
  summarises a merge rather than the verdict set, so it still short-circuits. **A new verdict also
  INVALIDATES the settle cache** — without that the correction contract is false once a prior result
  settled, because readers short-circuit on the marker and never open the shards (codex-reviewer,
  595 r3). Only the CACHE: a `state: MERGED` marker is evidence a PR landed and survives a late
  verdict, and an unrecognised or unreadable marker fails loud rather than being deleted.
  **Every reader dates a plain shard the same way** — filename ts, then frontmatter ts, then the
  normalized listing MTIME. The projection stopped at frontmatter, so a ts-less plain shard sorted as
  empty there and the two readers disagreed about the same directory.
  **`review verdict` (2026-08-10)** exists so that filing a verdict IS an engine write. It was the one
  act with no verb — `review request` printed a path and the reviewer wrote the shard themselves — so a
  reviewer touched no chokepoint, refreshed no presence, and left no work event. Every liveness fix of
  this cycle was blind to reviewers for that single reason. The verb is SUGAR over the same artifact:
  it writes exactly the canonical `<head>--<reviewer>.md` shard at the printed path, so tally / settle /
  retention see no new shape, and DIRECT shard-writing stays valid — the verb is additive, and its
  ADOPTION is what upgrades a reviewer from invisible to a work event. It REFUSES to overwrite an
  existing verdict: a verdict is evidence a merge may already rest on, and a changed head is a new
  round with its own filename, which is the supported way to revise.
  **Every registered command must be CLASSIFIED**, read or write or mixed, and written down as such:
  `tests/test_activity_covers_every_write_verb.py` walks the real argparse tree and fails on any
  command nobody has classified, in EITHER direction. A regex cannot decide this — `tell`, `reconcile`
  and `task restore` all persist through helpers and show no `transport.write` in their own bodies, so
  a source-scan classifier is confidently wrong. Reads go in `_ACTIVITY_READ_FUNCS`, which is
  COMPLETED AT MODULE END, after the extracted command modules are imported: assembled earlier it can
  only name what `cli.py` defines, and that hole let `headroom`, `route`, `atc report`,
  `annotate status` and `threads` refresh presence merely by being READ (codex-reviewer, 590 r1) —
  manufacturing liveness out of looking at a view, which is the worse direction because it suppresses
  the nudge for an agent who really is gone. The hook lives
  at the **single dispatch chokepoint** (`main`, after `rc = args.func(...)`), keyed on the
  command FUNCTIONS themselves so no verb can be missed, and fires only on `rc == 0`. The **actor is the
  WRITER** — `--from` / `FULCRA_COORD_AGENT` via `_known_sender` (never a target assignee); the anonymous
  `coord-reconcile:<host>` fallback is not a presence identity, so a missing actor or team skips silently.
  Two hard constraints, each pinned red-first: **(1) THROTTLE** — at most ONE presence write per
  `presence.ACTIVITY_REFRESH_INTERVAL` (60s) **per process**, via a module-level `_ACTIVITY_BEAT_MEMO`
  (`actor -> last monotonic time`); N writes inside one interval collapse to exactly one shard write
  (tests inject the monotonic clock, never wall time). **(2) FAILURE ISOLATION** — a refresh failure
  NEVER fails the successful bus write: the read+write is wrapped, a single `presence activity-refresh
  failed: <e>` stderr note is emitted, and no exception escapes and `rc` is untouched. **The refresh is a
  TIMESTAMP BUMP, not a `presence beat` re-run** (it does NOT go through the W1 engagement-resolution
  path): it rewrites ONLY the top-level `timestamp` line and preserves every other byte — `engagement`
  (mode/until/**state/lapsed_at**), workstreams, summary, body — verbatim, so it never slides a session's
  `until` and never writes `state`/`lapsed_at` (W3-owned). **The minimal-beat fallback fires ONLY on
  `list_dir`-CONFIRMED absence** (carrying NO engagement object): because the transport's `read` returns
  `None` on BOTH a missing file and a transient failure, a falsy read is confirmed against the RAISING
  `list_dir` contract and FAILS CLOSED on any UNKNOWN — a listing failure, or a shard present-but-
  unreadable, SKIPS (no write) rather than overwriting a possibly-live shard; only a listing that
  succeeds and does not contain the shard licenses the minimal beat. A present shard with no top-level
  `timestamp:` line likewise skips. Failure isolation never means destructive fallback. **Ship-gate: the
  throttle memo is process-global module state — reset it between
  tests; any new write verb added to the engine must join `_ACTIVITY_WRITE_FUNCS` (or the omission is
  justified), and the preserve-everything-but-timestamp rule stays red-first pinned.**
- **Engagement-aware liveness is a combiner over two ORTHOGONAL axes (wake-router W2).** `classify(ts,
  now)` stays PURE — freshness only (`live`/`idle`/`stale`), a function of the timestamp alone — and is
  NOT changed (it is called widely under that contract). The truth table is layered on top by
  `presence.liveness(shard, now=…)`, which returns `{state, freshness, annotation, engagement}`: it reads
  freshness from `classify` and mode/until/state from `parse_engagement`, then applies the authored table.
  **STALENESS** (timestamp freshness — post-W1.5 a working agent's bus write already refreshes it, so a
  fresh timestamp IS "recent activity"; no separate signal) and **DORMANCY** (a `session` past its `until`,
  `now ≥ until` boundary-inclusive, OR a durable W3 `engagement.state: lapsed` marker) are INDEPENDENT and
  rendered as two facts, NEVER a merged label. A dormant shard renders primary state **LAPSED** — distinct
  from stale/dead, EXPLAINED ("declared session window ended"), ROLE-RETAINING — with the freshness axis in
  the annotation: "still beating … — extend session or release" when fresh (a session overrunning its
  window while beating is honestly **LAPSED+active**, a nudge to extend, NEVER silently live), "stale Nh"
  when not. A degraded engagement reads as the legacy `resident`/`active` default and can therefore never
  manufacture dormancy. **CONCUR conditions in output:** any stale row shows a beat nudge (`stale Nh —
  nudge`); all agent lookups match by EXACT id (no substring/fuzzy — the corrupt-id lesson), incl.
  `presence.lapsed_holder`; dormancy ⊥ staleness stays two facts. **The verdict rides ADDITIVELY:** roster
  rows keep `liveness` = the pure freshness band (every existing caller — broadcast_roster, briefing,
  agents_digest — reads it and its meaning must not shift) and gain `state`/`freshness`/`annotation`;
  `presence show` and `agents` render `state` + annotation; `--json` carries all three. **`engagement gate
  <team>`** is the deterministic mixed-fleet gate (plan §3): for every **LIVE** roster agent, COVERED iff
  it beats with a well-formed `engagement` field OR appears in the operator map
  `_coord/router/engagement-defaults.json`; else UNCOVERED. Stale/idle/dead shards never block; overall
  **PASS** iff all live agents COVERED, else **BLOCKED**. **READ-CONTRACT LENS (mandatory — this bit W1
  and W1.5, and it bit the gate's ROSTER read too):** `transport.read` returns `None` on BOTH a missing
  file AND a transient failure, and `list_dir` degradation must never fold to an empty result, so a falsy
  read/listing is NEVER "confirmed absent/empty" on its own. This governs BOTH reads the gate makes, and
  an UNKNOWN on either is **DEGRADED** (fail closed — never PASS on unknown coverage): **(a) defaults**
  (`_load_engagement_defaults`) — router-dir lists without the file ⇒ genuinely absent ⇒ empty map +
  PASS-eligible; file listed but read `None`, body unparseable, or the listing raises ⇒ UNKNOWN. **(b) the
  presence ROSTER** (`_presence_shards_status`, a sibling of `_presence_shards` that PRESERVES degradation
  rather than swallowing a listing `TransportError` to `[]`) — the gate CERTIFIES the roster population, so
  an UNKNOWN roster read must never look like a confirmed-empty one (an empty gate PASSes vacuously —
  fail-OPEN, the P1 codex caught): presence-dir listing raises, a listed shard reads `None`, OR a listed
  shard reads non-empty but its frontmatter will not parse / carries no usable `timestamp` (a parse failure
  is UNKNOWN coverage exactly like an unreadable shard — never synthesize a `{}`/timestampless phantom row,
  which `classify` would fold to stale and SILENTLY EXCLUDE while `ok` stayed True), ⇒ `roster_ok=False` ⇒
  DEGRADED; only a CONFIRMED enumeration (listing succeeded and every live shard parsed with a classifiable
  timestamp) may PASS, and a confirmed-EMPTY roster still PASSes vacuously. `_engagement_gate_passes` routes through
  the same `(shards, ok)` helper, so a degraded roster returns False and the escalation falls back to
  today's behavior, never the suppression branch. **The vacancy/escalation SEMANTIC
  change is gated (§3, dormant today):** LAPSED RENDERING lands unconditionally (additive), but reading a
  LAPSED session role-holder as EXPLAINED ABSENCE (role-retaining, suppress the vacancy escalation WITH a
  note — mirroring `roles.escalation_due`'s dormant-suppress precedent) activates ONLY when
  `_engagement_gate_passes(team)` — `if gate_passes: <lapsed-holder suppression> else: <today's behavior
  verbatim>`. The gate is BLOCKED until the fleet is covered, so the new behavior ships DORMANT; BOTH
  branches are red-first pinned (a gate-PASS fixture suppresses-explained, a gate-BLOCKED fixture escalates
  as today). **Ship-gate: `classify` stays pure (no engagement read); any new dormancy/coverage input
  class gets a red-first test; the gate fails closed on any UNKNOWN; the gated semantic change keeps both
  branches pinned until the gate is satisfied fleet-wide (W10).**
- **The zero-token lapse sweep writes two fields and nothing else (wake-router W3).** `coord-engine
  engagement sweep <team>` is a host-tick, model-free pass that marks a session past its `until` as
  **LAPSED** by writing EXACTLY `engagement.state: lapsed` + `engagement.lapsed_at` (the sweep's
  evaluation time, UTC `…Z`) into the presence shard — the **ONE sanctioned exception** to agent-owned
  presence writes, scoped to those two qualified names. **MARK predicate** (`presence.sweep_decision`,
  the pure read-only seam — reads through `parse_engagement`, never a raw dict-walk): mark iff
  `mode == session` AND `until` present AND `now ≥ until` (boundary-inclusive) AND `state == active` AND
  engagement WELL-FORMED. Otherwise: `resident`/`occasional`/legacy-absent → SKIP (no session, no lapse
  concept); session `now < until` → SKIP `within-until`; `state == lapsed` already → **NOOP** (idempotent
  — a second sweep with no time change writes nothing, pinned with a write-count fake transport);
  degraded/unparseable engagement → SKIP (fail-closed — a malformed shard, e.g. a session missing its
  required `until`, NEVER manufactures a lapse). **The write preserves everything but the two fields:**
  the RAW parsed `engagement` map is mutated (so `mode`/`until` survive byte-for-byte) and re-rendered;
  the top-level `timestamp` is **NOT bumped** (the sweep is not a beat) and `until` is **NOT slid**; the
  body is preserved verbatim (`_split_body_verbatim` keeps the exact tail after the closing `---`, which
  `okf.split_frontmatter`'s `splitlines` would drop). **NEVER parks, NEVER releases roles** (operator
  decision 2026-07-22: park is explicit-only) — only the presence shard is ever written; role leases and
  continuity docs are untouched (blast-radius pinned to the one shard path). **READ-CONTRACT LENS (this
  class bit W1/W1.5/W2 — every read swept):** `transport.read` returns `None` on BOTH missing and
  failure; `list_dir` RAISES on failure. Enumeration via `list_dir`: if it raises, the roster is UNKNOWN
  and the sweep is **DEGRADED** — loud (stderr `DEGRADED` line / `enumeration_ok: false`), rc 1, and it
  must NEVER read as a clean `0 marked` swept roster (that would read as swept-clean). Per shard: read
  `None` / unparseable frontmatter / `_engagement_degraded` → SKIP into the `degraded` bucket (a failed
  read must NEVER cause a write; marking is a WRITE derived only from a CONFIRMED session-past-until-
  active shard). A per-shard write failure is reported and the sweep continues (never aborts). Output:
  `N marked, N already-lapsed, N skipped (bucketed by reason), N degraded`; `--json` returns the
  structured result; `--dry-run` previews would-be-marks and writes nothing. **rc 0 only on a clean
  sweep** (enumeration OK, zero degraded shards, zero write failures); rc 1 on enumeration-degrade, any
  degraded shard, or any write failure. **W4 consumes the marker** (reduced-cadence check-ins); the
  `lapsed → active` clearing happens via an explicit W1 session re-declaration in the beat, never the
  sweep. **Ship-gate: the sweep writes ONLY `state`/`lapsed_at`; the mark predicate stays fail-closed
  (no mark on UNKNOWN/malformed); enumeration-degrade is loud + rc-nonzero, never a silent clean sweep;
  idempotency and the two-field-only / never-park / never-release invariants stay red-first pinned.**
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
  - **A feed delta surfaces new docs THIS read.** On the healthy path, summaries-index folds combine
    the aggregate with team-filtered `data-updates` changes and read only the changed task docs, so a
    directive delivered between heartbeats surfaces now without relisting the task root. If the feed
    or any changed-doc read is doubtful, the legacy freshness overlay lists the task dir once and
    unions in docs written since the last reconcile. That fallback remains bounded
    (`COORD_OVERLAY_CAP` reads, default 16; `COORD_OVERLAY_BUDGET` time, default 10s) and **degrades the
    `inbox` source visibly** when capped, budget-breached, or a listed doc is unreadable —
    capped-but-visible, never silent truncation. A fresh team (no index yet) is unchanged.
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
  - **Reconcile's own pass is a feed delta, not a directory scan (E1).** A reconcile pass consumes
    `data-updates` since a durable cursor (`reconcile_cursor` in summaries.json — watermark +
    processed ledger + an incremental-streak counter, the W4 pattern), reads ONLY the changed task
    shards, and updates the rows in place — so "fresh" means "feed entries since the last pass"
    (0–a handful), not "every doc the index hasn't met". The full `list_dir(task/)` scan stays as
    (a) the fail-closed fallback on ANY cursor/feed doubt (no cursor, corrupt cursor, feed
    unavailable, a feed entry that won't positively parse, a changed shard that won't read) and
    (b) a scheduled drift self-check: every `COORD_RECONCILE_FULL_EVERY` passes (default 72), or once
    the aggregate crosses `MAX_FAST_PATH_HOURS`, a full scan runs and its rows are compared to the
    incremental-maintained view — a divergence is logged LOUD and rebuilt from the full scan, never
    silently absorbed. An incremental row is stamped byte-identically to a full-scan row (size from
    the shard's UTF-8 length, mtime from the feed's `uploaded_at` reformatted to the store's
    minute-granular listing shape), so the fallback stays byte-identical to today. **Ship-gate: a new
    reconcile fast/incremental path takes the full scan on ANY doubt (never a false "nothing changed"),
    keeps a periodic full-scan drift check that rebuilds loudly on divergence, and its cursor key is
    cut from `build_aggregate`'s passthrough and recomputed in full every pass.**
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
- **`listen` is retired (2026-07-27) and REMOVED (2026-08-03, PR #523) —
  don't hand-roll a replacement.** Replies to `tell`/`respond`/`review
  request` arrive as v3 events on the record queue; read it on your next wake.
  The `coord-engine listen` verb no longer exists (its folds were the surface
  that degraded ~9 ticks in 10 at fleet scale and hid work; the send verbs now
  echo `replies:`/`await verdicts:` breadcrumbs pointing at `queue` — see
  [`fulcra-agent-automation` §2](skills/fulcra-agent-automation/SKILL.md)). (`review status` on a tombstone slug
  is terminal rc 1 — see [`fulcra-agent-review`](skills/fulcra-agent-review/SKILL.md).)
- **Idle-agent parking (standing, operator-set 2026-07-20; restated for v3).**
  An agent with **2 days (48h) of no work** — no events, directives, reviews,
  or responses in its queue in that window — **parks a continuity checkpoint
  to the bus**: `coord-engine continuity park <team> --agent <self>
  --objective "<what you watch>" --next "resume on directed wake or new
  assignment"`, and stops any remaining scheduled cadence beyond a coarse
  daily check. A directed wake or a new assignment resumes it
  (`continuity resume`). Dormant watchers must not burn compute indefinitely;
  the parked checkpoint loses nothing. Applies to every agent, coord-boss
  included. `continuity park` exits rc 2 when the agent holds no fresh roles and
  therefore writes no checkpoint; treat that as "not parked", never as a clean
  no-op.
- **`snapshot` is the routine save; `park` means you are LEAVING.** Both write a
  checkpoint, so it is easy to treat them as interchangeable and reach for the
  one a rule happens to name. They are not interchangeable:
  - `continuity snapshot <team> <agent> <slug>` — a PROGRESS save. This is the
    form for `checkpoint-on-every-wake`: you did material work, you are still
    here, here is the state you left.
  - `continuity park <team> --agent <self>` — a SESSION-EXIT save (its own help
    says so): you are going away, so snapshot every held role at once and set
    the `checkpoint_ref`s. Use it at genuine handoff, at dormancy (above), and
    on the wake you `roles claim` so the new role starts with a checkpoint.
    **Do not park on every wake** — it announces a handoff you are not making,
    repeatedly, and drains the meaning from the one signal that should mean
    "this agent has stepped away".
- **park's rc 2 proves only that NO FRESH HELD ROLE WAS FOUND — nothing more.**
  That one exit code covers two opposite situations and the fix differs:
  - **An assigned/expected role whose lease lapsed** -> `roles claim`. Left
    unfixed this is expensive: the role sits VACANT past its SLA, work routed to
    the ROLE (rather than to your agent name) reaches no holder, and an
    escalation-due marker accumulates that nobody owns. Observed live 2026-08-05
    — coord-maintainer ran a day past SLA on a vacant role after reading rc 2 as
    a fact about itself rather than about its lease.
  - **Intentionally role-less** (workers, dispatchables — the steady state for
    most agents) -> use `continuity snapshot` for progress saves. **Do NOT
    fabricate or claim a role merely to make `park` succeed.** An arbitrary claim
    on an exclusive role is worse than the rc 2 it silences.

  Check `roles status <team> <role>` and your assigned role before concluding
  anything: rc 2 is a lease diagnostic, not an identity verdict, and it does not
  by itself tell you which case you are in. `continuity resume` always reports the checkpoint age (human output and
  JSON `checkpoint_age_seconds`); use `--max-age 1h` (durations accept `s`, `m`,
  `h`, or `d` through `999999999d`) when a wake or acceptance run must fail rc
  2 on stale state. JSON `error_code` separates invalid duration, unknown age,
  and stale checkpoints. Up to one second of future clock skew is clamped to
  zero age; farther-future checkpoints fail loud. The no-snapshot JSON shape is
  `{"snapshot":null,"checkpoint_age_seconds":null,"error_code":null}`.
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
- **Blocked-on-operator doctrine — a harness approval-gate is a bus event
  (operator order, 2026-07-23, after the W5 stall).** When work waits on an
  operator/harness approval that only a human can grant (a "nod before you build",
  a deploy sign-off, an entered credential, any approval you cannot self-serve),
  the blocked agent does THREE things the same turn, not one: (1) immediately post
  a **P1 BLOCKED-ON-OPERATOR** shard to coord-boss naming the EXACT approval and
  the EXACT artifact it gates (PR #, slug, host) — so the block is a visible,
  routable bus item, not a private wait; (2) **continue all non-blocked work** —
  the block scopes to the gated artifact, never to the agent; idle-while-blocked
  on one item when other work is ready is itself a failure; (3) **keep beating** —
  a blocked agent is still live and must stay so. **Silence-while-blocked is a
  protocol violation:** never let an operator approval turn into an invisible stall
  (the failure this codifies). The operator's absence is never approval — surface
  the block loudly and persistently, and take the other ready work meanwhile.
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

## Fleet bot identity & PAT custody (W9)

**The fleet's machine account is `AnachronixBot`** — a dedicated bot User, distinct
from `ashfulcra` (Ash's personal account) and from `FulcraBot` (reserved for
Fulcra-side repos, operator decision 2026-07-22). Agent-driven GitHub actions run
as the bot so they are **attributable to the fleet, not to Ash personally**; when
you see a push or merge by `AnachronixBot`, an agent did it.

**Custody — TWO homes, read at runtime, never embedded.** The same credential
lives in two places and **both must be rotated together**:

| Home | Where | Holders |
|---|---|---|
| **PRIMARY** | `FLEET_GH_TOKEN` + `FLEET_BOT_NAME` in the **CCR env config** | the four cloud agents: `coord-boss`, `coord-fable-worker`, `coord-opus-worker`, `coord-maintainer` |
| Mac host | macOS keychain, service `FLEET_GH_PAT` (account = host user) | host-local jobs on the resident Mac |

Cloud agents read `FLEET_GH_TOKEN` from their environment; on the Mac host, read
the keychain only at the moment of use:

```bash
GH_TOKEN=$(security find-generic-password -a "$USER" -s FLEET_GH_PAT -w)
```

**Rotating one home only is the trap:** revoking the old PAT after refreshing just
the keychain leaves every cloud agent holding a dead token. The rotation runbook
below updates the CCR envs *before* revocation for exactly this reason.

Hard rules, each one a real leak vector:
- **NEVER put it in a launchd plist `EnvironmentVariables`.** Plists under
  `~/Library/LaunchAgents` are readable; a token there is plaintext at rest. A
  scheduled job that needs it shells out to the keychain *at runtime*.
- **Never** commit it, echo it, paste it into a chat/transcript, or log it. Print
  derived facts (identity, permissions), never the value.
- Agents **cannot mint tokens** — only the operator can. An expired PAT is an
  operator action, never an agent workaround.

**Least privilege (verified 2026-07-24).** Fine-grained, per-repo:
Contents RW + Pull requests RW + Metadata R. **No admin, no workflows** — the
workflows scope would let a bot rewrite CI, which is far more blast radius than
the fleet needs. Confirmed on `ashfulcra/fulcra-tools`: `push: true`,
`admin: false`. A PAT can never exceed its account's own repo access — if a merge
fails with a permissions error, the fix is granting the **bot account** access,
not re-minting the token.

**Rotation.** 90-day expiry; the current token expires **2026-10-22**, with a P1
bus reminder armed for 2026-10-15 (7 days' lead). **The operator runs every step;
agents cannot mint or revoke tokens.** Order matters — the new token is verified
*before* the old one is revoked, so a failed rotation is always recoverable.

**Stage, verify, then promote — never overwrite the incumbent first.** A
fine-grained PAT value is shown exactly once, so overwriting `FLEET_GH_PAT` before
the new token is proven leaves nothing to roll back to (and stashing a copy of the
old value would violate the custody rules above). The incumbent stays untouched
and working until the candidate has passed verification.

1. **Mint** (GitHub UI, signed in as `AnachronixBot`): Settings → Developer
   settings → Personal access tokens → Fine-grained → *Generate new token*. Same
   scopes as the incumbent — **Contents: RW, Pull requests: RW, Metadata: R**, no
   administration, no workflows — same repos, 90-day expiry. Keep the value
   available (password manager / the once-shown page) until step 5.
2. **Stage** it under a *separate* item — the incumbent is not touched. `-w` with
   **no value** prompts, so the token never enters argv or shell history:
   ```bash
   security add-generic-password -a "$USER" -s FLEET_GH_PAT_NEW -w
   ```
3. **Verify the candidate from staging** — prints only derived facts:
   ```bash
   T=$(security find-generic-password -a "$USER" -s FLEET_GH_PAT_NEW -w)
   GH_TOKEN="$T" gh api user --jq .login                     # expect AnachronixBot
   GH_TOKEN="$T" gh api repos/ashfulcra/fulcra-tools \
     --jq '{push: .permissions.push, admin: .permissions.admin}'
   unset T                                    # expect push true, admin false
   ```
   Wrong `login` ⇒ minted on the wrong account. `push: false` ⇒ the **bot
   account** lacks repo access — grant it, don't re-mint.
4. **If step 3 fails, abort cleanly.** The fleet never noticed:
   ```bash
   security delete-generic-password -a "$USER" -s FLEET_GH_PAT_NEW
   ```
   The incumbent is untouched and still working. Fix and retry from step 1.
5. **Promote** — re-enter the *same* value at the prompt:
   ```bash
   security add-generic-password -U -a "$USER" -s FLEET_GH_PAT -w
   ```
   **Do NOT pipe a value into `-w`.** Verified 2026-07-24: a piped
   `-w` creates the item with an **empty** value and still exits 0 — a silent
   empty-credential promotion that looks like success. Always type/paste at the
   interactive prompt.
6. **Re-verify `FLEET_GH_PAT` itself** by re-running step 3 against service
   `FLEET_GH_PAT`. Promotion is a fresh manual paste, so it must be proven too —
   this is what catches the empty-value footgun. If it fails, the staging item
   still holds the verified-good token (readable via Keychain Access): redo
   step 5. Do not proceed until this passes.
7. **Propagate to the PRIMARY home — the cloud agents — BEFORE revoking.** Steps
   2–6 rotate the Mac keychain only; the four cloud agents (`coord-boss`,
   `coord-fable-worker`, `coord-opus-worker`, `coord-maintainer`) still hold the
   **old** token in `FLEET_GH_TOKEN`. Update `FLEET_GH_TOKEN` in each of their CCR
   env configs, then **confirm one real
   cloud push or PR operation succeeds** with the new value. Revoking before this
   step strands the entire cloud fleet on a dead credential — the whole reason
   custody is documented as two homes.
8. **Revoke the old token** — only after step 7 confirms. GitHub UI → previous
   token → *Revoke*. Skipping this leaves a live credential in circulation.
9. **Clean up staging and re-arm:**
   ```bash
   security delete-generic-password -a "$USER" -s FLEET_GH_PAT_NEW
   ```
   Then arm the next rotation reminder (~7 days before the new expiry) and update
   the expiry date recorded above.

Never let the value reach argv, stdout, shell history, a repo file, a launchd
plist, a log, or a chat transcript at any step.

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

### Freshness: run status cannot tell you a source has died

`last_run` / `last_outcome` / `consecutive_failures` all answer **did the plugin
run**. None answers **did it collect anything**. A source whose upstream goes
quiet keeps running, keeps exiting cleanly, and keeps writing
`last_outcome="done"` with zero consecutive failures — so every health signal
stays green while the data stops. Treat a green run as evidence of execution
only, never of freshness.

`freshness.py` supplies the missing half, from two independent clocks:

- `last_yield_at` (daemon clock) — when the plugin last accepted a record.
  Catches "runs fine, produces nothing".
- `newest_item_at` (source clock) — newest SOURCE timestamp ever accepted, and
  **monotonic**, so a backfill accepting older items cannot drag it backwards
  and manufacture a stall. Catches the sibling failure: a plugin that keeps
  writing while upstream is frozen, which a yield-only check calls healthy.

Plugin authors:

- Pass `observed_at=<ISO source timestamp>` to `ctx.annotation(...)` — when the
  thing happened upstream, not when you wrote it. Only the plugin knows this.
  Without it a source can still be monitored for total silence, but not for a
  frozen upstream.
- Declare `freshness=FreshnessExpectation(max_yield_silence=…,
  max_upstream_lag=…)` on your `Plugin` to opt in. **Monitoring is opt-in by
  design**: a bound guessed from `default_interval` would alert constantly on
  legitimately rare sources (a manual importer, a lab result arriving every few
  months), and an alert that cries wolf teaches operators to ignore the one that
  matters. Set `max_upstream_lag` above the source's normal quiet periods.
- A plugin that has never yielded reports `UNKNOWN` — deliberately neither
  healthy nor stale. "We have not looked" must stay distinguishable from "we
  looked and it is fine"; collapsing those is what let a four-day outage read as
  green.

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
