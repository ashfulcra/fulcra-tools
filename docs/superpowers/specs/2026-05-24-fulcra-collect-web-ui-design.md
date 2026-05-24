# fulcra-collect web UI + onboarding (revised) — design (sub-project 2.5)

**Date:** 2026-05-24
**Status:** Draft v2. Revised from v1 to address the user's framing on
real-world use, per-plugin setup flows, the popover quick-record
surface, "show it's working" status, agent documentation, and the
trakt-specific milestone.

## What this app actually does (and why)

**Fulcra Collect** is the local agent that watches your services and
your machine and writes the data into your **Fulcra account** —
where you can see it alongside your health metrics, location,
calendar, and everything else Fulcra captures.

It runs on your computer, talks to services you already use (Last.fm,
Trakt, Netflix, Spotify, Day One, your Apple Podcasts library, your
browser activity, Plex/Jellyfin, RSS feeds…), and turns each event
into a Fulcra annotation: "you watched X at Y", "you listened to Z
at W", "you wrote a journal entry at T". It does this on schedules,
on file imports, on incoming webhooks, and on user demand.

It's how your **media life, attention, and personal logs** end up
inside the same data layer as your fitness ring, your phone GPS, and
your calendar — so you can ask Fulcra "what was I doing on Tuesday
afternoon?" and get a real answer.

### Who uses it

- **Fulcra account holders** running it on their personal Mac (and
  eventually Linux/Windows).
- **People with multiple machines** who want one place to record
  activity (the daemon runs on one; chrome extension may run on
  several).
- **People without development background** — this has to be
  setup-by-clicking, not setup-by-editing-config.toml.
