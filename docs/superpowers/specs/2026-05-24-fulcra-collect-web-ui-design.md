# fulcra-collect web UI + onboarding — design (sub-project 2.5)

**Date:** 2026-05-24
**Status:** Draft. Pending user review.

## Context

Sub-project 2 (the Python+PyObjC+rumps menubar) shipped a working
at-a-glance surface: click menubar icon → popover → see plugin status,
Run-now manual plugins, gear → Preferences. Smoke testing surfaced
five real gaps that the existing menubar UI can't comfortably close:

1. **No Fulcra auth flow.** Every plugin that posts to Fulcra declares
   its own `bearer-token` credential, so the user pastes the same
   token into N plugins' fields. There's no sign-in step. Token
   rotation means re-pasting N times. This is wrong architecturally —
   the token belongs to the user/daemon, not to each plugin.
2. **No per-plugin settings UI.** File-based importers (`netflix`,
   `apple-takeout`, `youtube`, `spotify-extended`, `apple-podcasts-timemachine`,
   `dayone`, `generic-csv`) need a file path before they can do
   anything. `generic-rss` / `generic-csv` need a category choice
   (Watched/Listened/Read) and (for RSS) a feed URL.  `media-webhook`
   needs a port + auth token. None of these are addressable from the
   menubar today — they require hand-editing `~/.config/fulcra-collect/config.toml`.
3. **No first-launch onboarding.** Someone installing the menubar
   today sees 16 toggles with descriptions but no narrative path. The
   user asked "how does someone know what to do?".
4. **No definition picker.** The resolver auto-adopts by canonical
   name (great for the common case) but offers no UI to see *which*
   Fulcra definition a plugin will write into, or to choose a
   different existing definition, or to preview recent entries to
   confirm. User asked for this directly earlier.
5. **Enable toggle is meaningless for manual plugins.** The daemon
   doesn't poll manual plugins; "enable" only gates whether the
   Run-now button shows up. The honest UX is a single Run-now (or
   Import…) button for manual plugins, no toggle.

Plus a longer-term constraint surfaced this round: **the user wants
the option to support Windows later**. The current PyObjC+rumps stack
is Mac-only by construction. The eventual UX vision (popover's primary
surface becomes a "what to record now" list per
`project_post_smoke_onboarding_workstream.md`) also doesn't easily fit
in PyObjC.

This spec addresses all five of the above, sets up a cross-platform
foundation, and stays compatible with the existing menubar (which keeps
working unchanged during and after this lands).

## Goal

A **web-UI hybrid** for fulcra-collect:

- The daemon gains a small HTTP server that serves a static
  HTML/CSS/JS frontend from `localhost:<ephemeral-port>`.
- The frontend covers Preferences, per-plugin settings, the
  first-launch onboarding wizard, the definition picker, and the
  Fulcra sign-in flow.
- The existing native menubar (sub-project 2) stays as the
  at-a-glance status surface. Clicking the gear or any "configure"
  affordance opens the web UI in the user's default browser.
- The plugin contract gains a `Setting` declaration parallel to
  `Credential`, plus the daemon owns a single shared user-level Fulcra
  token (plugins drop their per-instance `bearer-token` credential).

The web UI is automatically cross-platform; the menubar stays
Mac-only for now but is the smallest part to re-shell later (a thin
Tauri/Electron tray launcher could give Windows / Linux users a "click
to open browser" entry point with the same daemon + same web UI
underneath).

## Stack decision (why web UI hybrid over Swift / Tauri / Electron)

Honest scorecard against the constraints (cross-platform potential,
iteration speed, sunk cost, ship-soon timeline):

- **Stay on Python+PyObjC for Preferences too** — rejected. Form
  rendering, file pickers, dynamic per-plugin layouts, an onboarding
  wizard, and a definition-picker sheet in PyObjC is a lot of code
  for a UI that's still Mac-only. Every framework friction we already
  hit (rumps menu API, `_nsapp` timing, `_T` class collision,
  py2app/Python 3.14) compounds with more PyObjC surface area.
