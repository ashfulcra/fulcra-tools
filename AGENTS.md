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

**No one-off fixes on coordination or continuity surfaces** (operator law,
2026-08-14; earned by twelve strict-consumer incidents,
[`OUTPUT-CONTRACT.md`](docs/coord/OUTPUT-CONTRACT.md)). Root-cause every
incident and fix it upstream at the surface that produced it, not with a
defensive patch at the consumer. The models reading these surfaces are smart;
what they need from us is surfaces that are durable.

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
- **`packages/gmail`** (`fulcra-gmail`) — the local, read-only Gmail relay:
  multi-account, crash-safe (append-only ledger + contiguous-frontier
  watermark), landing selected emails in Fulcra Files and relaying matches
  over the coord bus. The OAuth client MUST be an **External, Desktop app**
  client: Internal excludes personal Gmail accounts, and a Web client's
  secret is confidential and unsuitable for a shipped local relay (Google
  treats a Desktop client's secret as non-confidential, which is what lets
  one shared client ship to many installs). The full OAuth clickpath, account
  caps, logging rules, ledger/relay/pipeline design, and the in-plugin rule
  builder live in [`packages/gmail/README.md`](packages/gmail/README.md) —
  read it before touching the relay.
- **`packages/purpleair`** (`fulcra-purpleair`) — a `scheduled`/`live_polled`
  Collect plugin polling PurpleAir air-quality sensors (cloud API or a sensor
  on the LAN), fanning each reading out to per-measure **NumericAnnotation**
  tracks with EPA AQI derived locally from PM2.5; idempotency is per-reading
  via the daemon's `claim_dedup_keys`. Details:
  [`packages/purpleair`](packages/purpleair).
- **Shipping the Safari app to TestFlight** — `packages/attention/safari/scripts/release_testflight.sh`
  does the whole mechanical path (both JS bundles → archive → payload check →
  export → validate → upload) and refuses to start without three things a
  runner must never hold: an **Apple Distribution** certificate, an App Store
  Connect app record for `com.fulcra.attention`, and an ASC API key passed as
  `ASC_KEY_ID` / `ASC_ISSUER_ID` with the `.p8` at
  `~/.appstoreconnect/private_keys/AuthKey_<KEYID>.p8`. **`Developer ID
  Application` is not a substitute** — that signs the notarised `.dmg` for
  direct download, not an App Store build; they are different certificates for
  different channels and assuming otherwise fails late, looking like a
  provisioning problem. `--dry-run` stops after export; any other argument is
  rejected with exit 2 rather than falling through to an upload, because a
  consumed build number is not recoverable. `CURRENT_PROJECT_VERSION` must
  increase on every upload.
- **Running the Safari Swift tests** — `xcodebuild -scheme FulcraAttentionTests
  -destination 'platform=macOS' test`, but **build both JS bundles first**
  (`npm ci && npm run build && npx vite build --config vite.safari.config.ts`
  in `packages/attention/chrome`): the scheme builds the macOS app, which
  embeds the extension, which copies a built bundle as resources, so a clean
  checkout fails on missing files unrelated to the tests. Platform-agnostic
  logic belongs in the **macOS app target** even when only the extension uses
  it at runtime — that is what makes it reachable by `@testable import
  FulcraAttention`.
