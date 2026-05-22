# fulcra-collect — design (sub-project 1: headless hub core)

**Date:** 2026-05-22
**Status:** Approved design, ready for implementation planning.

## Context and decomposition

The goal is to take the local-running helper tools in the `fulcra-tools`
monorepo — the attention relay, the media importers, the CSV importer,
the Day One importer — and package them into a plugin-oriented
background application a user runs on startup. That full product is too
large for one implementation plan, so it is split into three
sub-projects, each producing working software on its own:

1. **Headless hub core + plugin API** — *this spec.* The background
   daemon: plugin discovery, scheduling, service supervision,
   credentials, config, a control CLI. All existing importers adapted
   as plugins.
2. **Menubar / tray UI** — the user-facing shell on top of sub-project 1
   (macOS menubar, Linux tray). Its own spec later.
3. **Public packaging** — code-signing, notarization, installer,
   auto-update. Its own spec later.

This spec covers **sub-project 1 only**. It is headless: control is via
the `fulcra-collect` CLI; the menubar UI of sub-project 2 will talk to
the same daemon through the same control socket.

## Goal

A background daemon, `fulcra-collect`, that hosts the monorepo's local
helpers as plugins: it discovers them, schedules the periodic imports,
supervises the long-running services, stores their credentials, and
exposes status and control over a local socket.

## Package

- Project name: `fulcra-collect`
- Directory: `packages/collect/`
- Python module: `fulcra_collect`
- CLI command: `fulcra-collect`
- A `uv` workspace member alongside the other packages.
- Workspace dependencies: `fulcra-common`; plus the packages whose
  importers it adapts (`fulcra-attention`, `fulcra-media-helpers`,
  `fulcra-csv-importer`, `fulcra-dayone`); `keyring`; `click`.

## The plugin API

A plugin is a Python object discovered via the entry-point group
`fulcra_collect.plugins`. Any installed distribution that registers in
that group is discovered with no change to hub code — this is the
extension point third parties and teammates use.

### Types (`fulcra_collect/plugin.py`)

```
PluginKind = "service" | "scheduled" | "manual"

Permission        # an OS permission a plugin needs
  id              # e.g. "full-disk-access", "network-loopback-server"
  explanation     # human text — rendered by the sub-project-2 onboarding

Credential        # a secret a plugin needs
  key             # storage key, e.g. "lastfm-api-key"
  label           # human label
  help            # where/how the user obtains it

Plugin
  id                   # stable slug — "attention-relay", "lastfm", "dayone"
  name                 # human label
  kind                 # PluginKind
  default_interval     # timedelta | None — required for "scheduled", else None
  requires_network     # bool (default True) — gates scheduled dispatch when offline
  required_permissions # list[Permission]
  required_credentials # list[Credential]
  run(ctx: RunContext) # the work (see below)

RunContext             # built by the hub, passed into run()
  config               # this plugin's non-secret settings (from config.toml)
  credentials          # dict[key -> secret], resolved from the OS keychain
  fulcra_token()       # the Fulcra access token, via the existing fulcra-api path
  progress(event)      # report structured progress back to the core
  log                  # a logger scoped to this plugin + run
  state                # this plugin's persisted store (watermarks, last-run)
```

`run(ctx)` semantics by kind:

- **scheduled / manual** — performs one import pass and returns. The hub
  invokes it on a cadence (scheduled) or only on explicit request
  (manual).
- **service** — the long-running entrypoint; it blocks (e.g. the relay's
  `serve_forever`). The hub keeps it alive in a supervised subprocess.