- **Pivot to SwiftUI** — rejected against the cross-platform
  constraint. SwiftUI is excellent for the Mac case but doesn't run
  on Windows; committing to it now means writing a second app later.
- **Pivot to Tauri (Rust shell + HTML/CSS UI)** — viable. Real
  cross-platform native shell, small bundle, modern stack.  Cost:
  introduces Rust to the toolchain; the existing menubar work gets
  thrown away.
- **Web UI served by the daemon, existing menubar kept as-is** —
  chosen. The daemon's already a long-lived Python process; an HTTP
  server module is ~50 lines. The frontend is HTML/CSS/JS — fastest
  iteration of any stack, no build step needed for v1, and works on
  any OS that has a browser. The menubar work is preserved; only the
  Preferences pane's role changes (it becomes a launcher for the web
  UI). Windows portability later = a small Tauri/Electron tray shell,
  not a rewrite of the actual UI.

The hybrid path lets us ship the bigger workstream's substance
quickly without locking in a platform choice for the menubar surface.

## Package

- New package: `packages/web-ui/`
- Frontend tech: HTML5 + CSS3 + vanilla JavaScript + Alpine.js (≈10KB,
  no build step; CDN or inlined). Tailwind via CDN for the brand-on-white
  palette. No bundler initially; if the UI grows, we can add a
  Svelte/Preact/build step later.
- Backend tech: FastAPI inside the daemon (new dep). FastAPI's chosen
  for async HTTP, automatic OpenAPI docs (handy for the frontend),
  pydantic for request validation. ~30 deps added including Starlette
  and pydantic, but they're small and well-maintained.