- **Shipping a new plugin in the frozen macOS app** — the menubar Briefcase
  `requires` is the ONLY list you edit, but it is not sufficient on its own: a
  monorepo package isn't on PyPI, so the release build must also build a local
  wheel into `wheelhouse/` and *prove* it landed (Briefcase can exit 0 with an
  empty `app_packages`). Both steps derive from
  `packages/menubar/scripts/bundle_manifest.py`, which resolves each workspace
  package's real import name from its own hatch config (the mapping is not
  mechanical). Do not reintroduce a hand-written package list in
  `build_macos_app.sh`; `test_registry_manifest.py` fails if you do (this
  drift shipped once — caught in PR #455 review).
- **Shipping a new plugin: document it in the same PR.**
  `docs/how-do-i-get-my-data.md` states that it lists *every* source Collect can
  pull from, so an undocumented plugin makes that opening sentence false. The
  catalogue cites plugins by id in backticks, and
  `packages/collect/tests/test_docs_coverage.py` fails when a registered plugin
  appears nowhere in it — so the test tells you at PR time, not a user months
  later. A plugin that genuinely is not a user-facing source goes in that file's
  `_NOT_USER_FACING` map **with a reason**, which is a deliberate statement, not
  a way to quiet the test. The check is coverage only: it proves the source is
  mentioned, never that the prose is right (this drifted for real: two shipped
  plugins were undocumented and one section affirmatively denied a plugin that
  was registered — the coverage test now catches the first class; only reading
  the prose catches the second).
- **coord** — the agent-coordination layer. In prose it is **coord**; the
  engine is `packages/coord-engine` (a **stdlib-only** CLI, `coord-engine`),
  and the skills under `skills/` — the `fulcra-agent-*` family (e.g.
  `fulcra-agent-review`, `fulcra-agent-continuity`,
  `fulcra-agent-automation`) plus `coordinator-discipline` — are how an agent
  actually drives it. (The `coord2` codename is fully retired — code,
  identifiers, and prose all say coord; installers migrate coord2-era
  on-host artifacts automatically when re-run.)
  The first-generation `fulcra-coord` and `fulcra-coord-files` packages were
  retired after their last live annotations surface moved to `fulcra-common`.
  Their provenance remains in git history; all coordination work uses coord.
- **`packages/coord-tracker-bridge`** — the alpha, provider-neutral projection
  core for reflecting coord work into external trackers (normalized snapshots,
  versioned policy, a pure diff plan, a Linear adapter). Workflow order, lane
  policy, and adapter contracts:
  [`packages/coord-tracker-bridge/README.md`](packages/coord-tracker-bridge/README.md).
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
- The OLD one-shot `migrate` exporter and the unused atomic `handoff`
  convenience verb are retired (`bus-v3 migrate`, described under
  [Coordinate on the bus](#coordinate-on-the-bus), is live and unrelated).
  Reassign live work with `task update --assignee <agent> --next "..."`; when
  another session needs resumable context, write the continuity snapshot first
  and then reassign the task.
- Machine JSON is compact by contract: public non-ATC `--json` documents,
  the single-array `threads` result, and
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
- Run tests: `uv run pytest packages/ -q` (a couple of minutes,
  and must NOT hit the network — a network-bound run is the bug, not slowness).
- Editable install: the `.venv` imports the live workspace source, so a code
  change is picked up by **restarting the daemon**, not re-syncing.
- Pull latest into a checkout with `bash scripts/update.sh` (git pull +
  `uv sync --all-packages --all-extras` + restart daemon/menubar).
- PyObjC-free logic is split into its own modules so tests run on Linux CI;
  macOS view-layer tests are marked and skipped off-darwin. Keep new PyObjC
  imports lazy (inside functions), never at module import time.
- No team-particular identity in engine logic. `review restore` gated a whole
  code path on `files != ["<one-agent>.md"]`, so the verb worked for exactly
  one agent on exactly one team and told everyone else "unexpected archived
  verdict shape". Predicates belong on the SHAPE (how many shards, is there a
  doc), never on whose name is in the filename — the repo generalizes, and the
  team's particulars live on the team's store.
- `continuity snapshot` exits 3 and says so when the write did not persist.
  `transport.write` returns **False** on a transport failure rather than
  raising; the snapshot path once swallowed that and still printed
  `snapshot <id>` at rc 0 — found live during a store outage, leaving a
  successor to resume from the PREVIOUS checkpoint believing it current,
  exactly when parking matters most. **Any caller of `transport.write` must
  treat `False` as failure**; it is not a Falsy-but-fine return.
- Audit every existing READER before you change what a marker MEANS. A
  marker's meaning is fixed by everything that acts on it, not by the writer's
  intent, so a new state or a new field is a change to every consumer at once.
  Do the reader sweep in the same change, and say in the PR which readers you
  checked — "I did not audit the others" is a finding, not a footnote.
- `health`'s continuity audit reads ONE pointer per agent
  (`continuity/LATEST.json`, written only AFTER the snapshot itself persists),
  not every snapshot — a store listing carries no mtime, so the walk is one
  read per task and, measured at fleet scale, hundreds of serial reads that
  repeatedly timed `health` out. **A missing pointer is UNKNOWN, never
  stale** — only "has never checkpointed" (proven by one extra listing) is a
  finding; `continuity_unknown` reports the rest. **A failed pointer update
  REMOVES the old pointer** — with no conditional write, deleting the cache is
  the only way to stop a stale timestamp being believed; if it can be neither
  updated nor removed, the verb exits 3. Pointer updates are monotonic: an
  older snapshot must never move an agent's reported age backwards.
- The `.settled` marker's MERGE EVIDENCE is never overwritten by the tally
  cache: a settleable tally once stamped `state: APPROVED` over
  `state: MERGED` + `merge_sha` — found in production, where the very
  `review status` run to CHECK a closure destroyed it. **Both the delete AND
  the write need the guard** (fixing one is the neighbour trap), and the write
  overwrites ONLY a positively identified CACHE or a positively ABSENT
  marker — an UNKNOWN one (unreadable, unrecognised `state:`, a FUTURE
  schema) is preserved and reported. A `review-settled/v2` marker written by
  a newer build must survive an older build's refresh.
- Date/clock tests: a module that fixes a top-level `NOW` for its data must also
  **pin the clock** — an autouse `monkeypatch.setattr(cli, "_now", ...)` to a
  `PINNED_NOW` at/just after `NOW` (template: `tests/test_threads.py`), deriving
  relative ages from `PINNED_NOW`, never asserting against the real clock.
  Otherwise the suite flips red once wall-clock passes `NOW + window`. Enforced
  by `tests/test_clock_pin_convention.py`.
- `escalate` never addresses a role's vacancy notice to the party who lapsed.
  When a role's registered `maintainer:` is also one of its own lease holders,
  the alarm lands in the absent one's bucket with no exit — observed live as
  daily ROLE VACANT directives nobody could receive. The notice is still
  written and counted; it is REPORTED (stderr + directive body) and never
  rerouted (rerouting was tried and moved a notice off a real operator onto
  the bare `human` default — fix the role doc's `maintainer:` field instead).
  The undelivered count is recomputed on EVERY sweep, the daily marker is a
  SUPPRESSOR and is never written for a notice that reached nobody, a
  closed-loop role re-surfaces every sweep, `escalate` reports
  `undelivered=N`, and the verb exits 3.
- The `no-team-internals` CI guard PROVES it can fail before it reports clean.
  `scripts/no-team-internals.sh` runs `--self-test` first: it stages a fixture
  carrying a public IP and a session ref, asserts the scan flags both, and only
  then scans the tree. Its first version used `\b`, which POSIX ERE does not
  support, so `git grep -E` matched nothing and the check went green on every
  PR while structurally unable to find its leak class. **Never use `\b` in a
  `git grep -E` pattern.** A guard's green is only evidence when its red is
  reachable.
- Environment hermeticity: the suite's answer must not depend on **who** runs
  it. `cli.INHERITED_ENV` maps each ambient variable the suite must neutralise
  to a representative value (identity: `FULCRA_COORD_AGENT`,
  `FULCRA_COORD_HUMAN`; channel: `COORD_RECORDS_TYPE`) — ONE mapping, so the
  fixture that clears the keys and the wall that populates them cannot drift.
  The coord-engine conftest clears them for every test, and
  `tests/test_env_hermeticity.py` re-runs the affected files under both an
  empty and a populated environment and requires the same outcome (dozens of
  tests once failed **iff** an identity was exported — line one of every
  agent's wake prompt). A test that needs a specific identity or channel sets
  it in its own body. A variable belongs in `INHERITED_ENV` when the suite
  reading it makes the ANSWER depend on who ran it; a variable that
  legitimately changes behaviour a test is about goes in `NOT_YET_WALLED`
  with the measurement instead. Measure siblings rather than guessing.

## Coordinate on the bus

Durable work — anything another session or agent must see — lives on the coord
bus (Fulcra Files), driven through `coord-engine` and the `fulcra-agent-*`
skills. Subagent-only work stays OFF the bus.

**IF YOUR MESSAGE ASKS FOR NOTHING, SEND IT `--fyi`.** `tell` mints a `proposed`
row that only the RECIPIENT can close, so a report, an ack or an FYI becomes a
permanent open obligation its assignee cannot discharge — there is nothing to do.
Measured at fleet scale, virtually the entire proposed pile was delivered
messages, not proposals. It is a ratchet, not a hygiene failure: a reply sent
with `--closes` closes its parent AND mints a fresh open row back at the sender,
so two agents who both behave perfectly still net one open row per exchange.
`--fyi` delivers identically — same durable ptr doc, same companion event, same
appearance in the recipient's queue — but the row is born closed and never enters
the open pile. This is the sibling of the merge-closes-review ruling (PR #561)
one plane over: closure belongs to the terminal event, not to a separate
discipline step nobody performs, and a notification's terminal event is its
delivery. Use a plain `tell` when you are genuinely asking for work; use `--fyi`
for everything else.

First time on the bus, or joining from a **remote/sandboxed session** (Claude
Code cloud, CI)? Follow [`docs/coord/GET-ON-THE-BUS.md`](docs/coord/GET-ON-THE-BUS.md)
— it covers the egress allowlist (`fulcra.us.auth0.com`, `api.fulcradynamics.com`),
headless device-flow auth (and the `fulcra auth login` HTTPS_PROXY caveat), the
human-free token-refresh grant, team bootstrap from zero, the join sequence,
role-takeover continuity (`continuity resume` at claim time), and the ephemeral-host
doctrine (survival invariant + heartbeat duty for long-lived remote sessions). The canonical invocation is the bare
`coord-engine` binary after `uv tool install` — `uvx`/`uv tool run` cannot resolve
it (not on PyPI).

- **Named identities**: personas are a convention for human legibility only;
  bus routing always uses the functional id, and the roster lives on the
  team's store.
- **Wake router — ships in the engine, unproven in deployment.** The one
  reference deployment was evaluated and retired (2026-08); scheduled wakes +
  queue reads are the standing pattern (status notes: [`README.md`](README.md)
  and [`docs/coord/BUS-V3.md`](docs/coord/BUS-V3.md)). The contract, should
  you deploy one, is [`wake-router-SPEC.md`](docs/coord/wake-router-SPEC.md) +
  [`wake-router-PLAN.md`](docs/coord/wake-router-PLAN.md); deployment
  (provisioning adapter scripts, scheduling a poller on a host) is a separate,
  operator-gated step. One telemetry shape holds at any scale: serial
  queue-entry reads cost ~7x prefetched (measured at fleet scale), so a reader
  whose pass time scales with fleet-wide queue depth overruns its own
  cadence — bound and prefetch. Two pieces of doctrine outlive any deployment:
  - **AN ADAPTER'S SUCCESS PROVES THE ADAPTER RAN — never that the AGENT
    ran.** Axis 1, what success proves: a DIRECT adapter re-enters the model
    session by its contract; an INDIRECT (queued/alignment) adapter only
    lands a nudge or aligns a schedule, and is a viable wake chain only when
    its independent consumer — session loop, Routine, heartbeat — exists AND
    is verified running; a NOTIFYING adapter shows a human a banner, never a
    wake. Axis 2, is there an implementation on the named executor:
    registration and implementation are separate, so a route can look
    configured, validate, enqueue forever, and never deliver. Classify each
    axis on its own evidence; never collapse them.
  - **EVERY HARNESS MUST NAME THE THING THAT RE-ENTERS THE AGENT** — the
    mechanism that runs the model, never a script beside it (an OS scheduler
    job runs shell, not the model; see
    [`docs/coord/HARNESS-MAP.md`](docs/coord/HARNESS-MAP.md)). And **never
    retire a working wake without naming its replacement and proving the
    replacement RUNS THE AGENT**: a standing session loop was once replaced
    over two weeks by a notifier that woke a human, a headless invoke that
    could not authenticate, and a peek that never consumed — none ran the
    agent, while every layer reported success.
- **Durable tooling stash.** An agent's operational bundle survives ephemeral
  machines via `coord-engine stash push/pull/list`: push refreshes a
  `manifest.json` (per-file sha256 + exec bit) and runs a **fail-closed
  secrets guard** (secret-shaped names and credential-shaped content are
  refused with the tripped rule named — `team/<team>/**` is readable by every
  agent on the bus); pull restores and **fails loud on checksum drift**.
  Procedures: [`fulcra-agent-durable-state`](skills/fulcra-agent-durable-state/SKILL.md).
- **On wake, read your event queue first — bus v3.** One bounded
  `get-records` query against the team's coordination annotation. The full
  read/write contract lives in [`docs/coord/BUS-V3.md`](docs/coord/BUS-V3.md);
  the agent-actionable core:
  - Dedupe by record id, keep `v:1` payloads addressed to you or `all`, fetch
    documents by `ptr`, and fail closed on any error or truncation — **an
    unreadable window is UNKNOWN, never empty**.
  - **Zero fresh events is not proof of zero durable work.** A successful
    text-mode queue read with no event rows and no obligations fold prints a
    stderr notice pointing to `coord-engine obligations <team> --agent <you>`;
    run that verb for the terminal durable-work answer. The notice preserves
    the queue read's rc 0 because its event window was read successfully;
    an unreadable event window still fails closed as UNKNOWN at rc 3.
  - **Terminal read states are DATA / CLEAR / ABSENT / UNKNOWN / INVALID**,
    and rc is never inferred: read `state` + `error_code` from the single
    JSON envelope (under `--json`, success is exactly one `queue-result`
    object and every nonzero exit exactly one `queue-error` object — empty
    stdout never means anything). INVALID (malformed bytes; human-fixable)
    is never ABSENT (the engine refuses to auto-recreate over a corrupt
    document) and never UNKNOWN (`*-read-failed` means retry; a retry never
    fixes a corrupt file).
  - A deliberate `queue --consume` takeover of another agent's cursor writes
    a durable audit doc BEFORE reading and is refused if that write fails;
    plain reads and `--peek` write nothing.
  - The read is cheap enough to ride every wake you already have — **do not
    run a polling loop or resident listener for it.** Keep `fulcra-api`
    current whenever you touch coord tooling (same pass, standing rule).

  Prove the write path after any install/upgrade — and whenever a recipient
  says it did not hear you — with `coord-engine doctor <team> --delivery
  --agent <you>` (rc 0: written, ingested, and parsed; rc 2: write refused;
  rc 3: written but not proven fleet-readable before the deadline).
  **Engine currency.** "The pin" (the commit in the store's `adopt-latest.sh`
  — what to install) and `current_engine_version` in the bus authority (a
  SEMVER FLOOR — the minimum engine the fleet accepts, compared at-or-above)
  are different objects: adoption moves the first and cannot touch the
  second, so every pin move that ships behavioural change evaluates a floor
  raise in the same pass, and raising the floor is also what reaches a dark
  host. Every queue read checks the floor for free and prints `queue: ENGINE
  STALE` when the runtime is older. Adoption-script doctrine, each clause
  earned live: a failed adopt must never leave the host worse than it found
  it — never force-reinstall a capability that works, self-heal a
  half-removed install once, and still FAIL when the retry fails; a cached
  "adopted" marker records what you DID, not what is on disk, so a skip must
  be bound to the ARTIFACT's own identity (the installed build's
  `direct_url.json` `vcs_info.commit_id`, PR #598), never a capability every
  candidate shares, and every unreadable/malformed/absent answer is UNKNOWN
  and takes the expensive path; when a check reads several evidence sources,
  define their combined evidence — an UNKNOWN member poisons the whole set;
  shipped-shell decisions live in functions a test can drive (reading shell
  proves nothing); and pre-publish acceptance runs on both macOS and Linux,
  because you cannot know in advance which failures are OS-specific.
  `coord-engine doctor <team> --self` is the currency check on demand and is
  TRI-STATE: rc 0 `current`; rc 3 `stale` (run the store's adopt-latest,
  re-run); rc 2 `unknown` (floor unreadable/absent/malformed) — comparison
  impossible is not current, so never read rc 2 as green; repair only on
  nonzero. `coord-engine briefing <team> --agent <you>` remains the fold
  over durable state — identity, role inboxes, reviews owed — honor every
  degraded row it prints as UNKNOWN.
- **VERIFICATION FAILS DIFFERENTLY FROM CODE, AND IN TWO DISTINCT WAYS.** Broken
  code usually fails visibly. A broken CHECK hands back an answer that looks
  fine, so the failure reads as a clean result rather than as an error — which is
  why "verify more carefully" is not the defence. In five logged instances every
  agent involved WAS being careful, and each committed the error inside work
  whose explicit subject was that error. The two modes need different defences
  and neither covers the other:
  1. **A broken INSTRUMENT returns a WELL-FORMED answer.** A probe reading the
     wrong path reports zero rather than erroring; a test harness that collides
     on its own fixture fails in a way that mimics the bug it hunts; a control
     that varies an input which short-circuits BEFORE the code under test proves
     only that the short-circuit works. Defence: check the SHAPE of the result —
     is this answer plausible at all? — and check it against an earlier, narrower
     measurement of the same thing. Too clean to believe, or disagreeing with a
     prior measurement, is the signal; discard the whole run rather than
     reporting part of it.
  2. **A broken INFERENCE returns a PLAUSIBLE answer.** The instrument is fine
     and the reading is fluent, reasonable, and wrong — "those must be false
     positives from matching ids against a human-organised document" is the
     shape. Plausibility cannot catch this, because being plausible is the
     answer's whole problem. The only defence is going to the SOURCE artifact and
     reading it. This is the half no heuristic covers, and the half that ships.
  The trigger is behavioural, not scheduled: **the moment you find yourself
  explaining a result away** — reconciling an inconvenient reading with what you
  expected, ruling a mismatch to be noise, or narrating why a source need not be
  opened — is the moment to run both defences. That impulse precedes the wrong
  conclusion in every logged instance, which is what makes it useful: it is
  available while the answer can still change.
  Corollary for any bug whose subject is a wrong reading: assume your own
  verification of it carries the same defect, and check the instrument before you
  trust what it told you.
- **`needs-me --json` is contract 2: one envelope object, health first.**
  stdout is a SINGLE JSON value — `{"contract": 2, "health":
  DATA|CLEAR|DEGRADED|UNKNOWN, "source", "degraded", "basis", "rows": […]}`.
  Read `health` before anything else: UNKNOWN means the authority itself was
  untrusted and the rows must not be acted on; DEGRADED means rows are a
  FLOOR (partial coverage — never infer absence); CLEAR is the only health
  that licenses "nothing for me". rc is a pure function of health
  (UNKNOWN|DEGRADED → 3, in text mode too). An incomplete parse is UNKNOWN;
  never truncate an authority read with `tail`/`head` and never regex fields
  out of a cut buffer. Other verbs migrate one PR at a time — a bare array
  is the contract-1 shape, scan its marker rows as before
  (`docs/coord/OUTPUT-CONTRACT.md`).
- **If your harness truncates output, read the verdict off stderr.**
  `needs-me` and `briefing` print their row payload with degraded and source
  markers inside it, so a truncating reader can lose exactly the part that
  says whether the read is trustworthy. Both emit one compact envelope line
  to **stderr** — `needs-me: N item(s), health=…, forge=…, source=…,
  degraded=N, rc=N` — which survives stdout truncation (a courtesy duplicate;
  the stdout envelope is the authority). `needs-me --envelope-only` gives you
  that verdict with no records at all, same rc. Trust the envelope's `health`
  and `rc` over a payload you cannot see the end of; `degraded>0` or `rc=3`
  means UNKNOWN or a floor, never clear.
- **Bus-v3 convergence is authority-gated, not a rollout convention.** The
  shared `_coord/bus-v3/records.json` atomically declares protocol and cursor
  schema versions, minimum safe reader/writer engine versions, cursor
  generation/activation, and the CHANNEL every writer resolves — writers read
  `data_type` from it at write time and REFUSE rather than falling back to a
  name lookup (**never resolve a channel by name**: a superseded definition
  can still read as live, so a by-name resolve silently writes where nobody
  reads). `queue` warns on legacy or mixed writer evidence and refuses an
  unknown/old reader or writer before cursor mutation; cursor v2 is
  physically isolated from legacy cursors, and after activation legacy
  activity is a loud health signal, never authoritative coverage. Full
  authority and activation contract:
  [`docs/coord/BUS-V3.md`](docs/coord/BUS-V3.md).
- **Legacy Bus-v3 migration is authority/cursor-only.** Run
  `coord-engine bus-v3 migrate <team> --dry-run` first, then `--apply` only
  after every cursor is `readable-legacy` or `absent`; malformed or unreadable
  state blocks, apply is idempotent and never rewrites legacy cursors, and
  task/role documents are not a migration target. JSON contract in BUS-V3.md:
  never branch on rc alone — read `state` + `error_code`, and treat
  `writes.authority: "ISSUED-BUT-UNPROVEN"` as a write whose read-back did not
  prove the resulting store state.
- **Cursor v2 is transactional: read → process → commit — commit the token
  when the last row is a `queue-delivery`.** Under an activated schema-v2
  authority, `queue` stages one pending batch and prints a `queue-delivery`
  token; it does **not** advance coverage. Process every surfaced event to a
  durable terminal classification, then run `coord-engine queue commit <team>
  --agent <you> --token <token> --result
  <record-id>=<completed|blocked|superseded|ignored>` (repeat `--result` for
  every staged event; an incomplete or extra set is refused). A crash or
  missing commit replays the identical token and batch; a stale token is
  rejected; a repeated successful commit is idempotent. The store transport
  exposes no conditional write, so schema v2 remains fail-closed until a
  transport provides a proven `compare_and_swap` (a write/read-back imitation
  is not CAS); keep the authority on schema v1 until `doctor` proves every
  active writer compatible **and** the CAS gate passes.
- **The Codex safety-net watch checks its literal inbox before briefing**
  (PR 484): one direct `inbox --json` read, then the authoritative `briefing`
  read — briefing's inbox subsection is never a substitute for the direct
  read, and a degraded surface falls back to the documented direct listing
  before reporting quiet. Deliberate redundancy against a stale or unreadable
  summaries index.
- **Retired (2026-07-27, operator-ordered) and REMOVED (2026-08-03, PR #523):
  the `listen` watcher as the wake surface — don't hand-roll a replacement.**
  Discovering work by walking the file tree degraded ~9 ticks in 10 at fleet
  scale and hid work; the v3 queue read (cursored, fail-closed) replaces it.
  Replies to `tell`/`respond`/`review request` arrive as v3 events on the
  record queue — read it on your next wake (the send verbs echo
  `replies:`/`await verdicts:` breadcrumbs; see
  [`fulcra-agent-automation` §2](skills/fulcra-agent-automation/SKILL.md)).
  Invoking `listen` is an argparse error; any surviving `listen-state.json`
  shard is historical residue. Presence stays **time-dirty** rather than
  feed-cached: each briefing evaluates the roster against the current clock,
  so an unchanged session shard still becomes `LAPSED` when `now >= until`.
- **Review handshake.** Nothing lands without an independent review by a
  *different agent identity* than the author — that review is the control, not
  who clicks merge. Where a forge exists the change goes through a **PR, never
  a direct push to `main`**. The handshake rides the bus, not the forge:
  `coord-engine review request <team> <slug> --of <artifact> [--head <exact-sha>]
  --reviewer <role>` opens a durable obligation that sits in the reviewer's
  `needs-me` until their verdict file exists at the exact path the command
  echoes (the required token is the role passed to `--reviewer`; that token is
  what the tally credits). The pending row itself carries the artifact: in
  `needs-me --json` every `review-pending` row serves `of` and the exact
  `head` (explicit `null` when a legacy register genuinely lacks the field),
  so a reviewer dispatches from the row alone — no second lookup
  (OUTPUT-CONTRACT OC5). **One PR has one review slug (`pr-N`), across every
  push**: pass the PR URL as `--of` and the full 40- or 64-hex commit id as
  `--head`; re-requesting the same slug with a NEW head advances the same doc
  to the next round, verdicts append at `verdicts/<head>--<required-token>.md`
  (frontmatter must repeat that exact `head`), and `review status` folds ONLY
  the active head, ignoring superseded-head verdicts without deleting them.
  Legacy/non-code reviews may omit `--head`. **A verdict that cannot be
  counted is REPORTED, never dropped, and the verb exits 3** — a malformed
  filename, a `verdict:` token outside `review.accepted_vocabulary()`, or a
  shard whose frontmatter attests a different head each name the rule that
  skipped them; the failure class is the old affirmative
  `pending_required: [alice]` while alice's verdict sat unreadable in the
  directory. The request is **durable-first, not atomic**: the doc lands
  FIRST, then one directive per required reviewer (a verb-opened review fires
  each reviewer's inbox — never hand-send a review tell); a partial
  notification failure is loud (rc 1, naming who was and wasn't notified) and
  re-running the SAME request is idempotent recovery, while a re-request with
  a *different* `of`/required-set/requester is a loud rc 1 conflict and a
  present-but-unreadable doc fails closed (never overwritten).
  `coord-engine review status <team> <slug>` computes APPROVED/CHANGES/PENDING
  and gates the merge. The `<artifact>` is an opaque ref, so the handshake
  works with any forge or none — a forge-only "Approve" does NOT count
  (co-located agents often share one forge account); the bus verdict, keyed
  by agent identity, is the source of truth. **The verdict FILE discharges
  the obligation**; the ack is inbox hygiene and targets the review-request
  directive by its inbox id (`review-request-<review-slug>-<hash>`), never
  the bare review slug. Full rules and per-harness wiring:
  [`fulcra-agent-review`](skills/fulcra-agent-review/SKILL.md) and
  [`fulcra-agent-automation`](skills/fulcra-agent-automation/SKILL.md).
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
  days (a reviewer's lease went stale while it filed verdicts hourly, and
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
- **Engine surfaces any bus reader must honor.** Two invariants a reader lives by:
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
    caller's OWN head is feed-gated too:** with a clean `data-updates` window, only caller-owned slugs
    named changed since the projection anchor are raw-tallied; unchanged caller-owned slugs are served
    from the projection. Without positive feed proof, every caller-owned head slug is raw-tallied
    fail-closed (see the head-budget rule below). No source row at all means the aggregate carries no
    projection: the pre-projection raw scan. Contract for readers:
    [`docs/coord/BUS-V3.md`](docs/coord/BUS-V3.md) → "Where a fold's answer came from". **Ship-gate:
    any new projection-served fold emits a source row through the shared renderer.**
  - **Honor every degraded row; never read a bounded fold as complete.** `briefing`/`needs-me` bound
    each section under `COORD_BRIEFING_BUDGET` (default 60s, cumulative across sections) and emit a
    `{scanned, total, skipped}` degraded row per section (`review-fold-degraded`, `forge-degraded`,
    `presence-degraded`, plus the public-read markers below). On ANY of them, fall back to the
    section's direct sweep (`review status` per slug, `forge feedback`, `presence show` — per-section
    fallbacks in [`fulcra-agent-review`](skills/fulcra-agent-review/SKILL.md) and
    [`fulcra-agent-automation`](skills/fulcra-agent-automation/SKILL.md)). The review sweep itself
    **fails closed**: `review status` returns rc 1 (`tally unknown, retry`) when the doc, the
    verdicts listing, or any shard is unreadable, rather than printing a partial APPROVED — a
    degraded transport can never green-light a merge.

  These budgets rest on **hard per-op boundedness**: every transport subprocess runs in its own
  process group and is SIGKILLed whole on timeout; the per-op bound is `COORD_TRANSPORT_TIMEOUT`
  (default 30s) — run it TIGHT on a resident bus reader (e.g. 8s). Every `COORD_*` tuning knob, the
  shared positive-finite parse policy, and the `FULCRA_COORD_*` legacy-prefix rule are catalogued in
  [`packages/coord-engine/README.md` → Environment / tuning](packages/coord-engine/README.md#environment--tuning);
  the mechanics that spend the budgets live in `coord_engine/budget.py`. **Ship-gate: a NEW bounded
  fan-out uses `budget.Deadline` for its deadline check (never a hand-rolled
  `time.monotonic() >= deadline`) and `budget.degraded_row` for its marker**, so the whole family
  keeps one `>=` boundary and one degraded shape.
- **The public-read failure contract — UNKNOWN is loud, never a clean-empty.** Every aggregate-backed
  public read (`status`, `board`, `needs-me`, `search`, `inbox`, the `agents`/`digest`/`asks`/
  `briefing` bundles) folds the summaries index via `_load_rows_status`, whose `ok` bit is **False
  when the index/listing is UNKNOWN** — as distinct from a genuinely-ABSENT index (a fresh team),
  which is a real, readable empty. A read whose `ok` is False must **NEVER return a clean-empty
  result**: it emits the shared marker `_read_degraded_row(reason)` = `{"type": "read-degraded",
  "reason": …}` (inside the one `--json` value; a stderr notice in text mode) while retaining any
  partial rows, and `reason` preserves every independent failure in the pass. The hazard it closes: a
  silently-empty fold reading "all clear" while a live unacked directive is merely unreadable.
  **Ship-gate: a new aggregate-backed read consumes `_load_rows_status` (never `_load_rows`) and
  surfaces the marker on `ok is False`, with a red-first test asserting no clean-empty under a
  degraded transport.**
- **`--json` purity: stdout is ALWAYS one parseable value.** Under `--json`, NO prose ever reaches
  stdout — every degraded/notice line becomes a JSON row or a reserved key, or goes to stderr. Each
  fold verb's `--json` branch is exactly one `json.dumps` of the single result; `threads` emits a
  single JSON **array** (the dropped list + a trailing `threads-degraded` element), NOT JSON-Lines.
  **Ship-gate: a new `--json` path is one `json.dumps`, with a red-first test that
  `json.loads(stdout)` yields exactly one value on every degraded path.** Enforced by PARSER
  DISCOVERY, not a hand-kept list: `test_json_purity.py` walks the real parser for every `--json`
  path and fails until each is in `_JSON_PINNED` (smoke-run under a corrupt index AND squeezed
  budgets) or `_JSON_EXEMPT` with a stated reason — `_JSON_EXEMPT` is empty today, pinned paths must
  print SOMETHING, and the widened sweep immediately found a live leak (`headroom --json` printed
  prose on its no-accounts early return).
- **`.settled` carries TWO things and only one of them is disposable.** `state: APPROVED` is a tally
  CACHE — cheap to recompute, safe to drop. `state: MERGED` + `merge_sha` is merge EVIDENCE written
  by `review close`, and **nothing recomputes it**. `review status` deletes a stale cache but
  **never** a MERGED marker; an unreadable marker is left alone, and an unclassifiable one
  (including an unrecognised `state:` from a future writer) is PRESERVED, reported on stderr, rc 3 —
  only a POSITIVELY identified cache may be deleted. **If you add a writer to a shared marker path,
  audit every existing deleter of it in the same change.**
- **A cache may only bind to evidence it can actually fingerprint, and EVERY reader applies the same
  rule.** The `.settled` cache carries an `evidence` digest over the verdict shard NAMES, recomputed
  from the current listing before it is honoured — but a name digest cannot see an in-place rewrite
  of a plain `<head>--<reviewer>.md` shard, and this store exposes no etag or content hash in a
  listing. So **a directory holding any plain shard gets no cache at all** (folded for real, every
  time; append-only directories keep the fast path), both writers refuse to stamp a marker no reader
  may honour, and every reader goes through ONE decision function, `review.settle_shortcircuit` —
  the fan-out once skipped on marker PRESENCE alone and hid a pending reviewer's obligation. MERGED
  markers short-circuit unconditionally. The projection tier ABOVE the readers is a reader too: its
  zero-op settled carry may only be taken by a row whose `ev_bindable` is True; everything else
  demotes to one listing whose fingerprint compares name+size+mtime per shard and refuses any
  unclosed minute. **When you add a validation rule to a reader, enumerate every tier that can
  answer BEFORE it.**
- **`escalate` attendance: one shared scan, and partial coverage is NOT an incident.** The vacancy
  sweep answers "did a holder file a verdict recently" from ONE `_verdict_activity_index` pass built
  before the role loop, bounded by BOTH a count (`budget`, 40) and a wall clock
  (`COORD_ATTENDANCE_SCAN_BUDGET`, 30s) — a count alone cannot bound time. The review register is
  far larger than the budget, so **coverage is always partial by design**: the stderr envelope
  reports `attendance=N/M` rather than alarming, and **rc 3 is reserved for a WALL-CLOCK cut** — a
  real anomaly.
- **PRIOR FRESHNESS IS LOAD-BEARING: a stale prior costs ~3x.** Measured on a live store, a
  CONVERGED prior folds the review register in a third of the ops and completes; a STALE one triples
  the cost and cuts short — and because forge completeness follows the review fold's, one host's
  staleness denies forge to every consumer of that aggregate. A SMALL unknown remainder is retried
  once inside the same pass (`RETRY_UNKNOWN_MAX`, bounded by the SAME deadline object); a tolerance
  that calls one-short complete was explicitly REJECTED — it manufactures the false-clear this file
  keeps warning about. A fold "stuck" at n-1 of n is usually a transient, not a broken record.
- **Never hand two builders ONE budget object.** A single `Deadline` shared by the review and forge
  projections let the review fold spend it first, so forge was not slow — it was never built: a
  section that always runs last inside a shared budget starves deterministically and silently. Each
  builder opens its own (`COORD_PROJECTION_BUILD_BUDGET`, `COORD_FORGE_BUILD_BUDGET`); worst-case
  pass duration is their SUM, stated rather than hidden. **Completeness coupling is different from
  budget coupling and must not be "fixed" with it** — forge's `complete` still follows the review
  fold's.
- **A DELIVERY path may never lose coverage to a FORMATTING failure.** A queue renderer that raised
  on a malformed event once skipped the cursor save, wedging the cursor on the same poison event
  forever with no error path that said so (measured live). Two layers, because the field fix alone
  guards the instance and leaves the class: rendering is PER EVENT and cannot raise — poison renders
  as an explicit, COUNTED POISON line (trading a crash for a disappearance is the worse bug) — and
  **the cursor save lives in a `finally`**: once a window has been READ, coverage is a fact about
  what the process RECEIVED. `peek` stays the one exit that deliberately does not advance. **When
  you add a step between a read and its acknowledgement, ask what happens to the acknowledgement if
  that step throws.**
- **Head-of-line: a budget cut may only ever truncate the TAIL — never the head.** The work-discovery
  folds do live per-op transport at query time over an unbounded population; under budget pressure the cut
  must land on the *lowest-priority* tail, so an agent's OWN assigned work and any decision parked on a
  human can never be the thing that goes invisible. Two structural heads enforce this:
  - **Blocked-on-human is the reserved FIRST section, and it is FREE.** `briefing` and `needs-me`
    render open rows blocked on a human first, computed by `query.blocked_on_human` PURELY from
    aggregate rows already in memory — zero extra transport ops, and free is what makes it
    un-starvable. `--on-user` TYPES the block as `blocked_on: user:<name>` (additive; legacy plain
    values resolve against the caller's already-loaded identity set), and **ambiguity resolves
    toward SURFACING** — a hidden human-blocked item is the incident, a false positive only noise.
  - **The caller's own reviews are the review-fold head, on a budget earlier legs cannot have spent.**
    `_pending_reviews_for` derives the caller-assigned review slugs for free from the review-request
    directive rows and scans them FIRST under a DEDICATED `deadline_seconds`, NOT the shared briefing
    budget's drained remainder (the fix for a live `scanned 0/N`: the leg used to start already
    expired on a busy board). The tail keeps the shared budget; truncating it is expected and reports
    `review-fold-degraded`. A head that STILL cannot complete is UNKNOWN and gets its OWN DISTINCT
    marker `review-head-degraded` — **on ANY non-complete outcome**: a budget cut, an unreadable
    review doc, a per-slug `TransportError`, or a caller-directive slug absent from the listing
    (fail closed — negative membership in a listing is not proof the obligation is gone; missing
    slugs are named in a `missing` field). The two markers carry PHASE-LOCAL counts and never borrow
    each other's numbers (an UNKNOWN head reads `0/1`, never `0/0` or `1/1`); a HEAD-only incident
    emits `review-head-degraded` and NOTHING else. **Ship-gate: a new bounded work-discovery fold
    puts blocked-on-human and caller-assigned work at the head (free where the data is already
    loaded; a dedicated budget where it is not), proves the head completes under a spent shared
    budget, and gives "head could not complete" a marker distinct from "tail truncated."**
  - **Every marker must RENDER, not just exist:** `briefing` and `needs-me` type-dispatch every
    review row type they can receive through ONE shared helper (`_review_row_line`), so an identical
    row type can never diverge between the two verbs. An unknown/typeless row must NEVER reach the
    generic task line (which prints `[ ?] ? None` on a marker shape); a degraded/UNKNOWN marker is
    always shown and NEVER counted as a pending item. **Ship-gate: a new review row type joins the
    shared dispatch with a red-first test that both verbs show its real line and that an UNKNOWN
    marker is not tallied as pending.**
- **Role routing is the same contract, one layer in — a role you hold is an address.** A directive
  assigned to a ROLE is directed at whoever holds a fresh lease on it, so `briefing`, `inbox`, and
  `needs-me` all fold role-routed work into the holder's queue (that is what makes role-based
  identity outlive a session). ONE resolver: `cli._held_roles_for_rows` — never resolve roles a
  second way, or the folds silently disagree about a lease. It returns `(held, unresolved)`, and
  **`unresolved` is the load-bearing half**: a role whose lease state is UNKNOWN (transport failure,
  unreadable lease shard, unparseable role doc, an explicitly invalid `sla_hours`, or a budget cut)
  is neither held nor not-held — folding it into an empty held-set renders a clean, role-blind queue
  indistinguishable from "you have no role work". Every caller surfaces it as `_role_degraded_row` =
  `{"type": "role-degraded", "roles": […]}` plus the text line. **Ship-gate: a new fold that answers
  "what needs this agent" resolves roles through that one helper and surfaces `unresolved`, with a
  red-first test proving a failed lookup is visible.** **Only a complete, successfully parsed
  listing is negative membership evidence** — a failed read and a failed parse are the same fact,
  and the rule reaches into the FIELD: an explicitly invalid value is UNKNOWN (a default is never a
  substitute for a value someone set and got wrong), while an absent/blank optional field means the
  default IS the stated intent. Fold that distinction ONCE, in `roles.py`, and let callers fail
  closed on `None`. **The ship-gate extends to write paths**: `continuity park` once swallowed a
  failed listing as "you hold no roles", printed "nothing to park", and exited 0 — silently
  discarding the checkpoint the next session resumes from. `_held_roles` now returns `(held, ok)`;
  on `ok is False` park fails non-zero saying the checkpoint was NOT written, and a complete fold
  proving zero fresh roles exits **rc 2** `CHECKPOINT NOT WRITTEN` — a command that ACTS on the
  roles you hold refuses to act on UNKNOWN rather than treating it as "nothing to do". The fold's
  cost is one `roles/` listing + a few ops per role the open work references, and it runs under one
  cumulative `COORD_ROLE_FOLD_BUDGET` (default 20s) — a cut marks every unfinished candidate
  `unresolved`, never "not held".
- **Presence engagement is an additive, defensively-parsed schema.** A presence shard MAY carry an
  `engagement` object with exactly four qualified names: `engagement.mode`
  (`resident|session|occasional`), `engagement.until`, `engagement.state` (`active|lapsed`),
  `engagement.lapsed_at`. **Absent `engagement` reads as `resident` + `active`** — every legacy
  shard is unchanged, and a beat with no `--engagement` flag writes NO engagement field
  (byte-identical legacy shard, pinned). `--engagement session` defaults `until` to beat time + 8h;
  `--until` is meaningful ONLY for `session` (anything else is rc 2, nothing written). **A beat is
  REFRESH-SAFE and must never manufacture liveness**: a session beat reads its own prior shard
  first — an ABSENT prior (disproven by one parent listing; the transport read is
  None-on-any-failure, so the listing is the disambiguator) is a fresh session; an UNKNOWN prior
  (listed-but-unreadable shard, failed listing) FAILS CLOSED at rc 1, so a transient read failure
  never lets fresh active engagement replace a sweep-marked lapsed session; a readable prior with
  malformed engagement self-heals as fresh. A continuing session's resolved `until` is PRESERVED
  (recomputed only for a genuinely new session; an explicit `--until` wins) — sliding it forward on
  every beat would make a session never lapse. The beat never writes
  `engagement.state`/`engagement.lapsed_at` to a non-default value — those two names are written
  ONLY by the lapse sweep (below), and clearing `lapsed` takes an explicit session re-declaration.
  Every fold PARSES engagement through the ONE defensive seam `presence.parse_engagement(fm)` — any
  malformed input (non-dict, unknown `mode`/`state`, unparseable timestamps, a `session` with no
  resolved `until`) degrades to the legacy `resident`/`active` default AND sets a visible
  `_engagement_degraded` marker, never raises — and only the liveness combiner (below) acts on it;
  the pure `classify` never consults it. **Ship-gate: any engagement read goes through
  `parse_engagement` (never a raw dict-walk); any new bad-input class gets a red-first
  degrades-with-marker test; no write path but the lapse sweep sets `state`/`lapsed_at`.**
- **Activity implies liveness.** Every engine bus **WRITE** verb refreshes the
  **actor's** presence timestamp, so a *working* agent is provably live — distinct from a dead session
  whose launchd beat still ticks (the liveness combiner consumes this as proof). Membership is a **DENYLIST**:
  every verb refreshes EXCEPT the declared reads (`status`/`board`/`search`/`needs-me`/`briefing`,
  `presence show`, `review status`, `queue`, `health`, `doctor`, `obligations`, `roles status`,
  `continuity resume`) and the plain `presence beat`. It was once an ALLOWLIST of functions,
  which could not keep this paragraph's promise — a verb added later was simply absent, and
  absence there is indistinguishable from "this agent is not working". Many write verbs had drifted
  outside it (`review close`, `escalate`, `continuity snapshot`/`park`, `roles claim`/`release`,
  `answer`, `bus-v3 send`, `stash push` …), so an agent whose job IS reviewing rendered `stale — nudge`
  while working: measured live, a reviewer showed `stale 6d` having filed a verdict hours earlier,
  and the roster attaches an imperative to that judgement, so it dispatches people, not just labels them.
  **The work axis.** Verb coverage alone cannot see work that never passes through a verb (report
  docs are written straight to the store), so `presence.liveness` also takes `work_ts` (newest
  work-artifact time, read-derived by the caller) plus a **three-valued** `work_scan`
  (`NONE|COMPLETE|PARTIAL`): NONE → byte-identical legacy behaviour; COMPLETE → absence is a real
  finding (`no work found`); PARTIAL (budget ran out, unreadable listing) → **no imperative at
  all**, because the refuting artifact may be in the unscanned part — `list_dir` cannot tell an
  empty directory from an unreadable one, so only a COMPLETED scan licenses an absence reading.
  `presence show` and `briefing` measure, both bounded (`COORD_PRESENCE_WORK_BUDGET`, default 20s;
  NB `env_float` is POSITIVE-finite — a `0` falls back to the default, it does not disable). **Scan
  order is load-bearing**: agent reports (a handful of listings) run BEFORE the review sweep
  (hundreds) — with reviews first, the sweep consumed the whole budget and PARTIAL muted the nudge
  fleet-wide. The sweep is now the POINTER-LESS FALLBACK only: **per-agent work EVENTS**
  (`_coord/agents/<agent>/work/<iso>-<digest>.json`) answer the read-side question in one listing +
  one read per agent, and are IMMUTABLE ON PURPOSE — the store has no conditional or versioned
  write, so a shared mutable pointer cannot be defended (review reproduced an older stamp clobbering
  a newer one, and a delete-on-failure that could erase another host's fresh write). A writer only
  CREATES its own event, "newest" is a deterministic fold over ISO-led names, and a failed write
  leaves prior events intact (slightly stale, never wrong) — NEVER add a delete-on-failure here.
  Four pinned rules: **ONE write site** (the activity chokepoint, so a new write verb stamps by
  default); **refusal semantics** (stamped only on `rc == 0`, monotonic, missing/corrupt is UNKNOWN
  and never "did nothing"); **transitional** (a pointer-less agent falls back to the sweep);
  **attributable** (`kind` + `path`). NB the pointer is keyed by the RAW agent name, NOT
  `tasks.agent_key()` — the hashed form would file every pointer where no reader lists. The
  **continuity audit deliberately does NOT consume work evidence**: "working but not snapshotting"
  is precisely its finding, and the asymmetry is chosen.
  **Classification is PER-OPERATION, not per-function.** Several handlers serve both a read and a write:
  `queue TEAM` vs `queue commit TEAM` (and `--consume`, which advances ANOTHER agent's cursor),
  `inbox TEAM` vs `inbox --ack`, `digest TEAM` vs `digest --store`. Keyed on the function alone these
  were wrong in BOTH directions at once — `queue commit` recorded durable classifications without
  counting as activity, while `inbox` and `digest` refreshed presence merely by being VIEWED.
  `_MIXED_MODE_ACTIVITY` maps such a handler to a predicate over the PARSED
  ARGS, and `_is_activity_invocation(args)` — not the function-only helper — is what dispatch calls.
  **Verdict shards are APPEND-ONLY (standing ruling).** Two forms are first-class, permanently:
  `<head>--<reviewer>.md` (hand-writers, unchanged) and `<head>--<reviewer>--<iso>-<digest>.md`
  (the verb) — the store has no create-if-absent and no versioned write, so writing a SHARED name
  is check-then-write and cannot protect evidence (a concurrent CHANGES was reproducibly
  overwritten by APPROVE at rc 0); a unique name touches no existing file. **Every register reader
  folds newest per (head, reviewer)**, ties break on the name, and **supersession is never
  silent** (`superseded_verdicts` is reported). A correction is a NEW shard, and **a new verdict
  INVALIDATES the settle cache** — only the cache (the evidence-binding rules above); a
  `state: MERGED` marker survives a late verdict. Every reader dates a plain shard the same way:
  filename ts, then frontmatter ts, then normalized listing mtime.
  **`review verdict`** exists so that filing a verdict IS an engine write (it was the one act with
  no verb, which left reviewers invisible to every liveness fix). It is SUGAR over the same
  artifact — it writes exactly the canonical `<head>--<reviewer>.md` shard at the printed path, and
  DIRECT shard-writing stays valid — and it REFUSES to overwrite an existing verdict: a verdict is
  evidence a merge may rest on; a changed head is a new round, the supported way to revise.
  **Every registered command must be CLASSIFIED**, read or write or mixed:
  `tests/test_activity_covers_every_write_verb.py` walks the real argparse tree and fails on any
  unclassified command (a regex cannot decide this — several verbs persist only through helpers).
  `_ACTIVITY_READ_FUNCS` is completed at module end, after the extracted command modules import —
  assembled earlier, it let view verbs refresh presence merely by being READ, the worse direction
  because it suppresses the nudge for an agent who really is gone. The hook lives at the single
  dispatch chokepoint, fires only on `rc == 0`, and the **actor is the WRITER** (`--from` /
  `FULCRA_COORD_AGENT`; the anonymous reconcile fallback is not a presence identity). Two pinned
  constraints: **THROTTLE** — at most ONE presence write per
  `presence.ACTIVITY_REFRESH_INTERVAL` (60s) per process, via a monotonic-clock memo; and
  **FAILURE ISOLATION** — a refresh failure never fails the successful bus write (one stderr note,
  `rc` untouched). **The refresh is a TIMESTAMP BUMP, not a beat re-run**: it rewrites ONLY the
  top-level `timestamp` line and preserves every other byte verbatim, so it never slides a
  session's `until` and never writes `state`/`lapsed_at` (sweep-owned). The minimal-beat fallback
  fires ONLY on `list_dir`-CONFIRMED absence and FAILS CLOSED on any UNKNOWN — failure isolation
  never means destructive fallback. **Ship-gate: the throttle memo is process-global state — reset
  it between tests; a new write verb joins `_ACTIVITY_WRITE_FUNCS` (or the omission is justified);
  the preserve-everything-but-timestamp rule stays red-first pinned.**
- **Engagement-aware liveness is a combiner over two ORTHOGONAL axes.** `classify(ts, now)` stays
  PURE — freshness only (`live`/`idle`/`stale`), a function of the timestamp alone. The truth table
  is layered on top by `presence.liveness(shard, now=…)`, which returns `{state, freshness,
  annotation, engagement}`. **STALENESS** (timestamp freshness) and **DORMANCY** (a `session` past
  its `until`, boundary-inclusive, OR a durable sweep-written `engagement.state: lapsed`) are
  INDEPENDENT and rendered as two facts, NEVER a merged label: a dormant shard renders primary
  state **LAPSED** — distinct from stale/dead, EXPLAINED, ROLE-RETAINING — with the freshness axis
  in the annotation (a session overrunning its window while beating is honestly **LAPSED+active**,
  a nudge to extend, NEVER silently live). A degraded engagement reads as the legacy default and
  can never manufacture dormancy; all agent lookups match by EXACT id (no substring/fuzzy). **The
  verdict rides ADDITIVELY:** roster rows keep `liveness` = the pure freshness band (existing
  callers read it; its meaning must not shift) and gain `state`/`freshness`/`annotation`.
  **`engagement gate <team>`** is the deterministic mixed-fleet gate: every LIVE roster agent is
  COVERED iff it beats with well-formed `engagement` OR appears in the operator map
  `_coord/router/engagement-defaults.json`; PASS iff all live agents COVERED. **READ-CONTRACT LENS
  (mandatory):** `transport.read` returns `None` on BOTH missing and failure, and a `list_dir`
  degradation must never fold to empty, so a falsy read/listing is NEVER "confirmed absent" on its
  own — an UNKNOWN on either of the gate's reads (the defaults file, or the presence ROSTER via
  `_presence_shards_status`, which preserves degradation instead of swallowing a `TransportError`
  to `[]`) is **DEGRADED, fail closed — never PASS on unknown coverage** (an empty gate passes
  vacuously, so an UNKNOWN roster must never look confirmed-empty; an unparseable shard is UNKNOWN
  coverage, never a synthesized phantom row). Only a CONFIRMED enumeration may PASS; a
  confirmed-EMPTY roster still passes vacuously. **The vacancy/escalation SEMANTIC change is gated,
  and the gate ships dormant by default — enable it explicitly:** LAPSED rendering lands
  unconditionally, but reading a LAPSED session role-holder as EXPLAINED ABSENCE (suppress the
  vacancy escalation with a note) activates ONLY when `_engagement_gate_passes(team)`; otherwise
  behavior is today's, verbatim. BOTH branches are red-first pinned. **Ship-gate: `classify` stays
  pure; any new dormancy/coverage input class gets a red-first test; the gate fails closed on any
  UNKNOWN; the gated semantic change keeps both branches pinned until the gate is satisfied
  fleet-wide.**
- **The zero-token lapse sweep writes two fields and nothing else.** `coord-engine engagement sweep
  <team>` is a host-tick, model-free pass that marks a session past its `until` as LAPSED by
  writing EXACTLY `engagement.state: lapsed` + `engagement.lapsed_at` into the presence shard —
  the ONE sanctioned exception to agent-owned presence writes, scoped to those two names. **MARK
  predicate** (`presence.sweep_decision`, the pure read-only seam, via `parse_engagement`): mark
  iff `mode == session` AND `until` present AND `now ≥ until` AND `state == active` AND engagement
  WELL-FORMED; anything else SKIPs (fail-closed — a malformed shard NEVER manufactures a lapse),
  and an already-lapsed session is an idempotent NOOP. The write preserves everything but the two
  fields — `timestamp` NOT bumped, `until` NOT slid, body verbatim — and **NEVER parks, NEVER
  releases roles** (operator decision: park is explicit-only). If enumeration raises, the roster is
  UNKNOWN and the sweep is DEGRADED — loud, rc 1, never a clean `0 marked`; a per-shard failed read
  SKIPs into the `degraded` bucket (a failed read must NEVER cause a write); a per-shard write
  failure is reported and the sweep continues. `--dry-run` previews and writes nothing; rc 0 only
  on a clean sweep. Downstream consumers (e.g. a deployed router) read the marker for
  reduced-cadence check-ins; `lapsed → active` clears only via an explicit session re-declaration
  in the beat. **Ship-gate: the sweep writes ONLY `state`/`lapsed_at`; the mark predicate stays
  fail-closed; enumeration-degrade is loud + rc-nonzero; idempotency and the two-field-only /
  never-park / never-release invariants stay red-first pinned.**
- **The rc / error register a bus reader parses.** Machine `type` fields ride the degraded **fold rows**
  (`*-degraded`); the **single-slug verify** paths are prose at **rc 1**, where the convention is
  load-bearing: the prose ends in **"…, retry"** iff the failure is retryable (a transient
  unknown — e.g. `review status` `tally unknown, retry`, `roles status` `lease state unknown … retry`,
  `tell` `cannot verify delivery, retry`) and names a **tombstone** iff terminal (a `review status` on a
  soft-deleted review — a retry never resurrects it). An **UNEXPECTED** exception is neither: the
  top-level guard emits a registered envelope `coord-engine: error: command=<cmd> type=<Exc>: <msg>`
  (rc 1) — the `error:` token distinguishes an engine fault from a retryable degrade.
- **Views never lie past the current read — the index-freshness invariant.** Two mechanisms keep
  `status`/`board`/`inbox` honest between heartbeats, so a same-minute close or a between-tick directive
  can't leave a surface stale:
  - **Same-minute-touched docs are reparsed, not reused.** The store `file list` mtime is
    minute-granular, so reconcile reuses a prior summaries row only when the doc is unchanged by
    mtime AND byte size AND its mtime-minute is provably closed before the last reconcile read; a
    row stamped by an older `ROW_SCHEMA_VERSION` is likewise reparsed once, so a projection change
    self-heals the whole index within one full pass.
  - **A feed delta surfaces new docs THIS read.** Healthy-path folds combine the aggregate with
    team-filtered `data-updates` changes and read only the changed task docs; on any doubt the
    legacy freshness overlay lists the task dir once, bounded (`COORD_OVERLAY_CAP` /
    `COORD_OVERLAY_BUDGET`) and **degrading the `inbox` source visibly** when capped — never silent
    truncation.
  - **Acks are folded change-driven, and reuse needs positive evidence.** Reconcile asks the store
    what changed since the ack fold's OWN anchor (`acks_folded_through`, not `generated_at`) and
    re-folds only those slugs; a prior `acked_by` is reused ONLY when the store answered and did
    not name that slug — every unknown falls back to the full fold and logs why. **No false
    advance:** a fold that couldn't read what it meant to leaves the anchor in place, a failed
    listing preserves the prior `acked_by` rather than un-acking, and a forced full fold every
    `COORD_ACKS_FULL_EVERY` passes (default 72) bounds anything the query could miss.
  - **Reconcile's own pass is a feed delta, not a directory scan.** It consumes `data-updates`
    since a durable cursor (`reconcile_cursor` — watermark + processed ledger) and reads ONLY the
    changed shards. The full `list_dir(task/)` scan stays as (a) the fail-closed fallback on ANY
    cursor/feed doubt and (b) a scheduled drift self-check (every `COORD_RECONCILE_FULL_EVERY`
    passes) whose divergences are logged LOUD and rebuilt from the full scan, never silently
    absorbed; an incremental row is stamped byte-identically to a full-scan row. **Ship-gate: a new
    reconcile fast path takes the full scan on ANY doubt, keeps the periodic drift check, and its
    cursor key is cut from `build_aggregate`'s passthrough and recomputed in full every pass.**
  - **summaries.json is one shared doc written by many hosts at many versions — a top-level key
    added in version N is wiped by any host older than N**, which rebuilds the document from the
    key set it knows and writes it over everyone else's (this is how `acks_folded_through` kept
    vanishing while any pre-passthrough host still reconciled). `build_aggregate` now carries
    unknown top-level keys through. **A new top-level key is live only once the whole fleet is
    upgraded** — check `health` before assuming a fold-state key is doing anything, and never
    rebuild the aggregate from a fixed key set.

  Mechanics (stamping, deterministic cut, the reconcile reuse anchor) live with the engine —
  [`fulcra-agent-reconcile`](skills/fulcra-agent-reconcile/SKILL.md) and
  [`packages/coord-engine`](packages/coord-engine/README.md).
- **Idle-agent parking (standing, operator-set).** An agent with **48h of no
  work** in its queue **parks a continuity checkpoint to the bus**
  (`coord-engine continuity park <team> --agent <self> --objective "<what you
  watch>" --next "resume on directed wake or new assignment"`) and drops to a
  coarse daily check; a directed wake or new assignment resumes it
  (`continuity resume`). Dormant watchers must not burn compute indefinitely;
  the parked checkpoint loses nothing. Applies to every agent, the coordinator
  included. `continuity park` exits rc 2 when no fresh role is held and writes
  no checkpoint — treat that as "not parked", never as a clean no-op.
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
  That one exit code covers two opposite situations and the fix differs: an
  assigned/expected role whose lease lapsed → `roles claim` (left unfixed the
  role sits VACANT past SLA and role-routed work reaches no holder — observed
  live when an agent read rc 2 as a fact about itself rather than its lease);
  intentionally role-less (the steady state for most workers) → use
  `continuity snapshot` for progress saves, and **do NOT fabricate a role
  merely to make `park` succeed**. Check `roles status <team> <role>` before
  concluding which case you are in: rc 2 is a lease diagnostic, not an
  identity verdict. `continuity resume` always reports the checkpoint age
  (JSON `checkpoint_age_seconds`); use `--max-age 1h` when a wake or
  acceptance run must fail rc 2 on stale state — JSON `error_code` separates
  invalid duration, unknown age, and stale; small future clock skew clamps to
  zero, farther-future checkpoints fail loud.
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
  the operator states an intent to ANY agent ("later today", "I'll enumerate
  that list", any commitment they own), that agent captures it immediately with
  `coord-engine intent <team> "<text>" --for <principal> [--by <when>]` — an
  uncaptured commitment is the drop nobody can see. Two surfaces back this:
  - **`coord-engine intent <team> "<text>" --for <principal> [--by <when>]`** —
    sugar over the directive path (writes an `intent:<principal>` item,
    `intent_by` frontmatter, hash-slug delivery + read-back inherited).
    Identity is **text + assignee only** — `--by` is EXCLUDED. So an identical
    restatement dedupes (rc 0 `intent already captured`), while a restatement
    with a DIFFERENT `--by` is a verified in-place window update on the same
    doc (rc 0 `intent window updated`, read-back-checked; unverifiable → rc 1,
    retry — never a stale deadline, never a forked item). A relative `--by`
    (`5d`/`36h`/`10m`) re-resolves from now on each restatement.
  - **`coord-engine threads <team> --for <principal> [--json]`** — the
    dropped-work fold, three mutually-exclusive modes (first match wins):
    **started-then-silent** (an item the principal owns/last-touched, quiet
    past `--silence-days`, default 3); **blocked-on-operator** (progress waits
    on the principal — `assignee: <principal>`, a `blocked-on:<principal>`
    tag, or a `needs:human` block naming them — surfaced immediately, no
    aging); **intent-never-started** (an `intent:<principal>` item past its
    window and not followed up). A **terminal item (`done`/`abandoned`) is
    NEVER a dropped thread** in any mode — the fold refuses it and reads the
    authoritative status from the task doc, not the summaries index (a
    same-minute close can leave the index stale). A **`threads-degraded` row**
    means the fold saw only PART of the store — sweep or wait, **never trust
    it as complete**; `--json` is ONE array, not JSON-Lines. The coordinator
    role typically runs this fold in its loop and owns the curation/push
    call.
- **Blocked-on-operator doctrine — a harness approval-gate is a bus event
  (operator order).** When work waits on an
  operator/harness approval that only a human can grant (a "nod before you build",
  a deploy sign-off, an entered credential, any approval you cannot self-serve),
  the blocked agent does THREE things the same turn, not one: (1) immediately post
  a **P1 BLOCKED-ON-OPERATOR** shard to the coordinator role naming the EXACT approval and
  the EXACT artifact it gates (PR #, slug, host) — so the block is a visible,
  routable bus item, not a private wait; (2) **continue all non-blocked work** —
  the block scopes to the gated artifact, never to the agent; idle-while-blocked
  on one item when other work is ready is itself a failure; (3) **keep beating** —
  a blocked agent is still live and must stay so. **Silence-while-blocked is a
  protocol violation:** never let an operator approval turn into an invisible stall
  (the failure this codifies). The operator's absence is never approval — surface
  the block loudly and persistently, and take the other ready work meanwhile.
- **ATC (air-traffic control, alpha).** On a subscription-cap fleet, consult
  `coord-engine route <team> --needs <tags>` before a dispatch to pick the
  cheapest model that covers the work, and log the outcome after with
  `coord-engine usage log`; the ledger feeds the headroom fold and demotes a
  model that keeps failing a task class. Rubric, routing procedure, and the
  coordinator-join surface (`atc harvest`, bindings, `route --for-role`):
  [`fulcra-agent-atc`](skills/fulcra-agent-atc/SKILL.md).
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

End the commit message with the trailer
`Co-Authored-By: <your model> <noreply@anthropic.com>` (e.g.
`Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`).

## Credential custody (bot tokens)

Where a team runs a machine (bot) forge account, its credential custody is
team-store material, not repo material. The generic doctrine:

- A bot PAT is read at runtime from a secret store, **never embedded** — never
  in an OS scheduler plist (plaintext at rest) and never in argv (visible in
  `ps` and shell history).
- Rotation is **stage → verify → promote**: verify the candidate under a
  staging name before overwriting the incumbent — a piped empty value can wipe
  a keychain entry silently and still exit 0, so always paste at the
  interactive prompt — and rotate EVERY home the credential lives in BEFORE
  revoking the old token.
- Agents **cannot mint tokens** — an expiring token needs an armed operator
  reminder on the team bus. Never let a token value reach argv, stdout, shell
  history, a repo file, a scheduler plist, a log, or a chat transcript.

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

- **macOS CI is path-filtered and bills at 10×**, so it only runs on changes
  under `packages/**`, `skills/fulcra-agent-automation/**`, the workflow file
  itself, and the dependency manifests (`pyproject.toml`, `uv.lock`) — see
  [`.github/workflows/macos.yml`](.github/workflows/macos.yml);
  `test_ci_trigger_coverage.py` fails if trigger and coverage drift apart.
  Everything on Linux (`uv-workspace.yml`) runs on every push/PR to `main`.
  The upshot: for anything outside the macOS trigger set (docs, most skills),
  the **local run is the only pre-merge macOS check** — run the relevant suite
  before you push.
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
  set-setting set-interval plugin doctor`. There is **no `start`**; `doctor`
  runs the pre-flight diagnostic.
- `set-credential` (keychain) and `set-setting` (config.toml) are the headless
  pair for configuring a plugin without the wizard. `set-setting` validates
  against the plugin's declared `required_settings` and **refuses** any key
  declared as a `Credential` — every mistake it could otherwise make (typo'd
  plugin id or key, out-of-range enum) writes successfully and is then never
  read, so it must fail loudly at the CLI or not at all.
- Config dir `~/.config/fulcra-collect/`: `control.sock` (the UDS the menu-bar
  + CLI use), `web-url` (default `http://127.0.0.1:9292`), `web-token` (Bearer
  for the web API).
- Plugin authors needing independent durable cursors/state use RunContext's
  `kv_get` / `kv_set` / `kv_update` / `kv_delete` API. It is isolated by
  plugin ID and backed by `state.db`; values must be JSON (64 KiB maximum,
  256 UTF-8-byte keys). Use `kv_update` only for quick, side-effect-free atomic
  transforms because it holds SQLite's writer lock while the callback runs.

### Freshness: run status cannot tell you a source has died

`last_run` / `last_outcome` / `consecutive_failures` answer **did the plugin
run**, never **did it collect anything** — a source whose upstream goes quiet
keeps exiting cleanly with `last_outcome="done"`, so every health signal stays
green while the data stops. `freshness.py` supplies the missing half from two
independent clocks: `last_yield_at` (daemon clock — catches "runs fine,
produces nothing") and `newest_item_at` (source clock, **monotonic** so a
backfill cannot drag it backwards — catches a plugin that keeps writing while
upstream is frozen). Plugin authors: pass `observed_at=<ISO source timestamp>`
to `ctx.annotation(...)` (only the plugin knows when the thing happened
upstream), and opt in with `freshness=FreshnessExpectation(max_yield_silence=…,
max_upstream_lag=…)` — **monitoring is opt-in by design** (a bound guessed
from `default_interval` would cry wolf on legitimately rare sources; set
`max_upstream_lag` above normal quiet periods). A plugin that has never
yielded reports `UNKNOWN` — deliberately neither healthy nor stale; collapsing
"we have not looked" into "we looked and it is fine" is what let a four-day
outage read as green.

### launchd PATH gotcha

launchd runs the daemon with a restricted PATH
(`/usr/bin:/bin:/usr/sbin:/sbin`) and does NOT source your shell profile — so
`~/.local/bin` (where `uv tool install fulcra-api` puts the `fulcra` CLI) is
invisible. Any code shelling out to the `fulcra` CLI must resolve it via
`credentials._find_fulcra_cli()` (PATH → `~/.local/bin` → homebrew), **never**
bare `shutil.which("fulcra")`.

**Second-order gotcha (bit the gmail relay twice):** resolving your OWN binary
to an absolute path is not enough if that binary shells out further —
`coord-engine` itself execs `fulcra-api` by bare name, so under the daemon's
PATH every relay emit silently no-oped for five days. When you shell out to a
tool that itself shells out, pass an `env` whose `PATH` includes the install
dirs (`relay._subprocess_env`) AND set `EnvironmentVariables.PATH` in the
daemon's launchd plist; keep both so a plist regeneration can't silently
reintroduce the outage.

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