**The watermark / backfill contract.** A scheduled plugin MUST import
incrementally from `ctx.state.watermark` — the timestamp (or cursor) of
the newest item it last imported — and advance the watermark at the end
of a successful run. It must never import "the last interval's worth" of
data. This is the contract that makes one run after a sleep or an
offline stretch back-fill the entire gap: a machine asleep for a week
wakes, the plugin runs once, and it imports everything since the
watermark. The watermark is persisted by the hub (see "Sleep, offline,
and backfill" below), survives restarts, and is the plugin's only source
of "where did I get to."

A plugin only *declares* what it needs (`required_permissions`,
`required_credentials`); the hub resolves and supplies them through
`RunContext`. A plugin never reads the keychain or the config file
directly. This keeps every plugin uniform and inspectable: what a plugin
can access is visible in its metadata before any of its code runs.

The Fulcra access token is **not** a hub-managed credential. Importers
already obtain it through `fulcra_common.BaseFulcraClient.get_token()`
(`fulcra auth print-access-token` / `FULCRA_ACCESS_TOKEN`).
`ctx.fulcra_token()` exposes that same path; the hub does not duplicate
or override `fulcra-api`'s auth. The hub keychain is only for
plugin-*source* secrets (Last.fm API key, Trakt token, Strava token).

## Hub core components

The core is one long-lived process. Files under `fulcra_collect/`:

| File | Responsibility |
|---|---|
| `plugin.py` | The API types above. |
| `registry.py` | Discover plugins via `entry_points(group="fulcra_collect.plugins")`; validate metadata; a plugin that fails to import or has invalid metadata is excluded and recorded as a load error — it never crashes the hub. |
| `config.py` | The hub config file, TOML at `~/.config/fulcra-collect/config.toml`: which plugins are enabled, per-plugin interval overrides, per-plugin non-secret settings. |
| `credentials.py` | A thin wrapper over `keyring` — get/set/delete a plugin's secrets in the OS keychain (macOS Keychain, libsecret/Secret Service on Linux). |
| `state.py` | Per-plugin persisted state under `~/.config/fulcra-collect/state/<plugin-id>.json`: last-run time, last outcome, last error, consecutive-failure count, and the plugin's own watermark store. This is the snapshot the CLI and the sub-project-2 UI read. |
| `scheduler.py` | For each enabled scheduled plugin, compute next-run from its last-run state plus its effective interval (default or override); fire a run when due. Manual plugins are never auto-fired. |
| `supervisor.py` | For each enabled service plugin, keep a worker subprocess alive: restart on exit/crash with exponential backoff; a crash-loop (too many restarts in a window) marks the plugin degraded and stops hammering. |
| `runner.py` | Execute one scheduled/manual run: spawn a worker subprocess, parse its JSON-line progress stream, enforce a per-run timeout, record the outcome to `state.py`. |
| `worker.py` | The worker-subprocess entrypoint (`fulcra-collect _worker <plugin-id>`): import the plugin, build the `RunContext`, call `run(ctx)`, emit JSON-line progress events on stdout. Runs in its own process so a plugin's dependencies, crashes, and leaks are isolated from the core. |
| `control.py` | The control plane — a Unix domain socket at `~/.config/fulcra-collect/control.sock` with a small JSON request/response protocol. Filesystem-permissioned; no TCP port. |
| `daemon.py` | The long-running core: load config + registry, start the supervisor and scheduler loops, serve the control socket. The entrypoint launchd/systemd runs. |
| `service_manager.py` | Generalizes `fulcra_attention/service_manager.py`'s launchd/systemd installer to install the *hub* daemon as the single background agent. |
| `cli.py` | The `fulcra-collect` Click CLI (see "Control plane / CLI"). |

## Data flow

1. **Boot.** launchd/systemd starts `fulcra-collect daemon`. The daemon
   loads config and discovers plugins. Each enabled service plugin → the
   supervisor spawns a worker. Each enabled scheduled plugin → the
   scheduler computes its next run from persisted last-run state.
2. **A scheduled run fires.** The runner spawns
   `fulcra-collect _worker <id>`. The worker imports the plugin, builds
   the `RunContext` (config + source credentials from the keychain +
   `fulcra_token()` + the plugin's watermark state), and calls
   `run(ctx)`. The worker streams JSON-line progress on stdout; the
   runner consumes it, advancing state `queued → running → progress →
   done | error`.
3. **A service plugin.** Its supervised worker subprocess runs the
   blocking `run(ctx)`. On exit or crash the supervisor restarts it with
   backoff.
4. **Control.** The CLI (and later the menubar UI) connect to the
   control socket, send a request (`{"cmd": "status"}`, etc.), and
   receive a state snapshot or an acknowledgement.
5. **Manual plugins** are never auto-run; they fire only on an explicit
   `fulcra-collect run <id>` (or the UI's "Run now").

## Sleep, offline, and backfill

The hub runs on laptops that sleep, close, and lose network for hours or
days. It must miss nothing and storm nothing when they wake. Four
properties, by design:

1. **No missed-run storm.** The scheduler decides a plugin is due from
   *time since its last run*, not a wall-clock cron. A machine asleep
   for eight hours wakes with each scheduled plugin overdue, and the
   scheduler returns each **once** — not once per missed interval. The
   daemon's tick loop uses a short relative sleep; system sleep suspends
   it and it resumes on wake, so catch-up happens within one tick of
   waking.

2. **One run back-fills the whole gap.** Because every scheduled plugin
   imports incrementally from its watermark (the contract above), the
   single catch-up run after a long sleep imports everything accumulated
   since — there is no separate "backfill mode." The watermark advances
   only on a successful run, so an interrupted or failed catch-up is
   retried, not skipped.

3. **Watermark persistence across the worker boundary.** A run executes
   in a worker subprocess; the plugin advances `ctx.state.watermark`
   there. The worker reports the final watermark in its result event;
   the runner — the single writer of plugin state in the core process —
   persists it alongside the run outcome. The watermark is therefore
   never lost to a worker exiting, and a fresh run always resumes from
   the last *successfully imported* point.

4. **Offline is deferral, not failure.** Each plugin declares
   `requires_network`. Before dispatching scheduled runs the daemon
   checks connectivity (a short-timeout probe); while the machine is
   offline it **skips** network-requiring scheduled plugins rather than
   running them into a guaranteed failure. A skipped run is not recorded
   as a failure and does not count toward the degraded threshold — so a
   day offline never marks a plugin degraded. When connectivity returns,
   the next tick dispatches the plugin and property 2 back-fills the
   offline gap. Service plugins and manual runs are unaffected by the
   connectivity gate (services are always supervised; manual runs are
   user-initiated and may fail fast if offline).

## Plugins adapted in this sub-project

Every importer in the monorepo is adapted as a plugin. Each adapter is a
thin `Plugin` object that wraps the package's existing import logic; the
import code itself is not rewritten.

Plugin kind is assigned by this rule, confirmed per importer against its
actual mechanism during implementation:

- **service** — runs a long-lived server: `attention-relay` (the
  loopback relay), `media-webhook` (the media `webhook_receiver`).
- **scheduled** — polls a remote API or a live local store incrementally
  with a watermark: the Last.fm, Trakt, Strava, Deezer, Spotify,
  YouTube, RSS, and Apple Podcasts importers.
- **manual** — consumes a user-supplied export file or a one-shot
  trigger: `dayone`, `csv`, and the Goodreads, Letterboxd, Netflix,
  Apple-takeout, Spotify-IFTTT importers.

Each scheduled plugin declares a `default_interval` appropriate to its
source (e.g. Last.fm hourly, Trakt every few hours); the user can
override any of them in `config.toml`.

## Credentials and config

- **Secrets** (plugin source credentials) live in the OS keychain,
  accessed only through `credentials.py`. A plugin declares them via
  `required_credentials`; the hub prompts for any that are missing
  (`fulcra-collect set-credential <plugin> <key>`) and injects them into
  `RunContext.credentials`.
- **Non-secret settings** — enabled/disabled, interval overrides,
  per-plugin options — live in `~/.config/fulcra-collect/config.toml`.
- **Per-plugin state** (watermarks, run history) lives under
  `~/.config/fulcra-collect/state/`.
- The Fulcra access token is out of this model — see the plugin-API
  section.

## Permissions model

Each plugin declares the OS permissions it needs (`required_permissions`)
with a human explanation. The core records, in each plugin's state,
whether each declared permission appears satisfied — checked by a
best-effort probe (e.g. attempting the read a Full-Disk-Access plugin
needs and catching the failure). A run whose permission is unsatisfied
fails fast with a clear message rather than a deep error. The core only
*declares and checks* permissions; the onboarding flow that *explains*
them to the user and walks them through granting is sub-project 2.

## Error handling

- A plugin run that raises → the worker records the error and exits
  non-zero; the runner records `last_error` and increments the
  consecutive-failure count. The next run is still scheduled — a
  transient failure does not disable a plugin. After a threshold of
  consecutive failures the plugin is marked **degraded** in status.
- A service worker that crashes → supervised restart with exponential
  backoff; a crash-loop marks the plugin degraded and stops restarting.
- A plugin that fails to import or has invalid metadata → excluded by
  the registry, recorded as a load error; the hub runs on.
- A worker that hangs → the runner's per-run timeout kills it and
  records a timeout outcome.
- A missing required credential or unsatisfied required permission → the
  run fails fast with a specific, human-readable message.
- The daemon process dying → launchd/systemd keep-alive restarts it;
  because all state is persisted to disk, schedules resume correctly.

## Control plane / CLI

`fulcra-collect` commands, all talking to the running daemon over the
control socket except `daemon` and `install`:

| Command | Does |
|---|---|
| `fulcra-collect daemon` | Run the core in the foreground (the entrypoint for launchd/systemd). |
| `fulcra-collect install` | Install the launchd/systemd user agent for the daemon. |
| `fulcra-collect status` | Print every plugin: kind, enabled, last run, last outcome, next scheduled run, degraded/load-error flags. |
| `fulcra-collect run <id>` | Trigger one run of a plugin now (the only way to fire a manual plugin). |
| `fulcra-collect enable <id>` / `disable <id>` | Toggle a plugin in config; the daemon picks up the change. |
| `fulcra-collect set-credential <id> <key>` | Prompt for and store a plugin secret in the keychain. |
| `fulcra-collect set-interval <id> <duration>` | Override a scheduled plugin's cadence. |
| `fulcra-collect _worker <id>` | Internal — the worker-subprocess entrypoint; not for direct use. |

The control-socket protocol is a small versioned JSON request/response;
the sub-project-2 UI is just another client of it.

## Testing

All tests use fakes/mocks — no live APIs, no real keychain prompts.

- **Plugin API + registry** — a fake plugin registered via a test
  entry-point; assert discovery, metadata validation, and that a
  bad/invalid plugin is excluded with a recorded load error.
- **Scheduler** — with an injected fake clock: next-run computation,
  interval override, that manual plugins never auto-fire, that a plugin
  overdue by many intervals (a long sleep) is returned exactly once, and
  that network-requiring plugins are excluded while offline.
- **Supervisor** — a fake service worker that exits: assert restart,
  exponential backoff, and crash-loop → degraded.
- **Runner + worker** — round-trip one run: structured progress parsing,
  watermark carried from the worker's result event into persisted
  state, error capture, and the per-run timeout killing a hung worker.
- **Config** — TOML round-trip; enable/disable and interval overrides.
- **Credentials** — `keyring`'s in-memory backend: get/set/delete.
- **Control socket** — the request/response protocol for each command.
- **Adapted plugins** — each importer plugin is discovered and run by
  the hub; network-touching importers reuse their package's existing
  mock-transport test infrastructure.

## Out of scope

- The menubar / tray UI and the permission-explaining onboarding flow —
  sub-project 2.
- Code-signing, notarization, the installer, auto-update — sub-project 3.
- Rewriting any importer's import logic — adapters wrap existing logic.
- Managing the Fulcra access token — that stays with `fulcra-api`.

## Future plugins (not in scope, recorded for later)

- **openwearables.io** — an open wearables-data source; a strong
  candidate for a scheduled-importer plugin family once the hub is
  stood up.
- **Apple Health** — viable as an export-file (`export.zip` /
  `export.xml`) manual plugin; HealthKit has no live macOS store, so it
  is a file importer, not a local-DB read.