- Static assets ship in `packages/web-ui/dist/` (hand-written; no
  bundler step). Daemon mounts that directory at the root.

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│  fulcra-collect daemon (Python, cross-platform)             │
│  ┌──────────────────┐  ┌──────────────────┐  ┌───────────┐ │
│  │ UDS control      │  │ HTTP server      │  │ Scheduler │ │
│  │ socket (existing)│  │ (new, FastAPI)   │  │ Worker    │ │
│  └────────▲─────────┘  └────────▲─────────┘  └───────────┘ │
│           │                     │                            │
│           │                     │ serves static + JSON API   │
│           │                     ▼                            │
│           │            /static/* → packages/web-ui/dist/*    │
│           │            /api/*    → handlers below            │
└───────────┼─────────────────────┼────────────────────────────┘
            │                     │
            │                     │
   ┌────────▼─────────┐  ┌────────▼──────────┐
   │ existing CLI     │  │ Web UI (HTML/CSS/ │
   │ (fulcra-collect) │  │  JS in user's     │
   │                  │  │  default browser) │
   └──────────────────┘  └───────────────────┘
                                   ▲
                                   │  opens
                                   │
                          ┌────────┴──────────┐
                          │ Menubar app       │
                          │ (existing PyObjC) │
                          │ Gear → opens URL  │
                          └───────────────────┘
```

The daemon now serves **two** interfaces in parallel:

- The **UDS control socket** (existing) — fast, local-only, used by
  the CLI and the menubar's at-a-glance polling.
- The **HTTP server** (new) — serves the web UI static files plus a
  JSON API parallel to the UDS commands. The menubar doesn't use HTTP;
  the web UI doesn't use UDS.

Both interfaces are entry points into the same Daemon class. The
HTTP handlers call into the same `Daemon.handle_request`-style logic
the UDS handlers do, just with different transport plumbing.

## Communication: daemon HTTP server

### Bind

- Bind to **127.0.0.1** only (loopback). Never binds to a public
  interface; never accessible from another machine.
- Pick an **ephemeral port** (`socket.bind(("127.0.0.1", 0))`).
  Daemon writes the resulting URL to
  `~/.config/fulcra-collect/web-url` (mode 0600). The menubar reads
  that file when the user clicks the gear button.

### Auth

- A randomly-generated **web token** (32 bytes, base64) is created
  on first daemon start and stored at
  `~/.config/fulcra-collect/web-token` (mode 0600).
- Frontend includes the token as `Authorization: Bearer <token>` on
  every API request. The token is fetched once at page load via a
  cookie set by the daemon on the initial HTML response — the HTML
  response sets a `fulcra_token` cookie containing the value, and JS
  reads it for subsequent calls.
- This is enough security for a localhost-only service: a process
  running as the same user could read the token file anyway (file
  is 0600; the threat model is the same as any local secret); a
  process running as a different user can't read the file at all.
- No CORS configured — the frontend and backend share the origin.

### HTTP routes

**Static**:

| Route | Behaviour |
|---|---|
| `GET /` | Serves `packages/web-ui/dist/index.html`. Sets the `fulcra_token` cookie. |
| `GET /static/*` | Serves any other file from `packages/web-ui/dist/`. |

**JSON API** (all require the Bearer token):

| Route | Maps to | Notes |
|---|---|---|
| `GET /api/status` | UDS `status` | Current plugin snapshot. |
| `POST /api/plugin/{id}/run` | UDS `run` | Triggers a run. |
| `POST /api/reload` | UDS `reload` | Re-reads config. |
| `GET /api/version` | UDS `version` | Daemon + plugin versions. |
| `GET /api/plugin/{id}/credentials` | UDS `credential_status` | Per-credential set/missing. |
| `PUT /api/plugin/{id}/credential/{key}` | UDS `set_credential` | Body: `{"secret": "..."}`. |
| `DELETE /api/plugin/{id}/credential/{key}` | UDS `delete_credential` | |
| `GET /api/plugin/{id}/settings` | **NEW** | Read non-secret per-plugin settings (file path, URL, category, etc.). |
| `PUT /api/plugin/{id}/settings` | **NEW** | Body: `{"key": "value", ...}`. Persists to `config.toml`'s `plugin_settings.<id>` and calls reload. |
| `GET /api/fulcra/auth/status` | **NEW** | Returns `{authenticated: bool, expires_at?: str, account?: {...}}` (the shared user-level Fulcra token's state). |
| `POST /api/fulcra/auth/token` | **NEW** | Body: `{"token": "..."}`. Stores in keychain as `fulcra-collect:user:bearer-token`. |
| `DELETE /api/fulcra/auth/token` | **NEW** | Forgets the shared Fulcra token. |
| `GET /api/definitions?annotation_type=<type>` | **NEW** | Lists existing Fulcra definitions matching a schema (used by the definition picker). |
| `GET /api/definitions/{id}/recent?limit=N` | **NEW** | Last N annotations for a given definition id (used by the picker preview). |
| `POST /api/plugin/{id}/definition` | **NEW** | Body: `{"definition_id": "...", "force_new": false}`. Bind a plugin to a chosen definition or force-create. |
| `DELETE /api/plugin/{id}/definition` | **NEW** | Clear the cached definition_id; next run re-resolves. |

The "new" routes are pure additions; nothing the menubar uses today
changes.

## Plugin contract changes

### 1. Drop per-plugin `bearer-token` from `required_credentials`

Today's `attention-relay`, `media-webhook`, and others declare:

```python
required_credentials=(
    Credential(key="bearer-token", label="bearer-token", help="..."),
)
```

This is wrong. The bearer-token is the **user's Fulcra access token**,
not a plugin-specific credential. The fix:

- Remove the `bearer-token` `Credential` from every plugin that has it.
- The shared user-level token lives in keychain at
  `fulcra-collect:user:bearer-token`.
- `RunContext.fulcra_token()` reads from that shared keychain entry
  (it already exists conceptually — this just centralises the storage).
- Plugins drop their per-instance bearer-token UI fields automatically
  (no more N times paste).

**Migration**: on first daemon start after this lands, if the shared
keychain entry is empty AND any per-plugin `bearer-token` exists,
copy one of them into the shared location and delete the per-plugin
entries. Pick the per-plugin entry deterministically (e.g., from
`attention-relay` if present, else the first plugin alphabetically).

### 2. Add `required_settings: tuple[Setting, ...]` to `Plugin`

Parallel to `required_credentials`. A `Setting` is a non-secret
configurable value the user provides via the UI:

```python
@dataclass(frozen=True)
class Setting:
    key: str
    label: str
    kind: Literal["text", "path", "url", "port", "enum", "toggle", "interval"]
    help: str = ""
    enum_values: tuple[str, ...] | None = None  # required for kind="enum"
    default: object = None                       # default value, optional
    required: bool = True                        # if False, plugin runs even when empty
```

Examples:

```python
# netflix
required_settings=(
    Setting(key="csv_path", label="Viewing-history CSV",
            kind="path",
            help="Download from netflix.com/Activity"),
)

# generic-rss
required_settings=(
    Setting(key="feed_url", label="RSS feed URL", kind="url"),
    Setting(key="category", label="Category", kind="enum",
            enum_values=("watched", "listened", "read"),
            default="watched"),
)

# media-webhook
required_settings=(
    Setting(key="port", label="Listen port", kind="port", default=7322),
    Setting(key="auth_token", label="Webhook auth token",
            kind="text", help="Shared secret between your media server and this webhook"),
)
```

Storage: `config.toml`'s existing `plugin_settings` table — already
in the schema, currently unused. Each plugin gets a sub-table:

```toml
[plugin_settings.netflix]
csv_path = "/Users/scanning/Downloads/NetflixViewingHistory.csv"

[plugin_settings.generic-rss]
feed_url = "https://example.com/feed.xml"
category = "watched"
```

`RunContext.config` already surfaces these to plugin code (the slot
exists; plugins just don't have a way to declare what they expect
or for users to provide values).

### 3. Keep per-plugin `required_credentials` ONLY for third-party secrets

After the bearer-token cleanup, remaining credentials are
plugin-specific (Last.fm API key, Trakt OAuth refresh token, Deezer
access token, etc.). These stay in the plugin's `required_credentials`
declaration and stay in the per-plugin keychain location.

## Web UI surfaces

### First-launch onboarding wizard (`/onboarding`)

Renders when:

- No shared Fulcra token is set, OR
- No plugins are enabled.

Steps:

1. **Welcome** — one-paragraph overview of Fulcra Collect.
2. **Sign in to Fulcra** — either:
   - Device-flow OAuth: app shows a 6-digit code, link to
     `https://fulcra-dynamics.com/device`. Backend polls Fulcra's
     OAuth endpoint until the user completes the flow. Token returned
     and stored. (Future preference; needs the Fulcra OAuth endpoint
     to exist.)
   - Paste-token fallback (v1): "Open
     `https://fulcra-dynamics.com/account/tokens`, generate one,
     paste here." One-time paste. Backend validates the token via a
     test API call before saving.
3. **Pick plugins** — checklist of the 16 bundled plugins, grouped
   by kind (Services, Scheduled, Manual). Each shows its description.
   User checks the ones they want.
4. **Configure each enabled plugin** — for each, walk through the
   per-plugin settings + credentials. File picker for `path` kind,
   text inputs for the rest. Skip plugins with no settings or
   credentials (just confirm).
5. **Done** — "You're set up. Bring up the menubar icon any time to
   run / monitor plugins. To revisit settings, click the gear in the
   menubar."

The wizard is dismissible at step 1; user can come back via
"Settings → Sign in" later.

### Preferences (`/preferences`)

Tabs: **Plugins**, **Fulcra account**, **Notifications**, **About**.

**Plugins tab**:

- Search bar at the top (filter by name / description).
- Grouped by kind. Each row's UI depends on kind:

| Kind | Row UI |
|---|---|
| `service` | Enable toggle + per-plugin credentials/settings (expandable). |
| `scheduled` | Enable toggle + interval input (humanised: "Every 60 minutes (1 hour)") + per-plugin credentials/settings + Run-now (manual override). |
| `manual` | NO Enable toggle. Per-plugin credentials/settings + a prominent **Run now** / **Import…** button (depending on kind="path" presence). |

For each plugin, an expandable "Definition" section showing the
currently bound definition (id + name + last-entry timestamp) + a
"Change definition…" button (opens the picker — see below).

**Fulcra account tab**:

- Current sign-in state (authenticated or not).
- If authenticated: account info, "Sign out" button.
- If not: "Sign in to Fulcra" button (re-runs the sign-in step).

**Notifications tab**:

- Same as today's Preferences > Notifications.

**About tab**:

- App + daemon version, plugin versions, paths, Open Activity Logs,
  Launch-at-login (only applicable when menubar runs from a packaged
  `.app`; informational otherwise).

### Definition picker (`/preferences/plugin/{id}/definition`)

Modal or full-page sheet. Shows:

- Currently bound definition (if any), with last entry timestamp.
- List of OTHER Fulcra definitions matching this plugin's expected
  schema (annotation_type + measurement_spec). Each entry shows:
  - Definition name
  - Created date
  - Last entry timestamp
  - Inline preview: last 3 entries (compact: timestamp + summary line)
  - "Use this one" button → calls `POST /api/plugin/{id}/definition`
    with `definition_id=<chosen>`.
- "Create a new definition instead" button → calls the same endpoint
  with `force_new=true`.

### Popover-style "what to record now" (future Piece 4)

Out of scope for this spec — the post-smoke onboarding workstream
memory captures it as the *next* iteration after this one ships.

## Menubar app changes

Minimal:

- The **gear button** opens the user's default browser at the URL
  read from `~/.config/fulcra-collect/web-url`. The native
  `PreferencesController` window goes away.
- The **manual plugin row in the popover** drops the gating on
  `enabled`. Manual plugins always show the Run-now button. (Manual
  plugins are always-runnable; "enabled" has never had meaning for
  them.)
- The **first-launch trigger**: when the menubar starts and detects
  "no shared Fulcra token" + "no enabled plugins" (via
  `/api/fulcra/auth/status` + status reply's enabled count), it
  auto-opens the browser at `/onboarding`. Subsequent launches don't
  auto-open.

Everything else in the menubar stays: status item icon + states,
popover, plugin rows, polling, notifications, Quit footer.

## Plugin "kind" UI semantics (post-refactor)

To eliminate the toggle-meaningless-for-manual confusion:

| Kind | Daemon behaviour | UI surface |
|---|---|---|
| `service` | When enabled, daemon supervises (keep-alive, restart-on-crash). | Enable toggle = "supervise". Disabled service plugins don't run. |
| `scheduled` | When enabled, daemon fires at the interval. | Enable toggle = "include in cycle". Disabled scheduled plugins don't auto-fire. |
| `manual` | Never auto-fired. | NO Enable toggle. Run-now / Import… button is the only action. Always visible in the popover. |

The popover plugin list shows:

- All `service` plugins (with their current state).
- All ENABLED `scheduled` plugins.
- All `manual` plugins (always — they're always actionable).

## Communication with Fulcra (auth + token lifecycle)

- The shared user-level token lives in keychain at
  `fulcra-collect:user:bearer-token`.
- The daemon's existing `BaseFulcraClient` is constructed with that
  token whenever a plugin needs one.
- Token refresh: out of scope for v1 (assume long-lived personal
  access tokens, not OAuth-with-refresh). If/when Fulcra ships OAuth
  with refresh-token semantics, we add a refresh path in the daemon.
- "Sign out" deletes the keychain entry. Plugins that try to run
  without it fail with a clear error → next popover poll surfaces
  "Fulcra sign-in needed" + the daemon-stopped-style fallback card
  (or a new "fulcra-unauthenticated" state in the menubar).

## How it works — end-to-end first-launch

1. User downloads / runs `fulcra-collect` for the first time. Daemon
   starts, generates the web token, picks an ephemeral port, writes
   `~/.config/fulcra-collect/web-url`.
2. Menubar starts, reads the URL. Polls `/api/fulcra/auth/status` →
   `authenticated: false`. Polls `/api/status` → 0 enabled plugins.
   Auto-opens the browser at `/onboarding`.
3. User clicks through the wizard: Welcome → Sign in (pastes token) →
   picks 3 plugins (lastfm, attention-relay, netflix) → configures
   each (lastfm API key, attention bearer-token IS NOT ASKED because
   it's the shared Fulcra token now, netflix CSV path picker) → Done.
4. Wizard hits the API to enable selected plugins, write their
   settings + credentials, kick a reload.
5. Browser closes. Menubar's status poll shows the new state. Icon
   transitions from empty → healthy.
6. The next time daemon starts, no auto-open happens (token + plugins
   already configured).

## Testing

- **Backend**: pytest for the new FastAPI routes. Each route gets a
  test using FastAPI's `TestClient`. Existing UDS tests stay.
- **Frontend**: minimal (vanilla JS). Consider Playwright for an
  end-to-end smoke that opens the onboarding wizard, completes a
  fake sign-in (against a mock backend), enables a plugin, checks
  config.toml. Defer to v1.5 if it's too heavy for v1; rely on
  manual smoke for the first round.
- **Plugin contract**: `Plugin` dataclass + `Setting` + the
  removed-bearer-token retrofits get unit tests.
- **Migration**: the per-plugin → shared bearer-token migration is
  one-shot at daemon start; test with a synthetic keychain that has
  the old layout, run the migration code path, assert the new
  location has the token and the old locations don't.

## Cross-platform notes

- **Daemon**: Python, already cross-platform. The new FastAPI server
  adds no Mac-specific deps. Should run on Linux + Windows with
  `uv pip install fulcra-collect`.
- **Web UI**: HTML/CSS/JS — cross-platform automatically.
- **Menubar**: today Mac-only (Python+rumps). On Linux/Windows, the
  user could open the web URL directly (no tray icon for now). A
  future thin Tauri tray shell ports the menubar to Win/Linux
  without touching the web UI.
- **Keychain**: `keyring` is cross-platform (uses Windows Credential
  Manager on Windows, libsecret on Linux). The shared
  `fulcra-collect:user:bearer-token` entry works the same way on
  each OS.

## Out of scope

- **Sub-project 3** (signing + notarization + homebrew cask for the
  Mac menubar). Still planned but separate.
- **Tauri/Electron tray shell** for non-Mac. Designed-for in this
  spec; not built in this spec.
- **Plugin marketplace / registry / add-third-party-plugin-from-URL**.
  Bundled plugins only for v1; third-party requires editing
  pyproject.toml manually.
- **OAuth with refresh token**. Long-lived tokens only for v1.
- **The "primary popover = recordable annotations" pivot** (Piece 4
  of the post-smoke workstream). Comes after this lands.
- **Real-time updates** (websocket / SSE between web UI and daemon).
  Polling on demand is fine for a config UI; v1 polls `/api/status`
  when the user lands on Preferences and stops polling when they
  leave.
- **Multi-account Fulcra support**. One token per user/install. If
  someone needs two Fulcra accounts they run two daemons (separate
  `FULCRA_COLLECT_HOME` dirs).

## Open questions

- **OAuth device flow vs. paste-token-only for v1.** Device flow is
  better UX but requires the Fulcra side to have a `/device` endpoint
  + polling endpoint. Paste-token works today; device flow can be
  added incrementally without breaking the paste-fallback.
  Recommendation: ship paste-token in v1; add device flow in v1.5
  once the Fulcra side is ready.
- **Browser auto-open on first launch — is it always desirable?**
  Some users may launch the daemon programmatically and not want a
  browser. Recommendation: gate auto-open on the menubar's launch
  detection (the daemon doesn't auto-open from a launchd start; the
  menubar opens it when it detects the no-auth + no-plugins state).
- **Multi-user macOS**: the daemon runs as the user, so
  `~/.config/fulcra-collect/` is per-user. Two users on the same Mac
  get separate daemons, separate keychain entries, separate web URLs.
  No issue.
- **Static files: ship in source or build step?** v1 hand-writes
  HTML/CSS/JS; no build step. If we later add a framework (Svelte,
  Preact) we can add a build step + ship pre-built files. Defer.
- **Where do plugin-installation-time defaults for `Setting.default`
  come from when a plugin first appears?** Plugin declares them on
  the dataclass; the wizard surfaces them as placeholders. They're
  not auto-written to config.toml — only user-confirmed values are.

## Required pre-work

Before the implementation plan can run cleanly, these foundations
need to be in place. Most are pure additions to the daemon — minimal
risk:

1. **Daemon gains an HTTP server module** (`fulcra_collect/web.py`)
   with FastAPI + uvicorn. Started alongside the UDS control server
   in `daemon.serve`. Ephemeral port + URL file + web token + cookie
   path all wired here.
2. **`Setting` dataclass** added to `fulcra_collect/plugin.py`. Plugin
   gains `required_settings: tuple[Setting, ...] = ()`.
3. **`bearer-token` cleanup migration** code path at daemon startup.
4. **Static frontend scaffold**: empty `packages/web-ui/dist/` with
   `index.html`, `app.css`, `app.js` placeholders. The HTTP server
   serves them.
5. **`RunContext.fulcra_token()` confirmed reading from the shared
   location.** If today it pulls from a per-plugin credential, refactor
   to read from `fulcra-collect:user:bearer-token`.

Each of these is a small task. The implementation plan builds on
them.

## Concurrent small refactor: manual-plugin row in the existing menubar

Independent of the web UI work but easy to bundle: refactor the
Python menubar's Plugins-tab and popover-row to handle the kind-aware
semantics (no Enable toggle for manual; Run-now is always visible
for manual). This stays in the existing PyObjC menubar — it's a
small UI fix, not a stack change.

Plus the original Fixes 4–6 from the earlier iteration list:

- **Fix 4**: Label + humanise interval inputs ("Every 60 minutes (≈ 1 hour)").
- **Fix 5**: Caption under "Launch at login" toggle.
- **Fix 6**: Open Activity Logs rendering glitch in About tab.

These three can land alongside the manual-plugin refactor in one
small commit batch before the bigger web-UI work starts.

## Future (not in scope, recorded for later)

- Tauri/Electron tray shell for Win/Linux (gives non-Mac users a
  native menubar entry to the same web UI).
- OAuth device flow on the auth step.
- Real-time updates (SSE/websocket from daemon to web UI).
- The "primary popover = recordable annotations" pivot from the
  onboarding-workstream memory.
- Plugin marketplace / installable-from-URL plugins.
- iOS/iPadOS companion app talking to the daemon over Tailscale.
- Browser-based diagnostics surface (live logs view, definition
  inspector, manual annotation poking).

## Self-review (placeholder for the next pass)

This is a draft. Things to verify on the next review:

- Confirm `RunContext.fulcra_token()` does what the spec assumes
  (read from a shared keychain location) — if it currently pulls
  from a per-plugin credential, the migration is mandatory before
  the bearer-token removal can ship.
- Confirm the Fulcra API has a way to validate a pasted token (so
  the wizard can verify before save) — if not, fail-on-first-real-call
  is acceptable v1.
- Confirm the `keyring` library's macOS Keychain backend supports
  writing entries that can be migrated; the migration code path
  needs to actually work end-to-end.
- The `Setting(kind="path")` UI: file picker per-OS varies. In the
  web UI it's a standard `<input type="file">` — but that gives the
  browser-sandboxed File object, not a filesystem path the daemon
  can open. For paths, the web UI may need to either (a) ask the
  user to type the path manually with a "browse" hint, or (b) post
  the file content directly to the daemon for one-time import.
  Recommendation: (b) for one-off importers — the daemon receives
  the file bytes via POST and processes them; the path stays in the
  user's mind, the daemon never persists a host filesystem path.
  This is more secure (no path-traversal risk) and works the same on
  Mac and Windows.
