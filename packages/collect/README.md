# fulcra-collect

The background process behind Fulcra Collect. It runs imports, keeps their
progress, and serves the setup wizard and dashboard.

**Using Collect?** Start with the [Mac installer and setup guide](../../docs/collect.md#get-started-new-user).
Apple Notes is included. You do not need to install this Python package separately.
The [plugin status](../../docs/collect.md#plugin-status) lists what's available
and what's still being built.

The rest of this README is for contributors and source installations. The
[system specification](../../docs/coord/SYSTEM-SPEC.md) covers the contract
between Collect, Coord Engine, and the agent bus.

The daemon hosts
every Fulcra Collect *plugin* — the periodic importers, the long-lived
webhook receivers, the pointer plugins — under one
process, supervises them, exposes their state over a JSON API plus a
web UI on `127.0.0.1:9292`, and stores per-plugin state in a single
SQLite database at `~/.config/fulcra-collect/state.db`.

The daemon, the menubar app (`packages/menubar`), the web UI
(`packages/web-ui`), and every helper that publishes data to Fulcra
(`packages/media-helpers`, `packages/attention`, `packages/dayone`,
…) all sit on this package. Anything new that wants to import a data
source into a Fulcra account becomes a `fulcra-collect` plugin and gets
the scheduler, credential storage, dashboard, wizard, and OAuth
plumbing for free.

## What it does

* **Discovers plugins.** Any installed Python distribution that
  registers under the `fulcra_collect.plugins` entry-point group is
  picked up at startup. A plugin declares one of three kinds:
  `scheduled` (an importer fired on a default interval), `service`
  (a long-running process the daemon supervises with restart-back-off),
  or `manual` (only fires when the user clicks Run).
* **Runs them in worker subprocesses.** Each scheduled or manual run
  spawns a fresh `fulcra-collect _worker <id>` process, so a crashing
  importer can never take down the hub. The worker streams structured
  progress and annotation events back to the parent over a pipe; the
  parent records them in the unified state store and in an in-memory
  ring buffer that powers the dashboard's "Recently" feed.
* **Stores secrets in the OS keychain.** Per-plugin credentials live
  under a `fulcra-collect:<plugin-id>` service name; the user-level
  Fulcra bearer token shares one namespace (`fulcra-collect:user`)
  across the whole hub.
* **Serves a web UI.** A FastAPI app bound to `127.0.0.1:9292`
  serves the wizard / dashboard / settings frontend out of
  `packages/web-ui/dist/` and answers the JSON API described below.
  The port is stable across restarts so that OAuth redirect URIs
  registered with external providers don't break when the daemon
  restarts.
* **Auto-launches the macOS menubar app** on startup when one is
  installed, so the user always has a visible status indicator
  without remembering a second command.

## Running from source

The Mac installer includes the daemon, runtime, and bundled plugins. A source
installation discovers only the plugin packages installed in its Python
environment. Installing `packages/collect` alone does not install them all.

From a checkout of this monorepo:

```bash
uv run --directory packages/collect fulcra-collect daemon
# → web UI at http://127.0.0.1:9292
```

Or install it as a standalone tool plus a launchd user agent that
brings it up on login:

```bash
uv tool install --force --editable packages/collect
fulcra-collect install         # writes ~/Library/LaunchAgents/com.fulcra.collect.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.fulcra.collect.plist
```

On Linux, `install` writes a `~/.config/systemd/user/fulcra-collect.service`
unit file instead, and you'd `systemctl --user enable --now fulcra-collect`.

For the macOS UI, follow the [menubar development instructions](../menubar/README.md#run-in-dev-mode).
The downloadable app handles background-service installation from its menu.

### CLI

```text
fulcra-collect daemon                       run the hub in the foreground
fulcra-collect install                      install the launchd/systemd user agent
fulcra-collect status                       list every plugin: kind, enabled, last run
fulcra-collect enable  <plugin-id>          enable a plugin
fulcra-collect disable <plugin-id>          disable it
fulcra-collect run     <plugin-id>          trigger one run now (via the running daemon)
fulcra-collect set-interval <plugin-id> N   override a scheduled plugin's cadence (seconds)
fulcra-collect set-credential <plugin-id> <key>
                                            stash a secret in the OS keychain (hidden prompt)
fulcra-collect set-setting <plugin-id> <key> <value>
                                            set a NON-SECRET setting in config.toml
fulcra-collect plugin reset-definition <plugin-id>
                                            clear the cached Fulcra def_id (re-resolve next run)
```

`enable`, `disable`, `set-interval`, `set-setting` write to `config.toml`
even when the daemon isn't running and signal `reload` when it is. `run` and
`status` need a running daemon; they talk to it over a Unix-domain
control socket at `~/.config/fulcra-collect/control.sock`.

`set-credential` and `set-setting` are the two halves of configuring a
plugin without the wizard, and the split is a security boundary, not a
convenience: secrets go to the OS keychain, everything else to a plaintext
`config.toml`. `set-setting` therefore **refuses** a key the plugin declared
as a `Credential` and points you at `set-credential` instead. It also
validates against the plugin's declared contract — unknown plugin id,
unknown setting key, a value outside an `enum`'s declared values, or a
non-numeric `port` are all rejected rather than written, because every one
of those writes succeeds silently and then is never read by anything.

```console
$ fulcra-collect set-credential purpleair api_key   # prompts, hidden
$ fulcra-collect set-setting purpleair mode api
purpleair: mode = 'api'
$ fulcra-collect set-setting purpleair sensor_index 142559
purpleair: sensor_index = '142559'
```

## Module layout

The package is roughly split into the daemon core, the persistence
layer, the worker plumbing, and the HTTP surface.

```
fulcra_collect/
    cli.py                  click entry points — `fulcra-collect …`
    daemon.py               Daemon: control-socket request handler, scheduler tick,
                            service supervision, account-fingerprint pre-flight,
                            quick-record + delete-annotation dispatch
    config.py               Config dataclass + config.toml round-trip
    credentials.py          OS keychain shim (per-plugin + user-level namespaces)
    registry.py             entry-point discovery, RegistryResult
    scheduler.py            due_plugins(): which scheduled plugins are due now
    supervisor.py           ServiceSupervisor: keep service plugins alive with back-off
    runner.py               spawn a worker subprocess, consume its stream, record state
    worker.py               in-subprocess plugin runner; builds the RunContext;
                            adapts fulcra-common's HTTP client for def resolution
    control.py              Unix-domain-socket request/response server (CLI ↔ daemon)
    oauth.py                PKCE state for browser-OAuth plugins (Trakt, Spotify, …)

    plugin.py               THE PLUGIN CONTRACT — Plugin / Credential / Setting /
                            SetupStep / HealthResult / RunContext dataclasses

    db.py                   SQLite connection lifecycle + schema migrations
    state.py                PluginState (typed wrapper over db.fetch/upsert)
    activity.py             in-memory ring buffer of recent annotations (dashboard feed)

    web.py                  FastAPI app factory + uvicorn launcher
    routes/                 per-area HTTP route modules; each exports register(app, ctx)
        _deps.py            RouteContext + Pydantic body models shared across routes
        status.py           /api/status, /api/version, /api/reload
        plugins.py          /api/plugin/{id}/{run,enable,disable,credentials,settings,
                            contract,setting_options/{key},health_check,check_permission,
                            request_permission,upload}
        definitions.py      /api/definitions, /api/plugin/{id}/definition (bind/clear)
        fulcra_auth.py      /api/fulcra/auth/{status,token,cli_status,cli_login}
        oauth.py            /api/oauth/{plugin_id}/{start,callback}
        annotations.py      /api/annotations  POST + DELETE  (quick-record write/undo)
        activity.py         /api/activity, /api/quick-record/{definitions,favorites}
        menubar.py          /api/menubar/{status,launch}
        docs.py             /api/docs/{name}        — serves repo-root docs/

    menubar_launcher.py     best-effort spawn of the macOS menubar app on startup
    quick_record_favorites.py
                            per-machine favorites file (`quick_record_favorites.json`)
    service_manager.py      launchd plist / systemd unit installer
```

`web.py` constructs the FastAPI app, bootstraps its local auth token
(`~/.config/fulcra-collect/web-token`), creates the Fulcra HTTP client factory,
and mounts the frontend. It passes a shared `RouteContext` to each
`routes/*.py` module's `register()` function. The route modules import `httpx`
through `fulcra_collect.web`
deliberately so the existing `monkeypatch.setattr(web, "httpx", …)`
test idiom keeps working.

### Plugin contract

A plugin is a `Plugin` instance discovered via setuptools entry points:

```toml
# in the plugin package's pyproject.toml
[project.entry-points."fulcra_collect.plugins"]
dayone = "fulcra_dayone.collect_plugin:PLUGIN"
```

The object (or callable returning one) is a `Plugin` dataclass
declaring `id`, `name`, `kind`, the `run(ctx)` callable, plus optional
`required_credentials`, `required_settings`, `required_permissions`,
`setup_steps` (the wizard renders these), `health_check`,
`permission_check`, `permission_request`, `setting_options`, OAuth callables, a category, and a
`canonical_definition_name`. The daemon builds a `RunContext` for
every invocation and passes it in; the plugin reaches for its config,
credentials, state, and the Fulcra def-resolver through the context
rather than touching the filesystem or keychain directly.

Long-lived plugins store independent cursors and per-entity state through
`ctx.kv_get`, `ctx.kv_set`, `ctx.kv_update`, and `ctx.kv_delete`. Values are
plugin-isolated JSON (64 KiB per value; 256 UTF-8 bytes per key) in `state.db`.
Use `kv_update` for a quick, side-effect-free atomic read/modify/write when
multiple worker processes may touch the same key.

For examples, read [Day One](../dayone/README.md) for scheduled imports and
[Apple Notes](../apple-notes/README.md) for vault files, access checks, and an
explicit start action. A new plugin needs its setup instructions and limitations
in its README and the [source guide](../../docs/how-do-i-get-my-data.md) in the
same PR.

### Native permission requests

Keep `Plugin.permission_check(ctx)` read-only: the wizard may invoke it when a
permission step opens or the user clicks **Verify access**. Native prompts belong
in the separate optional `Plugin.permission_request(ctx)` callback, which the
wizard invokes only after the user clicks **Allow access**. Both callbacks return
`{"granted": bool, "hint": str | None}` and receive current saved settings and
correctly scoped Keychain credentials. Permission callbacks must not enable the
plugin or start synchronization.

The contract exposes `permission_request_available`. The authenticated
`POST /api/plugin/{id}/request_permission` endpoint calls only that callback;
`POST /api/plugin/{id}/check_permission` calls only the read-only check.
Unsupported actions return 404; provider, credential-access, or malformed-result
failures return `granted: false` with a generic hint, without exception details.
Full Disk Access continues to use **Open System Settings** and **Verify access**
when no native request callback is declared. The Mac app includes this contract starting with 0.1.2.

### Discovered multiselect settings

Collect supports a reusable list picker through `Setting(kind="multiselect")`
and `Plugin.setting_options`. The Mac app includes it starting with 0.1.2.

Declare a setting and include its key in an `input` setup step:

```python
selected_lists = Setting(
    key="selected_lists", label="Lists to sync", kind="multiselect",
    default=[], required=True,
)
list_step = SetupStep(
    kind="input", title="Choose lists", settings_keys=("selected_lists",),
)

# Attach this callback to Plugin(setting_options=discover_options, ...).
def discover_options(ctx, key):
    if key != "selected_lists":
        raise ValueError("Unsupported setting")
    provider = make_provider(ctx)  # plugin-owned adapter; reads ctx.credentials
    return [
        {"value": item.id, "label": item.name, "disabled": not item.writable}
        for item in provider.collections()
    ]
```

The callback receives a `RunContext` containing saved settings and each declared
credential from its plugin or user Keychain scope. It must only discover choices,
use bounded source calls, and raise if source access or a partial read fails.
It must never enable the plugin, start synchronization, or log credentials.
The lightweight discovery context does not expose worker KV write callbacks.

`GET /api/plugin/{id}/setting_options/{key}` requires the normal bearer token
and returns `{"options": [{"value": "opaque-id", "label": "Example list"}]}`
with `Cache-Control: no-store`. IDs must be unique, nonempty strings; labels
must be nonempty strings. The only optional option field is `disabled`, a
boolean. An unsupported plugin, setting, or callback returns 404. Discovery
failures and malformed callback results return a sanitized 503, while a
successful discovery with no lists returns `{"options": []}`.

`PUT /api/plugin/{id}/settings` accepts multiselect values only as arrays of
unique nonempty string IDs. Saving never calls discovery or rejects an ID
because its list is currently missing. An empty array can be saved to deselect
all lists, but the HTTP enable route rejects empty required selections. Plugins
must enforce selection scope in their own run logic as well. Use the wizard or
JSON settings API for these arrays; the text-oriented CLI is not an array editor.
The shared wizard preserves selected IDs, displays missing selections for
removal, and requires an explicit Enable action after configuration.

## HTTP API surface

All routes except the OAuth callback require a bearer token from
`~/.config/fulcra-collect/web-token` (seeded into a cookie by the
HTML root). Grouped by concern; see `routes/*.py` for the exact
shapes.

* **Status / version** (`routes/status.py`) — `GET /api/status`,
  `GET /api/version`, `POST /api/reload`.
* **Plugin operations** (`routes/plugins.py`) — run, enable/disable,
  read/write credentials, read/write settings, discover setting options, fetch contract,
  health-check, permission-check, explicit permission-request, file upload (multipart, used by the
  wizard's `file_upload` step).
* **Annotation definitions** (`routes/definitions.py`) — list defs on
  the Fulcra account, bind one to a plugin, list a def's recent
  events, soft-delete a def.
* **Fulcra auth** (`routes/fulcra_auth.py`) — set/clear the user-level
  bearer token, probe the `fulcra` CLI as a fallback source.
* **OAuth** (`routes/oauth.py`) — `POST /api/oauth/{plugin_id}/start`
  and `GET /api/oauth/{plugin_id}/callback`. PKCE state lives in
  `oauth.py`; the callback is the only unauthenticated route because
  the provider redirects the browser to it.
* **Quick-record** (`routes/annotations.py` + `routes/activity.py`) —
  `POST /api/annotations` writes a Moment or Duration directly;
  `DELETE /api/annotations/{source_id}` writes a tombstone (Fulcra
  has no hard-delete primitive for events). `GET/PUT
  /api/quick-record/favorites` round-trips the per-machine favorites
  file.
* **Browser extension** — there is no daemon-side route. The Fulcra
  Attention Chrome extension is fully relayless: it signs in via an
  Auth0 device flow and POSTs records directly to the Fulcra API
  (`https://api.fulcradynamics.com/ingest/v1/record/batch`). Collect's
  only involvement is the `attention-relay` pointer plugin, which tells
  the user to install the extension and sign in via the browser. The
  former `routes/extension.py` (`POST /api/extension/attention`) and the
  `/api/plugin/attention-relay/pair` pairing route have been removed.
* **Menubar** (`routes/menubar.py`) — status + relaunch endpoints so
  the dashboard can show "Launch menubar app" when the user has
  accidentally quit it.
* **Docs** (`routes/docs.py`) — serves the repo's `docs/*.md` so
  wizard help links can deep-link to the same source the README
  references.

## State and storage

Everything lives under `~/.config/fulcra-collect/` (override via
`FULCRA_COLLECT_HOME`):

| Path | Purpose |
|---|---|
| `config.toml`                  | Per-plugin enabled flag + interval overrides + `[daemon] web_port`. |
| `state.db`                     | SQLite (WAL mode) — single source of truth for per-plugin run state and plugin-scoped JSON/KV. Schema migrations live in `db.py`; current version is 6. |
| `control.sock`                 | Unix-domain socket the CLI talks to. |
| `web-token`                    | Random bearer token (0600) seeded on first boot, mounted into the web UI as a cookie. |
| `web-url`                      | The currently-bound web URL — read by the menubar and ad-hoc tools. |
| `auth-fingerprint`             | SHA-256 prefix of the bearer token at last boot. Drives the account-switch pre-flight that invalidates cached `def_id`s when the user re-auths to a different Fulcra account. |
| `quick_record_favorites.json`  | The user's pinned annotation defs for the menubar popover. |
| `state/<plugin-id>.json.migrated` | Leftovers from the JSON → SQLite migration in `db.py:_migration_002`. Safe to delete once the soak period is over. |

Credentials never touch the config directory — they go to the OS
keychain via `credentials.py` (Keychain on macOS, Secret Service on
Linux, Windows Credential Manager on Windows; everything `keyring`
supports).

## Tests

```bash
uv run --package fulcra-collect pytest packages/collect/tests/ -q
```

The suite covers the daemon's request handlers, the scheduler /
supervisor, the SQLite migration path, every route module, the
account-fingerprint pre-flight, and the worker subprocess plumbing.
`tests/test_setting_options.py` covers synthetic option discovery, scoped
credentials, malformed responses, saved arrays, and the required-selection
enable gate. `tests/test_permission_request.py` verifies explicit-only native
permission requests, authentication, credential scope, and sanitized failures.
`tests/test_end_to_end.py` exercises a full
discover → enable → run → state-write loop against a stub plugin.

### Configuration changes cancel pending task writes

Workers receive `RunContext.config_epoch` with their settings snapshot. Collect
rotates that opaque revision when plugin settings, enablement, or credentials
change through the app or CLI. The task plugins check it before each write, so
turning preview on, disabling a plugin, removing a list, or reconnecting an
account cancels pending work. Interactive Fulcra account sign-in or sign-out also
invalidates pending task writes before the token changes; routine automatic token
refresh does not. Returning to an earlier selection starts a fresh
baseline. Configuration saves serialize across processes and replace the file
atomically; a concurrent save cannot restore an older revision. Direct edits to
`config.toml` bypass this lifecycle tracking.

The CLI accepts multiselect values as JSON arrays of unique, nonempty string IDs:
`fulcra-collect set-setting apple-reminders selected_lists '["example-list-id"]'`.
Use `[]` to clear the selection; a required empty selection cannot be enabled.
The dashboard discovers IDs and labels for you, so it is the easier setup path.