- **AI agents** writing custom plugins for services we haven't
  pre-built — they get their own documentation surface (see "Agent
  documentation" below).

### Real-world end-to-end user journey (the experience to design for)

1. Friend recommends Fulcra. User signs up. User installs Fulcra
   Collect (e.g., via homebrew cask once sub-project 3 lands).
2. **First launch**: an onboarding wizard opens in their browser.
   "Welcome to Fulcra Collect. This app writes data from your
   services and your computer into your Fulcra account…" One
   paragraph. Click Next.
3. **Sign in to Fulcra**: paste your Fulcra access token (or device
   flow once it exists). App verifies the token works. "Signed in
   as ash@fulcradynamics.com."
4. **Pick the kinds of things you want to capture**: a checklist
   grouped by category (Music, Video, Books, Journal, Activity,
   Other). User checks "Last.fm", "Trakt", "Netflix", and "Day
   One".
5. **Configure each one** — kind-specific, step-by-step:
   - **Last.fm**: "Sign in at last.fm/api/account/create, give your
     app a name, copy the API key here." User pastes it. App tests
     the key against the Last.fm API. "✓ Working — last 5 scrobbles
     from your account: Song A, Song B…" "Should we write to your
     existing 'Listened' Fulcra annotation [showing 3 recent
     entries] or create a new one?" User picks. "Done — Last.fm
     will sync every hour."
   - **Trakt**: "Trakt records your TV and movie watch history.
     You'll need to create a Trakt OAuth app at
     trakt.tv/oauth/applications. Use these settings…" User does
     it. Pastes Client ID + Secret. Clicks "Sign in to Trakt".
     Trakt OAuth opens in a new tab; user authorizes; redirected
     back. "✓ Signed in as your-trakt-username — last 5 watches:
     Movie A, Show B…" Definition picker → done.
   - **Netflix**: "Netflix doesn't have an API, so we use your
     downloaded viewing history. Open netflix.com/Activity, scroll
     to the bottom, click 'Download all', save the CSV. Drop it
     here." User drops the file. App parses + shows "Found 1,247
     watches from 2018 to today." Definition picker → done.
   - **Day One**: similar pattern; explain how to export the
     library, point to the file.
6. **"You're set."** App shows the home view: status of each
   enabled plugin (Last.fm and Day One showing "Healthy — last
   sync 2 min ago"; Trakt running its first import — progress bar
   showing "Imported 342 of 1,500 watches…"; Netflix done with
   "Imported 1,247 events; last from yesterday").
7. **A week later**, user wants to add Letterboxd. Clicks "Add
   plugin" in the menubar or web UI. Picker shows plugins they
   haven't enabled. Picks Letterboxd. Per-plugin onboarding wizard
   step opens. User pastes their Letterboxd profile URL. Configure
   schedule, definition picker, done.
8. **A month later**, user gets a notification: "Trakt sync failing
   for 3 days — your access token expired." User clicks the
   notification → opens the web UI's Plugins page → Trakt row is
   red with "Re-authenticate" button → user clicks, re-does the
   OAuth dance, fixed.
9. **Quick logging from the menubar**: user finishes lunch. Clicks
   the Fulcra menubar icon. Popover shows a list of *recordable
   annotations* the user has frequently used: "Coffee", "Walk",
   "Reading session". User taps "Reading session" → "Start" →
   menubar icon turns into a small reading-mode indicator → 45
   minutes later user taps "Stop" → annotation recorded
   (Duration, 45m). One tap each side; no configuration needed.

The app needs to:
- Make all of this feel obvious without reading docs.
- Show, at every step, **that it's working** (or clearly that it
  isn't).
- Surface failures with **actionable** error messages and one-click
  re-auth where possible.
- Let agents (AI or otherwise) build new plugins for new services
  without our intervention — they read the docs and follow the
  contract.

## Scope of this spec

Everything to get a non-developer end user from download to
"Fulcra is getting my data from N services" without opening a
terminal. Specifically:

1. **Web UI hybrid** — HTTP server inside the daemon serves a
   browser-based UI for everything configuration-related; the
   existing native menubar stays as the at-a-glance + quick-action
   surface.
2. **Plugin contract additions** — `Setting` dataclass, structured
   `setup_steps` for per-plugin onboarding instructions, shared
   user-level Fulcra token, per-plugin health checks.
3. **First-launch onboarding wizard** with per-plugin setup flows
   tailored to each service (Last.fm API-key paste, Trakt OAuth,
   Netflix takeout upload, Day One export, Apple Podcasts permission
   request, browser-extension install for Attention, etc.).
4. **Status surface** that shows the user it's working: per-plugin
   health pills, recent activity feed, last-import timestamps,
   running-now indicators, total-imported-today counts.
5. **Add-plugin-later flow** — same per-plugin wizard reusable
   for new plugin enablement after first-run.
6. **Failure UX** — actionable errors with re-auth links, retry
   feedback, clear "needs your attention" indicators in both
   the menubar and the web UI.
7. **Definition picker with preview** — show last N entries of an
   existing Fulcra annotation so the user can confirm before
   binding a plugin to it.
8. **Popover quick-record surface** — the menubar's primary
   popover view becomes a quick "log this now" list (Coffee,
   Reading, Walking — whatever the user records often), with the
   plugin status view moved to a secondary card reached via a
   dropdown / "View status" button.
9. **Agent documentation** — `docs/agents/plugin-development.md`
   that an AI assistant (or developer) can read to write a new
   plugin against the contract.
10. **Trakt as the reference end-to-end onboarding flow** — the
    milestone for this workstream. When trakt onboarding works
    cleanly all the way through, the foundation is proven; other
    per-plugin flows follow the same template.

Parallel small refactor (not strictly web UI work, but easy to
batch alongside): Fixes 4–6 from the prior smoke iteration + the
manual-plugin row refactor in the existing menubar (drop Enable,
replace with Run-now).

## Stack decision (recap from v1)

Web UI hybrid chosen over Swift / Tauri / Electron / pure-PyObjC. The
daemon serves localhost; the frontend is HTML/CSS/JS; the existing
menubar stays. Rationale: cross-platform automatic (daemon is Python,
web UI is browser), no Rust dependency, fastest iteration, no sunk
cost thrown away.

Frontend stack: vanilla HTML5 + CSS3 + vanilla JavaScript +
**Alpine.js** for reactivity (10KB, no build step, CDN). Tailwind via
CDN for the brand-on-white palette. We add a build step + framework
(Svelte / Preact) later if complexity demands.

Backend stack: **FastAPI** inside the daemon — async HTTP, automatic
OpenAPI docs, pydantic validation. Adds ~30 transitive deps, all
small and well-maintained.

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│  fulcra-collect daemon (Python, cross-platform)              │
│  ┌──────────────────┐  ┌───────────────────┐  ┌───────────┐ │
│  │ UDS control      │  │ HTTP server       │  │ Scheduler │ │
│  │ socket (existing)│  │ (new, FastAPI)    │  │ Worker    │ │
│  └────────▲─────────┘  └─────────▲─────────┘  └───────────┘ │
│           │                      │                            │
│           │                      ├ serves static + JSON API   │
│           │                      └ stores per-plugin oauth    │
│           │                        callbacks (e.g. trakt)     │
└───────────┼──────────────────────┼─────────────────────────────┘
            │                      │
   ┌────────▼──────────┐    ┌──────▼───────────┐
   │ CLI               │    │ Web UI in browser │
   │ (fulcra-collect)  │    │ — onboarding,     │
   └───────────────────┘    │   preferences,    │
                            │   status, picker  │
                            └──────▲───────────┘
                                   │  open URL on demand
                                   │
                            ┌──────┴────────────┐
                            │ Menubar app       │
                            │ — quick record,   │
                            │   at-a-glance,    │
                            │   gear opens web  │
                            └───────────────────┘
```

The daemon owns two long-lived servers (UDS + HTTP). The web UI is
the primary configuration + status surface; the menubar is the
quick-action surface. They share the daemon as the single source of
truth.

## Plugin contract changes

### 1. Drop per-plugin `bearer-token`; centralise on `RunContext.fulcra_token()`

Existing `attention-relay`, `media-webhook`, and others declare a
`bearer-token` Credential. Wrong: this is the **user's** Fulcra access
token, not a plugin secret.

- Remove the `bearer-token` `Credential` from every plugin that has it.
- The shared user-level token lives in keychain at
  `fulcra-collect:user:bearer-token`.
- `RunContext.fulcra_token()` reads from there.
- One-shot migration at daemon startup: if shared entry is empty AND
  any per-plugin `bearer-token` exists, copy one of them in and
  remove the per-plugin entries.

### 2. New: `Setting` dataclass for non-secret configurables

Parallel to `Credential`:

```python
@dataclass(frozen=True)
class Setting:
    key: str
    label: str
    kind: Literal[
        "text",       # short text input
        "long_text",  # multi-line
        "path",       # file picker (frontend uploads file content; daemon stores hash or content reference)
        "url",        # URL input with validation
        "port",       # numeric port
        "enum",       # dropdown
        "toggle",     # boolean switch
        "interval",   # duration in seconds; rendered as humanized minutes/hours
        "secret",     # password input — but stored in config.toml, NOT keychain
                      # (use Credential for true secrets that should go in keychain)
    ]
    help: str = ""              # short helper shown under the input
    enum_values: tuple[str, ...] | None = None
    default: object = None
    required: bool = True
    placeholder: str = ""       # input placeholder text
```

Storage: `config.toml`'s `plugin_settings.<plugin_id>` table.

### 3. New: structured `setup_steps` for per-plugin onboarding

The plugin tells the UI how to onboard a user. Without this, every
plugin's wizard step would be a hand-written special case in the UI
code. With this, the UI is a generic renderer that walks the steps.

```python
@dataclass(frozen=True)
class SetupStep:
    kind: Literal[
        "intro",                  # markdown text explaining the service
        "external_action",        # tells the user to do something elsewhere (with a link)
        "input",                  # captures one Setting or Credential value
        "oauth",                  # OAuth flow (initiates redirect; awaits callback)
        "file_upload",            # file picker; daemon receives bytes
        "permission_request",     # macOS permission (e.g. Full Disk Access for podcasts)
        "browser_extension",      # tells user to install the Fulcra browser extension
        "test_connection",        # daemon calls plugin's health_check; UI shows result
        "definition_picker",      # picks the Fulcra annotation definition to write to
        "done",                   # final confirmation step
    ]
    title: str
    body_md: str = ""             # markdown body (rendered as HTML in the wizard)
    settings_keys: tuple[str, ...] = ()      # for kind="input", which Setting/Credential keys to render
    external_link: str = ""        # for kind="external_action"
    extension_url: str = ""        # for kind="browser_extension"
```

Each plugin declares its own steps in order. The wizard renders them
one at a time. Trakt's might be:

```python
setup_steps=(
    SetupStep(kind="intro", title="What Trakt does",
              body_md="Trakt tracks your TV and movie watch history. "
                      "Once connected, every time you finish a show or "
                      "movie, it'll be recorded as a Watched annotation "
                      "in your Fulcra account."),
    SetupStep(kind="external_action", title="Create a Trakt OAuth app",
              body_md="Visit https://trakt.tv/oauth/applications and "
                      "click 'New Application'. Use these settings:\n\n"
                      "- Name: `Fulcra Collect`\n"
                      "- Redirect URI: `http://localhost:NNNN/api/oauth/trakt/callback`\n"
                      "  *(this URL will be filled in for you when you're ready)*\n\n"
                      "Click 'Save App' and copy the Client ID and Client Secret to the next step.",
              external_link="https://trakt.tv/oauth/applications"),
    SetupStep(kind="input", title="Paste your Trakt OAuth credentials",
              settings_keys=("client_id", "client_secret")),
    SetupStep(kind="oauth", title="Sign in to Trakt",
              body_md="Click below to authorize Fulcra Collect to read your Trakt history."),
    SetupStep(kind="test_connection", title="Verify connection",
              body_md="Fetching your most recent watches from Trakt…"),
    SetupStep(kind="definition_picker", title="Where should we write your Trakt watches?",
              body_md="We can write to your existing 'Watched' annotation or create a new one."),
    SetupStep(kind="done", title="You're set",
              body_md="Trakt will sync every 6 hours. You can change this in Preferences."),
)
```

The wizard renders these steps; the plugin author doesn't write UI
code. New plugins follow the same template; agents writing new plugins
get a clean target (see Agent documentation below).

### 4. New: `health_check` callback for "is this plugin actually working?"

```python
@dataclass(frozen=True)
class Plugin:
    ...
    health_check: Callable[["RunContext"], "HealthResult"] | None = None
```

```python
@dataclass
class HealthResult:
    ok: bool
    summary: str        # "5 recent scrobbles found", or "401 from API"
    preview: list[dict] = field(default_factory=list)  # optional sample data (last N items)
```

Called during the wizard's `test_connection` step. Called periodically
by the daemon as a passive health probe (shows up in the menubar's
status indicators). For trakt, this is "fetch the last 5 watches and
return them as preview"; for media-webhook it's "is the listener
bound to the port"; for file-based plugins it's "does the configured
path exist and parse cleanly?".

### 5. New: `category` for grouping in the picker

Plugins gain a `category: str` for grouping in the "Pick plugins to
enable" view:

```python
category="music"  # values: "music", "video", "books", "journal", "activity", "other"
```

The picker groups plugins by category for browsability.

## Web UI surfaces

### `/onboarding` — first-launch wizard

Auto-opens (via menubar) when no shared Fulcra token AND no plugins
enabled. Manually reachable from web UI "Settings → Restart
onboarding".

Steps:

1. **Welcome** — what Fulcra Collect is, privacy/data assurance,
   what's next. One paragraph + Next button.
2. **Sign in to Fulcra** — paste-token (v1; device flow v1.5). Test
   the token before storing.
3. **Pick plugins** — checklist grouped by category. Each plugin
   shows its name + description + a small `i` button that pops the
   first SetupStep's intro body as a tooltip.
4. **For each picked plugin, walk its `setup_steps`** — generic
   renderer iterates the SetupStep array. After all steps complete
   for one plugin, advance to the next picked plugin.
5. **Done** — summary: "You enabled N plugins. Last.fm is syncing
   every hour. Trakt will sync every 6 hours. Day One — drop new
   exports here whenever you have them. You're set."

### `/preferences` — top-level navigation

After onboarding, the home of the web UI:

- **Home**: dashboard view — per-plugin status pills, recent
  activity feed, daemon health, "Add plugin" button.
- **Plugins**: per-plugin configuration (settings, credentials,
  intervals, definition binding) + Run-now / Import buttons.
- **Fulcra account**: signed-in state, manage shared token.
- **Notifications**: failure-on, mute-all (matches existing
  menubar Notifications tab).
- **About**: versions, paths, "Open Activity Logs", documentation
  links.

The **Home dashboard** is the "show it's working" surface. It shows:

```
Fulcra Collect — Dashboard

Today: 47 annotations written to Fulcra

Recently
  18:32  Last.fm        Song A — Artist B (Listened)
  18:14  Trakt          Movie C (Watched)
  17:55  Attention      Reading wikipedia.org (Active)
  …

Plugins
  attention-relay    ● Running       last write 2 min ago
  lastfm             ● Healthy       next sync in 23 min · 12 events today
  trakt              ⚠ Sign in expired  [Re-authenticate]
  netflix            – Manual        last import 3 days ago · 1,247 events
  …
```

The "Recently" feed shows the **last ~50 annotations written** —
real receipts that the app is working. Comes from a new daemon
endpoint that exposes the recent activity buffer.

### `/preferences/plugins/{id}` — per-plugin detail

- Plugin metadata (name, description, category)
- Settings + Credentials + Definition binding
- Health check status (with last-run preview)
- Run-now / Import button
- Schedule (for scheduled)
- "Re-run onboarding for this plugin" — runs the SetupStep flow
  again
- "Disable this plugin" / "Remove from list" (where "disable" =
  enabled=False; "remove" doesn't exist for bundled plugins; future
  third-party plugins get an uninstall path)

### `/preferences/add-plugin` — add-plugin-later flow

Same picker as the onboarding step 3, but only shows plugins not
already enabled. Same per-plugin setup_steps walk for whichever
user picks. Returns to dashboard after.

### `/preferences/plugins/{id}/definition` — definition picker (modal)

- Currently bound definition (if any) + last 3 entries
- Other matching-schema definitions, each with last 3 entries as
  inline preview
- "Use this one" per row
- "Create a new definition instead" footer

## Menubar app changes (existing Python+rumps)

Stays mostly as-is; minimal changes:

- **Click status icon → popover** (already working).
- **Popover primary view becomes the quick-record surface**:
  the user's most-frequently-used user-recordable annotations
  (Moment annotations they tap to record, Duration annotations
  they Start/Stop). For now, populated from a config-level list
  (user adds them in Preferences → "Quick record" tab); future
  iterations auto-populate from recent activity.
- **Popover dropdown / "More" button** → reveals the plugin
  status view (current popover content), so power-users can still
  get to it quickly without a browser open.
- **Gear icon** → opens the web UI in the user's default browser
  (Preferences home).
- **Failure indicators**: when a plugin needs attention
  (auth-expired, etc.), the menubar icon gets a small warning
  badge (different from the failure-3x badge) and clicking it
  opens the relevant plugin in the web UI.
- **Manual-plugin rows in popover**: Run-now (or Import…) button
  is always visible — no enable gating.

## Daemon HTTP API (new)

All routes require `Authorization: Bearer <web-token>` (token at
`~/.config/fulcra-collect/web-token`, 0600).

### Static

- `GET /` → `packages/web-ui/dist/index.html`, sets `fulcra_token`
  cookie.
- `GET /static/*` → static asset.

### Plugin operations (parallel to UDS)

- `GET /api/status` → snapshot.
- `POST /api/plugin/{id}/run` → trigger run.
- `POST /api/reload` → reload config.
- `GET /api/version` → daemon + plugin versions.
- `GET /api/plugin/{id}/credentials` → per-credential set/missing.
- `PUT /api/plugin/{id}/credential/{key}` → store a credential.
- `DELETE /api/plugin/{id}/credential/{key}` → remove a credential.
- `GET /api/plugin/{id}/settings` → read non-secret settings.
- `PUT /api/plugin/{id}/settings` → write settings; reload.
- `POST /api/plugin/{id}/enable` → enable; reload.
- `POST /api/plugin/{id}/disable` → disable; reload.

### Plugin contract reads

- `GET /api/plugin/{id}/contract` → returns the plugin's
  `required_settings`, `required_credentials`, `required_permissions`,
  `setup_steps`, `category`, `description`, `health_check_available`,
  `kind`, `default_interval`. This is what powers the wizard's
  step renderer.

### Health + activity

- `POST /api/plugin/{id}/health_check` → invoke the plugin's
  `health_check` callback; return `HealthResult`. May be slow (e.g.
  remote API call); UI shows a spinner.
- `GET /api/activity?limit=50` → returns the last N annotations
  written to Fulcra by ANY plugin (daemon maintains a small ring
  buffer of recent writes).
- `GET /api/plugin/{id}/preview?limit=N` → last N entries from the
  plugin's bound Fulcra definition (for the dashboard's per-plugin
  detail view).

### Fulcra account

- `GET /api/fulcra/auth/status` → `{authenticated, account?,
  expires_at?}`.
- `POST /api/fulcra/auth/token` → body `{token}`; validate +
  store + return account info.
- `DELETE /api/fulcra/auth/token` → forget.

### OAuth callbacks

- `GET /api/oauth/{plugin_id}/callback?code=...&state=...` →
  generic OAuth callback handler. Plugin author registers the
  expected callback via `Plugin.oauth_handler` (a callable that
  takes the code + state and returns a token / refresh-token /
  account info). Daemon stores the result in keychain under the
  plugin's namespace.

### Definitions

- `GET /api/definitions?annotation_type=...` → list matching defs.
- `GET /api/definitions/{id}/recent?limit=N` → last N entries.
- `POST /api/plugin/{id}/definition` → body `{definition_id?,
  force_new?}`; bind the plugin to a chosen def or force-new.
- `DELETE /api/plugin/{id}/definition` → clear cache; next run
  re-resolves.

### Quick-record (for the menubar popover)

- `GET /api/quick-record/definitions` → user-selected list of
  Moment / Duration definitions surfaced in the popover.
- `POST /api/annotations` → write an arbitrary annotation directly
  (used for one-tap recording from the menubar).

## Trakt as the reference flow (the milestone)

When trakt's end-to-end onboarding works, the system is proven.
Concretely, the trakt plugin gets:

1. **Plugin metadata**: `category="video"`, `description`
   ("Records your TV and movie watch history."),
   `canonical_definition_name="Watched"`, `default_interval=6h`,
   `kind="scheduled"`.
2. **`required_settings`**: `client_id`, `client_secret` (both
   stored in keychain via Credential, not Setting — they're
   secrets).
3. **`required_credentials`**: oauth `access_token`, `refresh_token`
   (filled by the daemon's OAuth callback handler after the user
   completes the flow).
4. **`oauth_handler`**: takes the auth code from the callback,
   exchanges it for tokens via Trakt's token endpoint, stores the
   tokens. Implements PKCE for security.
5. **`health_check`**: hits Trakt's `/users/me` endpoint to confirm
   tokens work; also fetches last 5 watches and returns them as
   `HealthResult.preview`.
6. **`setup_steps`**:
   - intro: "What Trakt does"
   - external_action: "Create a Trakt OAuth app" (link to
     trakt.tv/oauth/applications, instructions for the form)
   - input: Client ID + Client Secret
   - oauth: "Sign in to Trakt" — daemon initiates the OAuth flow
     with PKCE, opens trakt.tv in a new tab, polls for the
     callback completion
   - test_connection: invokes health_check, shows last 5 watches
   - definition_picker: pick existing "Watched" or create new
   - done: "Trakt will sync every 6 hours"
7. **Daemon-side OAuth machinery**: a new module
   `fulcra_collect/oauth.py` that handles PKCE state, code
   exchange, refresh-token rotation. Reusable for future
   OAuth-using plugins (Spotify, future Last.fm OAuth, etc.).

End-to-end test of the milestone:
- Fresh daemon, fresh keychain, fresh config.toml.
- Sign in to Fulcra (paste token).
- Pick Trakt in the picker.
- Walk through trakt setup_steps.
- Health check shows recent watches.
- Definition picker offers existing "Watched" (if any) with
  preview, or "Create new".
- After Done, trakt runs once, writes recent watches to Fulcra.
- Dashboard shows trakt as Healthy + recent entries in activity
  feed.

## Per-plugin setup-step authoring guide (high level)

For each plugin in the current 16-plugin set:

| Plugin | Step shape |
|---|---|
| **attention-relay** | intro → permission_request (browser extension install + Fulcra Auth) → browser_extension → test_connection → definition_picker → done |
| **media-webhook** | intro → input (port, auth_token) → external_action (configure Plex/Jellyfin with this URL) → test_connection (verify listening) → definition_picker → done |
| **lastfm** | intro → external_action (last.fm api signup) → input (api_key) → test_connection → definition_picker → done |
| **trakt** | (above) |
| **letterboxd** | intro → external_action (find your profile URL) → input (profile RSS URL) → test_connection → definition_picker → done |
| **goodreads** | similar to letterboxd |
| **netflix** | intro → external_action (netflix.com/Activity download) → file_upload → test_connection → definition_picker → done |
| **deezer** | intro → external_action (deezer api signup) → input (access_token) → test_connection → definition_picker → done |
| **spotify-extended** | intro → external_action (request gdpr export from Spotify) → file_upload → test_connection → definition_picker → done |
| **apple-podcasts** | intro → permission_request (Full Disk Access for ~/Library/Containers/com.apple.podcasts/) → test_connection → definition_picker → done |
| **apple-podcasts-timemachine** | intro → permission_request → external_action (point at a Time Machine snapshot dir) → file_upload (or path input) → test_connection → definition_picker → done |
| **apple-takeout** | intro → external_action (request from privacy.apple.com) → file_upload → test_connection → definition_picker → done |
| **youtube** | intro → external_action (Google Takeout) → file_upload → test_connection → definition_picker → done |
| **generic-rss** | intro → input (feed_url, category) → test_connection → definition_picker → done |
| **generic-csv** | intro → input (category) → file_upload → test_connection → definition_picker → done |
| **dayone** | intro → external_action (export from Day One app) → file_upload → test_connection → definition_picker → done |

Each plugin's `setup_steps` is a small declarative payload. The
wizard renders it; no plugin-specific UI code needed.

## "Show it's working" — status surface design

The single biggest user-trust issue: did anything actually get
written to Fulcra? The Dashboard's recent-activity feed answers
this concretely:

- Daemon maintains an **in-memory ring buffer of the last ~200
  annotations** it wrote (or attempted to write) to Fulcra.
- Each entry: timestamp + plugin id + annotation summary (e.g.
  "Watched: Better Call Saul S6E13").
- `GET /api/activity?limit=50` exposes it.
- Dashboard polls every 5s while focused; renders a live feed.
- Menubar popover's secondary "View status" card shows a compact
  3-item version of the same feed.

Per-plugin pills on the dashboard:

- `Healthy` (green) — last run succeeded, no consecutive failures.
- `Running` (purple, animated) — currently mid-run.
- `Scheduled` (grey) — enabled, next run in X.
- `Manual` (mint) — enabled, awaiting Run-now.
- `Auth needed` (amber) — credentials missing or expired; clickable
  → "Re-authenticate".
- `Failing` (red) — consecutive_failures ≥ 3; clickable → plugin
  detail page showing recent errors.
- `Disabled` (grey, italic) — toggle is off.

Each pill is clickable; takes the user to `/preferences/plugins/{id}`.

## Failure surface design

When something breaks, the user should know AND know what to do:

1. **Notification** (existing 3-consecutive-failure trigger fires) —
   "Trakt sync failing: 401 unauthorized — re-authenticate"
2. **Menubar badge** — the existing red-dot badge on the icon
   covers this.
3. **Plugin pill** in dashboard turns red or amber per above.
4. **Plugin detail page** — shows the last error in full, with
   action buttons:
   - "Re-authenticate" for auth-related errors (re-runs the OAuth
     step of setup_steps).
   - "Reset definition" for definition-mismatch errors (clears
     cache; re-resolves on next run).
   - "Open Activity Logs" for everything else.
5. **Quiet recovery** — when a plugin transitions from failing
   back to healthy, the menubar badge goes away and the
   dashboard pill turns green; no notification (no need to
   interrupt the user for good news).

## Agent documentation

Living at `docs/agents/plugin-development.md`. Written for AI
assistants (Claude, GPT, Codex, etc.) but also useful for human
developers writing custom plugins. Contents:

1. **What a Fulcra Collect plugin is** — one paragraph, in plain
   terms.
2. **The Plugin dataclass** — every field documented with examples.
3. **The Setting / Credential / Permission / SetupStep dataclasses** —
   same.
4. **The RunContext API** — `fulcra_token()`, `resolved_definition_id()`,
   `progress()`, `config`, `credentials`, `state`, `log`.
5. **Entry-point registration** — how to register a plugin in
   `pyproject.toml`'s `fulcra_collect.plugins` group.
6. **Three example plugins, fully worked through**:
   - A simple scheduled plugin (modeled on lastfm).
   - An OAuth-using scheduled plugin (modeled on trakt).
   - A manual file-import plugin (modeled on netflix).
7. **Testing patterns** — how to write tests for a plugin without
   hitting real APIs.
8. **The agent contract**: an AI assistant writing a new plugin
   declares it via Plugin, writes its setup_steps, writes its
   health_check, registers the entry point, writes tests, runs them.
   At that point the daemon auto-discovers it and the web UI's
   "Add plugin" picker surfaces it.
9. **Glossary** of Fulcra annotation concepts (moment / duration /
   measurement_spec / definition).

The agent docs live in the repo, are version-controlled, and the
web UI's About tab links to the rendered version.

## What the existing native menubar keeps

Even after the web UI lands, the native menubar is the
ergonomically-correct surface for:

1. **At-a-glance status** — icon state (idle / running pulse /
   failure badge / auth needed badge).
2. **Quick record** — one-tap Moment / Duration recording for the
   user's frequent annotations.
3. **Run-now** — quick trigger of manual plugins.
4. **Open the web UI** — gear icon.

The web UI is for configuration and deep status; the menubar is for
acting on what's happening right now.

## Cross-platform notes

- **Daemon**: Python — works on macOS, Linux, Windows. The HTTP
  server module uses FastAPI (cross-platform). UDS is POSIX-only but
  Windows can fall back to a TCP loopback socket if needed (out of
  scope for v1 — Windows menubar work is future).
- **Web UI**: HTML/CSS/JS — works in any browser on any OS.
- **Menubar**: Mac-only (current Python+rumps). Future thin
  Tauri/Electron tray shell for Linux/Windows.
- **Keychain**: `keyring` is cross-platform — Windows Credential
  Manager, libsecret on Linux, macOS Keychain on Mac.

## Out of scope (recorded for future)

- **Sub-project 3**: code-signing, notarization, homebrew cask,
  installer. Separate workstream.
- **OAuth device flow** for Fulcra sign-in. v1 uses paste-token;
  device flow lands in v1.5 once Fulcra exposes a device-flow
  endpoint.
- **Real-time updates** (websocket / SSE). Polling on demand is
  sufficient for v1; SSE if it becomes a bottleneck.
- **Multi-account Fulcra support**. One Fulcra token per install.
  Power users run two installs.
- **Plugin marketplace / registry**. Third-party plugins still
  install via `uv pip install <package>` in v1; a discoverable
  marketplace is its own workstream.
- **Native Linux tray / Windows tray app**. Web UI handles config;
  a thin tray shell can come later.

## Open questions

- **Where does the activity ring buffer live?** In-process in the
  daemon (lost on restart) vs persisted to a small sqlite table.
  Recommendation: in-memory v1, sqlite v1.5 if users want history
  beyond the last daemon-uptime.
- **OAuth state storage**: daemon-side dict keyed by `state`
  parameter, with TTL. Survives daemon restart? v1: no (in-memory);
  v1.5 if needed.
- **Path inputs vs file uploads**: for one-off importers (netflix,
  apple-takeout, etc.), file_upload (browser sends bytes, daemon
  parses, no path stored) is more secure and works on all OSes. For
  recurring path-based plugins (apple-podcasts: reads a local
  SQLite continuously), we DO need a path — the user grants Full
  Disk Access and the daemon reads from there. Spec assumes:
  one-off importers use file_upload; recurring plugins use path
  input (and macOS permission grant via Settings → Privacy & Security).
- **Browser-extension distribution**: chrome extension for
  attention-relay — where is it hosted? In the repo at
  `packages/chrome-extension/` (per existing structure). The
  setup_steps `browser_extension` kind shows install instructions
  + a link to the chrome web store (or `chrome://extensions` for
  side-loading during dev).
- **The HTTP port survives daemon restart**: ephemeral port means
  it changes each restart. The web URL file updates each time. Is
  this surprising for users who bookmark the URL? Recommendation:
  pin to a fixed port (e.g. 7321) when available, fall back to
  ephemeral on conflict; document.

## Implementation phases (preview — actual plan lives in a separate doc)

The implementation plan that follows this spec will land in
phases. Approximate sketch:

- **Phase A**: parallel cleanup of the existing Python menubar
  (Fixes 4–6 + manual-plugin row refactor).
- **Phase B**: daemon foundation — HTTP server, Setting dataclass,
  SetupStep + setup_steps, health_check, category. Shared Fulcra
  token + migration. Static frontend scaffold.
- **Phase C**: web UI core — onboarding wizard renderer, plugin
  picker, sign-in step, generic per-plugin setup_steps walker.
- **Phase D**: status surface — recent activity ring buffer, API,
  dashboard page.
- **Phase E**: definition picker — list/preview endpoints + modal
  UI integrated into per-plugin config.
- **Phase F**: OAuth machinery + Trakt — daemon `oauth.py`
  module with PKCE, callback handler; trakt plugin's `setup_steps`
  + `oauth_handler` + `health_check`.
- **Phase G**: popover quick-record — menubar popover refactor,
  Quick-record tab in Preferences for choosing which annotations
  to surface, `/api/annotations` endpoint.
- **Phase H**: agent documentation — `docs/agents/plugin-development.md`
  with full plugin contract reference and three worked examples.
- **Phase I**: per-plugin setup_steps for the remaining 13 plugins
  (lastfm, letterboxd, goodreads, netflix, deezer, spotify-extended,
  apple-podcasts, apple-podcasts-timemachine, apple-takeout,
  youtube, generic-rss, generic-csv, dayone, attention-relay,
  media-webhook). Many are mechanical; agent docs make this easy
  for future contributors.
- **Phase J**: verification, security scan, pre-push sweep, push.

The trakt milestone covers Phases A through F and a slice of I (just
trakt itself). Phases G, H, and the rest of I follow.
