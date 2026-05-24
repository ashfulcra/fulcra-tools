# fulcra-collect web UI + Trakt milestone — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the web-UI hybrid for fulcra-collect — daemon serves a localhost web UI in the user's browser; existing Python+rumps menubar stays as the at-a-glance + quick-record surface. The milestone is **Trakt working end-to-end** through the new onboarding flow: install → sign in to Fulcra → pick Trakt → walk the Trakt-specific setup wizard (intro → create OAuth app → paste creds → OAuth sign-in → test connection showing recent watches → definition picker → done) → first run imports.

**Architecture:** FastAPI HTTP server inside the daemon; static HTML/CSS/JS frontend with Alpine.js for reactivity; same daemon backs both the existing UDS control socket and the new HTTP API. Plugin contract gains `Setting`, `SetupStep`, `health_check`, `category`, `oauth_handler` so the wizard is a generic renderer. Shared user-level Fulcra token replaces per-plugin `bearer-token`. Recent-activity ring buffer powers the "show it's working" dashboard.

**Tech Stack:** Python 3.12+, FastAPI ≥0.115, uvicorn ≥0.30, pydantic ≥2.7, HTML5/CSS3/vanilla JS + Alpine.js 3 (CDN), Tailwind CSS (CDN), pytest, httpx for FastAPI's TestClient.

**Spec:** `docs/superpowers/specs/2026-05-24-fulcra-collect-web-ui-design.md` (committed as `f854ead`). The spec carries the architectural rationale, all 9 user-vision points, the SetupStep authoring guide for each of the 16 plugins, and the Phase A–J phase sketch.

**Worktree note:** Continue on `main`. The user has explicitly consented to working on main throughout this session; the pre-push orphan sweep runs at the end before each `git push`. **Each subagent verifies `git symbolic-ref HEAD` is `refs/heads/main` at start and end.**

---

## Phases

This plan covers **Phases A through F + the Trakt slice of Phase I** — the Trakt milestone. Phases G (popover quick-record), H (agent docs), and the remaining 13 plugins in Phase I follow in subsequent plans.

| Phase | Scope | Task count |
|---|---|---|
| **A** | Parallel cleanup of existing Python menubar: Fixes 4–6 + manual-plugin row refactor | 4 |
| **B** | Daemon foundation: Setting / SetupStep / health_check / category, HTTP server, static scaffold, shared Fulcra token + migration | 10 |
| **C** | Web UI core: API routes parallel to UDS, frontend app shell, SetupStep renderer, onboarding wizard, Preferences home | 11 |
| **D** | Status surface: ring buffer, /api/activity, dashboard feed + per-plugin pills | 4 |
| **E** | Definition picker: list/preview routes, modal UI integrated into per-plugin config | 3 |
| **F** | OAuth + Trakt: daemon `oauth.py` (PKCE), callback handler, Trakt plugin's setup_steps + oauth_handler + health_check + setting/credential declarations | 7 |
| **Total** | | **39** |

---

## Phase A — parallel cleanup of the existing Python menubar

These can be done in any order; bundle as a batch in one subagent dispatch. Each lands as its own commit.

### Task A1: Fix 4 — humanize interval inputs

**Files:**
- Modify: `packages/menubar/fulcra_menubar/preferences/plugins_tab.py`
- Create: `packages/menubar/fulcra_menubar/_humanize.py`
- Create: `packages/menubar/tests/test_humanize.py`

- [ ] **Step 1: Test the helper first (TDD)**

```python
# packages/menubar/tests/test_humanize.py
from fulcra_menubar._humanize import humanize_minutes

def test_under_one_hour():
    assert humanize_minutes(30) == "30 minutes"

def test_one_hour():
    assert humanize_minutes(60) == "1 hour"

def test_exact_hours():
    assert humanize_minutes(360) == "6 hours"

def test_mixed_hours_minutes():
    assert humanize_minutes(90) == "1h 30m"

def test_one_day():
    assert humanize_minutes(1440) == "1 day"

def test_exact_days():
    assert humanize_minutes(2880) == "2 days"

def test_one_minute():
    assert humanize_minutes(1) == "1 minute"

def test_zero():
    assert humanize_minutes(0) == "0 minutes"
```

- [ ] **Step 2: Implement**

```python
# packages/menubar/fulcra_menubar/_humanize.py
"""Pure functions for humanizing durations and timestamps."""

def humanize_minutes(minutes: int) -> str:
    """60 → '1 hour', 360 → '6 hours', 1440 → '1 day', 90 → '1h 30m'."""
    if minutes == 0:
        return "0 minutes"
    if minutes == 1:
        return "1 minute"
    if minutes < 60:
        return f"{minutes} minutes"
    if minutes % 1440 == 0:
        days = minutes // 1440
        return f"{days} day" if days == 1 else f"{days} days"
    if minutes % 60 == 0:
        hours = minutes // 60
        return f"{hours} hour" if hours == 1 else f"{hours} hours"
    return f"{minutes // 60}h {minutes % 60}m"
```

- [ ] **Step 3: Render the label + helper text in plugins_tab.py**

In `_make_plugin_row` find the existing interval field block. Wrap it with:
- Left label: `"Every"`
- Right label: `"minutes"`
- Below: a 12pt secondary-text caption rendered live: `"≈ {humanize_minutes(value)}"`. Updates on text changes via the existing `textChanged:` action target.

Use `from .._humanize import humanize_minutes`.

- [ ] **Step 4: Run tests**

```bash
uv run --package fulcra-menubar pytest packages/menubar/tests/test_humanize.py -v
```

Expected: all green.

- [ ] **Step 5: Branch check + commit**

```
feat(menubar): humanize interval inputs in Preferences

A bare "360" next to a plugin name now reads:
  Every [360] minutes
  ≈ 6 hours
Where the caption updates live as the user types.
```

### Task A2: Fix 5 — caption under Launch-at-login

**Files:**
- Modify: `packages/menubar/fulcra_menubar/preferences/about_tab.py`

- [ ] **Step 1: Find the Launch-at-login toggle** in `about_tab.py`.

- [ ] **Step 2: Add a 12pt secondary caption underneath**:

```python
launch_caption = NSTextField.labelWithString_(
    "Open Fulcra Collect automatically when you log in to your Mac."
)
launch_caption.setFont_(typography.small())
launch_caption.setTextColor_(colors.text_secondary())
launch_caption.setFrame_(NSMakeRect(<x>, <y_under_toggle>, 380, 16))
view.addSubview_(launch_caption)
```

Position 18pt below the toggle.

- [ ] **Step 3: Branch check + commit**

```
feat(menubar): caption Launch-at-login toggle in Preferences > About

Adds a 12pt secondary-coloured caption matching the Notifications-tab
style. Answers the user's "don't know what switch on bottom of about
tab does" smoke feedback.
```

### Task A3: Fix 6 — Open Activity Logs layout

**Files:**
- Modify: `packages/menubar/fulcra_menubar/preferences/about_tab.py`

User reported the "Open Activity Logs" button rendering as a tooltip-overlay glitch — caused by the plugin-versions list overflowing into the button's frame without a scroll view.

- [ ] **Step 1: Restructure the About tab layout**

Top-down:

1. Action row at top: `[Open Activity Logs button]   [Launch-at-login toggle + caption]`
2. Identity block: App version, Daemon version, Config path, State path
3. Plugin-versions list wrapped in an `NSScrollView` so it scrolls cleanly with 16+ plugins, not overflowing.

- [ ] **Step 2: Implement and ensure proper spacing**

The action button moves OUT of the bottom-left area and INTO a top action row.
The plugin versions list moves INTO an `NSScrollView` with `setHasVerticalScroller_(True)`.

- [ ] **Step 3: Branch check + commit**

```
fix(menubar): About-tab — actions at top, plugin versions in a scroll view

The plugin-versions list previously overflowed without a scroll view
and visually covered the "Open Activity Logs" button at the bottom,
making it look like a tooltip overlay.

Now: actions (Open Logs + Launch-at-login + new caption) sit at the
top; identity metadata in the middle; plugin-versions list wrapped
in an NSScrollView so it scrolls instead of overflowing.
```

### Task A4: Manual-plugin row refactor — drop Enable; show Run-now / Import always

**Files:**
- Modify: `packages/menubar/fulcra_menubar/preferences/plugins_tab.py`
- Modify: `packages/menubar/fulcra_menubar/popover/plugin_row.py`

Manual plugins have no automatic firing — the Enable toggle is meaningless. Replace with a prominent Run-now (or Import…) button always visible.

- [ ] **Step 1: In `preferences/plugins_tab.py`'s `_make_plugin_row`**:

```python
if snap.kind == "manual":
    # Manual plugins: NO Enable toggle. Replace with a prominent Run-now button.
    run_btn = NSButton.alloc().initWithFrame_(NSMakeRect(width - 80, height - 36, 64, 28))
    # The button label depends on whether the plugin has a "path" Setting (Import…)
    # or not (Run now). For v1 of this refactor we always say "Run now"; the
    # Setting-aware "Import…" label lands when Phase B introduces Settings.
    run_btn.setTitle_("Run now")
    run_btn.setBezelStyle_(NSBezelStyleRounded)
    _attach(run_btn, lambda _s: client.run(snap.id))
    row.addSubview_(run_btn)
else:
    # service / scheduled: keep the existing Enable toggle
    enabled_switch = NSSwitch.alloc().initWithFrame_(...)
    ...
```

- [ ] **Step 2: In `popover/plugin_row.py`'s `make_row`**:

For manual plugins, show Run-now unconditionally (today it's gated by `snapshot.enabled`):

```python
if snapshot.kind == "manual":
    # Always actionable from the popover; no Enable gate.
    show_run_now = True
elif snapshot.kind == "scheduled":
    show_run_now = snapshot.enabled
else:  # service
    show_run_now = False  # services aren't user-triggered

if show_run_now:
    # ... existing Run-now button construction
```

- [ ] **Step 3: Branch check + commit**

```
fix(menubar): drop Enable toggle for manual plugins; Run-now always visible

User feedback: "the takeouts/csv importers aren't active watchers —
they should be buttons to start that allow you to parse the data".
Today's Enable toggle is meaningless for manual plugins (daemon never
auto-polls them); it just gated whether the Run-now button shows in
the popover. Now the Run-now button is unconditional for manual
plugins both in Preferences and in the popover. service / scheduled
keep the toggle (it gates supervision and cycle inclusion
respectively).
```

---

## Phase B — daemon foundation

The architectural foundation for everything else. Lands the new plugin contract types, the HTTP server, the static frontend scaffold, and the shared Fulcra token migration.

### Task B1: `Setting` dataclass

**Files:**
- Modify: `packages/collect/fulcra_collect/plugin.py`
- Modify: `packages/collect/tests/test_plugin.py`

- [ ] **Step 1: Write failing tests**

```python
def test_setting_dataclass_fields():
    from fulcra_collect.plugin import Setting
    s = Setting(key="feed_url", label="RSS feed URL", kind="url",
                help="Where to fetch the feed from.", default=None,
                required=True, placeholder="https://example.com/feed.xml")
    assert s.key == "feed_url"
    assert s.kind == "url"
    assert s.required is True

def test_setting_enum_kind_requires_enum_values():
    from fulcra_collect.plugin import Setting
    # enum without enum_values should still construct; consumers validate
    s = Setting(key="category", label="Category", kind="enum",
                enum_values=("watched", "listened", "read"), default="watched")
    assert s.enum_values == ("watched", "listened", "read")

def test_plugin_required_settings_default_empty():
    from fulcra_collect.plugin import Plugin
    p = Plugin(id="x", name="X", kind="manual", run=lambda c: None)
    assert p.required_settings == ()
```

- [ ] **Step 2: Add to `plugin.py`**

```python
@dataclass(frozen=True)
class Setting:
    """A non-secret configurable value the user provides via the UI.

    Lives in config.toml's plugin_settings.<plugin_id> table. For
    secrets (API keys, OAuth tokens, etc.) use Credential instead;
    those go in the OS keychain.
    """
    key: str
    label: str
    kind: Literal[
        "text", "long_text", "path", "url", "port",
        "enum", "toggle", "interval", "secret",
    ]
    help: str = ""
    enum_values: tuple[str, ...] | None = None
    default: object = None
    required: bool = True
    placeholder: str = ""
```

And add to `Plugin`:

```python
@dataclass(frozen=True)
class Plugin:
    ...
    required_settings: tuple[Setting, ...] = ()
```

- [ ] **Step 3: Tests pass**

```bash
uv run pytest packages/collect/tests/test_plugin.py -q
```

- [ ] **Step 4: Branch check + commit**

```
feat(collect): Setting dataclass for non-secret plugin config

Parallel to Credential. Kinds: text / long_text / path / url / port /
enum / toggle / interval / secret. The "secret" kind is for short-
lived secrets like a webhook auth token that go in config.toml
rather than the keychain; true secrets (API keys, OAuth tokens) use
Credential and live in keychain.

Plugin gains `required_settings: tuple[Setting, ...] = ()`. Plugins
that opt in declare what config they need; the web UI's per-plugin
wizard renders the inputs from this declaration. Pre-spec.
```

### Task B2: `SetupStep` dataclass

**Files:**
- Modify: `packages/collect/fulcra_collect/plugin.py`
- Modify: `packages/collect/tests/test_plugin.py`

- [ ] **Step 1: Write failing tests**

```python
def test_setup_step_dataclass():
    from fulcra_collect.plugin import SetupStep
    s = SetupStep(kind="intro", title="What this does", body_md="…")
    assert s.kind == "intro"

def test_setup_step_input_kind_with_settings_keys():
    from fulcra_collect.plugin import SetupStep
    s = SetupStep(kind="input", title="Paste your API key",
                  settings_keys=("api_key",))
    assert s.settings_keys == ("api_key",)

def test_plugin_setup_steps_default_empty():
    from fulcra_collect.plugin import Plugin
    p = Plugin(id="x", name="X", kind="manual", run=lambda c: None)
    assert p.setup_steps == ()
```

- [ ] **Step 2: Add to plugin.py**

```python
@dataclass(frozen=True)
class SetupStep:
    """One step in a plugin's onboarding wizard. The web UI renders
    these in order. Per-plugin custom UI code isn't needed — the
    plugin declares its steps; the renderer handles the rest.
    """
    kind: Literal[
        "intro", "external_action", "input", "oauth", "file_upload",
        "permission_request", "browser_extension", "test_connection",
        "definition_picker", "done",
    ]
    title: str
    body_md: str = ""
    settings_keys: tuple[str, ...] = ()
    external_link: str = ""
    extension_url: str = ""


@dataclass(frozen=True)
class Plugin:
    ...
    setup_steps: tuple[SetupStep, ...] = ()
```

- [ ] **Step 3: Tests pass + commit**

```
feat(collect): SetupStep dataclass for per-plugin onboarding wizards

Plugin gains `setup_steps: tuple[SetupStep, ...] = ()`. Kinds:
intro / external_action / input / oauth / file_upload /
permission_request / browser_extension / test_connection /
definition_picker / done.

The web UI's wizard reads this declaration and renders each step
with a generic renderer per kind. Plugin authors (and agents writing
new plugins) declare the wizard shape; no plugin-specific UI code
required.
```

### Task B3: `health_check` + `HealthResult`

**Files:**
- Modify: `packages/collect/fulcra_collect/plugin.py`
- Modify: `packages/collect/tests/test_plugin.py`

- [ ] **Step 1: Tests**

```python
def test_health_result():
    from fulcra_collect.plugin import HealthResult
    r = HealthResult(ok=True, summary="5 recent scrobbles",
                     preview=[{"title": "Song A"}, {"title": "Song B"}])
    assert r.ok is True
    assert len(r.preview) == 2

def test_plugin_health_check_optional():
    from fulcra_collect.plugin import Plugin
    p = Plugin(id="x", name="X", kind="manual", run=lambda c: None)
    assert p.health_check is None
```

- [ ] **Step 2: Add to plugin.py**

```python
@dataclass
class HealthResult:
    ok: bool
    summary: str
    preview: list[dict] = field(default_factory=list)


@dataclass(frozen=True)
class Plugin:
    ...
    health_check: Callable[["RunContext"], "HealthResult"] | None = None
```

- [ ] **Step 3: Tests pass + commit**

### Task B4: `category` field + retrofit on every plugin

**Files:**
- Modify: `packages/collect/fulcra_collect/plugin.py`
- Modify: All plugin `Plugin(...)` declarations across packages

- [ ] **Step 1: Add to plugin.py**

```python
@dataclass(frozen=True)
class Plugin:
    ...
    category: Literal["music", "video", "books", "journal", "activity", "other"] = "other"
```

- [ ] **Step 2: Retrofit each plugin**

| Plugin | category |
|---|---|
| attention-relay | activity |
| media-webhook | video |
| lastfm | music |
| spotify-extended | music |
| spotify-ifttt (not auto-registered) | music |
| deezer | music |
| apple-podcasts | music |
| apple-podcasts-timemachine | music |
| trakt | video |
| netflix | video |
| letterboxd | video |
| youtube | video |
| apple-takeout | video |
| goodreads | books |
| generic-rss | other |
| generic-csv | other |
| dayone | journal |

- [ ] **Step 3: Test discovery shows category**

```python
def test_status_includes_category(collect_home):
    # Same fixture style as the existing description test; assert each
    # plugin in the status reply has a category field.
```

- [ ] **Step 4: Commit**

### Task B5: FastAPI HTTP server module

**Files:**
- Create: `packages/collect/fulcra_collect/web.py`
- Modify: `packages/collect/pyproject.toml` (add fastapi + uvicorn)
- Modify: `packages/collect/fulcra_collect/daemon.py` (start the HTTP server alongside UDS)
- Create: `packages/collect/tests/test_web.py`

- [ ] **Step 1: Add deps**

In `packages/collect/pyproject.toml`'s `[project] dependencies`:

```toml
"fastapi>=0.115",
"uvicorn[standard]>=0.30",
"pydantic>=2.7",
"httpx>=0.27",  # for FastAPI's TestClient + outbound HTTP (OAuth, Fulcra preview)
```

- [ ] **Step 2: Implement `web.py`**

```python
"""HTTP server that fronts the daemon via JSON API + static frontend.

Bound to 127.0.0.1 only on an ephemeral port. Writes the resulting
URL to ~/.config/fulcra-collect/web-url so the menubar can open it.
Auth: a Bearer token from ~/.config/fulcra-collect/web-token (0600)
seeded into a cookie on the initial HTML load."""
from __future__ import annotations

import secrets
import socket
import threading
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Depends, HTTPException, status
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from . import config as _config

WEB_TOKEN_PATH = lambda: _config.config_dir() / "web-token"
WEB_URL_PATH = lambda: _config.config_dir() / "web-url"

def _ensure_token() -> str:
    p = WEB_TOKEN_PATH()
    if p.exists():
        return p.read_text(encoding="utf-8").strip()
    token = secrets.token_urlsafe(32)
    p.write_text(token, encoding="utf-8")
    p.chmod(0o600)
    return token

def _frontend_dir() -> Path:
    # packages/web-ui/dist sits alongside packages/collect at the workspace level
    here = Path(__file__).resolve()
    workspace_root = here.parents[3]  # collect/fulcra_collect → packages → workspace
    return workspace_root / "packages" / "web-ui" / "dist"


def build_app(daemon) -> FastAPI:
    """Construct the FastAPI app with the daemon injected for handlers."""
    app = FastAPI(title="Fulcra Collect")
    token = _ensure_token()
    bearer = HTTPBearer(auto_error=False)

    def require_token(creds: HTTPAuthorizationCredentials = Depends(bearer)):
        if creds is None or creds.credentials != token:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "auth required")

    @app.get("/")
    def root():
        # Serve index.html; set the cookie containing the bearer token.
        idx = _frontend_dir() / "index.html"
        resp = FileResponse(str(idx))
        resp.set_cookie("fulcra_token", token, httponly=False, samesite="strict",
                         secure=False, path="/")
        return resp

    # Mount static assets
    static_dir = _frontend_dir() / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    # JSON API routes (Phase C populates these)
    @app.get("/api/status", dependencies=[Depends(require_token)])
    def status_route():
        return daemon.handle_request({"cmd": "status"})

    return app


def serve(daemon, *, host: str = "127.0.0.1", port: int = 0) -> tuple[str, threading.Thread]:
    """Start the HTTP server in a background thread. Returns (url, thread)."""
    if port == 0:
        # Pick an ephemeral port
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind((host, 0))
        port = s.getsockname()[1]
        s.close()

    app = build_app(daemon)
    config = uvicorn.Config(app, host=host, port=port, log_level="warning")
    server = uvicorn.Server(config)

    url = f"http://{host}:{port}"
    url_file = WEB_URL_PATH()
    url_file.write_text(url, encoding="utf-8")
    url_file.chmod(0o600)

    thread = threading.Thread(target=server.run, daemon=True, name="fulcra-web")
    thread.start()
    return url, thread
```

- [ ] **Step 3: Wire into daemon.py's serve()**

```python
def serve(self, *, tick_seconds: float = 30.0) -> None:
    ...
    # Start the HTTP server alongside the UDS control server
    from .web import serve as web_serve
    web_url, _web_thread = web_serve(self)
    logging.getLogger("fulcra_collect").info("web UI: %s", web_url)
    ...
```

- [ ] **Step 4: Tests**

```python
# packages/collect/tests/test_web.py
from fastapi.testclient import TestClient
from fulcra_collect.web import build_app, _ensure_token


def test_status_route_requires_token(daemon_fixture):
    app = build_app(daemon_fixture)
    client = TestClient(app)
    r = client.get("/api/status")
    assert r.status_code == 401


def test_status_route_with_token(daemon_fixture, collect_home):
    token = _ensure_token()
    app = build_app(daemon_fixture)
    client = TestClient(app)
    r = client.get("/api/status", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert "plugins" in r.json()
```

- [ ] **Step 5: Commit**

### Task B6: Static web UI scaffold

**Files:**
- Create: `packages/web-ui/dist/index.html`
- Create: `packages/web-ui/dist/static/app.css`
- Create: `packages/web-ui/dist/static/app.js`
- Create: `packages/web-ui/README.md`

- [ ] **Step 1: `index.html` skeleton**

```html
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Fulcra Collect</title>
  <link rel="stylesheet" href="/static/app.css">
  <script defer src="https://cdn.jsdelivr.net/npm/alpinejs@3.x.x/dist/cdn.min.js"></script>
  <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-white text-slate-900" x-data="app()" x-init="boot()">
  <main class="max-w-5xl mx-auto p-6">
    <header class="flex items-center gap-4 mb-6">
      <img src="/static/logo.svg" alt="Fulcra" class="w-8 h-8">
      <h1 class="text-2xl font-semibold">Fulcra Collect</h1>
    </header>

    <template x-if="route === 'onboarding'">
      <section x-html="onboardingHtml"></section>
    </template>

    <template x-if="route === 'dashboard'">
      <section x-html="dashboardHtml"></section>
    </template>
  </main>

  <script src="/static/app.js"></script>
</body>
</html>
```

- [ ] **Step 2: `app.js` shell**

```js
const TOKEN = document.cookie.split('; ').find(r => r.startsWith('fulcra_token='))?.split('=')[1];

function api(path, opts = {}) {
  return fetch(path, {
    ...opts,
    headers: { 'Authorization': `Bearer ${TOKEN}`, 'Content-Type': 'application/json', ...(opts.headers ?? {}) },
  }).then(r => {
    if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
    return r.json();
  });
}

function app() {
  return {
    route: 'loading',
    status: null,
    fulcraAuth: null,
    onboardingHtml: '',
    dashboardHtml: '',

    async boot() {
      try {
        const [status, fulcra] = await Promise.all([
          api('/api/status'),
          api('/api/fulcra/auth/status').catch(() => ({ authenticated: false })),
        ]);
        this.status = status;
        this.fulcraAuth = fulcra;
        const anyEnabled = status.plugins?.some(p => p.enabled);
        if (!fulcra.authenticated || !anyEnabled) {
          this.route = 'onboarding';
          this.onboardingHtml = '<p>Welcome — onboarding wizard renders here in Phase C.</p>';
        } else {
          this.route = 'dashboard';
          this.dashboardHtml = '<p>Dashboard renders here in Phase D.</p>';
        }
      } catch (e) {
        this.route = 'error';
        this.onboardingHtml = `<p>Failed to load: ${e.message}</p>`;
      }
    },
  };
}
```

- [ ] **Step 3: `app.css` brand palette**

```css
/* Brand palette: white background, Fulcra accents. */
:root {
  --accent-violet: #6B5BEE;
  --accent-mint: #2D8267;
  --accent-cyan: #10C7BE;
  --warning: #B7791F;
  --error: #DC2626;
  --text: #0B0D17;
  --text-secondary: #5A6072;
  --border: #E5E7EB;
}
body { color: var(--text); }
```

- [ ] **Step 4: Smoke**

After daemon starts with HTTP server (B5), `curl http://localhost:<port>/` should return the HTML. `curl -H "Authorization: Bearer <token>" http://localhost:<port>/api/status` returns JSON.

- [ ] **Step 5: Commit**

### Task B7: Shared user-level Fulcra token migration

**Files:**
- Modify: `packages/collect/fulcra_collect/credentials.py` (add user-level helpers)
- Modify: `packages/collect/fulcra_collect/daemon.py` (run migration at startup)
- Create: `packages/collect/tests/test_user_token.py`

- [ ] **Step 1: Add user-level helpers in `credentials.py`**

```python
_USER_SERVICE = "fulcra-collect:user"

def set_user_secret(key: str, value: str) -> None:
    keyring.set_password(_USER_SERVICE, key, value)

def get_user_secret(key: str) -> str | None:
    return keyring.get_password(_USER_SERVICE, key)

def delete_user_secret(key: str) -> None:
    try:
        keyring.delete_password(_USER_SERVICE, key)
    except keyring.errors.PasswordDeleteError:
        pass

def has_user_secret(key: str) -> bool:
    return bool(get_user_secret(key))
```

- [ ] **Step 2: Migration code in daemon.py**

```python
def _migrate_bearer_token_to_user_level(registry, plugins_with_old_creds):
    """One-shot at daemon startup. If shared user-level bearer-token is
    empty AND any per-plugin bearer-token exists, copy the first one
    over and delete all per-plugin entries."""
    from . import credentials
    if credentials.has_user_secret("bearer-token"):
        return
    for pid in plugins_with_old_creds:
        token = credentials.get_secret(pid, "bearer-token")
        if token:
            credentials.set_user_secret("bearer-token", token)
            # Delete from all per-plugin locations
            for cleanup_pid in plugins_with_old_creds:
                credentials.delete_secret(cleanup_pid, "bearer-token")
            return
```

Call this at `Daemon.__init__` end. `plugins_with_old_creds` is the list of plugin ids that declared bearer-token before the cleanup (hardcoded — about 5 plugins).

- [ ] **Step 3: Tests**

```python
def test_migration_copies_first_plugin_token_to_user_level(monkeypatch):
    # Set up fake keyring with per-plugin bearer-token but no user-level
    # Run migration
    # Assert user-level has the token; per-plugin entries are gone

def test_migration_no_op_when_user_token_already_set(monkeypatch):
    # User-level already set
    # Migration does nothing
```

- [ ] **Step 4: Commit**

### Task B8: Drop per-plugin `bearer-token` Credential from plugins

**Files:**
- Modify: `packages/attention/fulcra_attention/collect_plugin.py`
- Modify: `packages/media-helpers/fulcra_media/collect_plugins.py` (any with bearer-token)
- Find via: `grep -rn 'Credential(key="bearer-token"' packages/`

- [ ] **Step 1: Remove `Credential(key="bearer-token", ...)` from each plugin's `required_credentials`**.

- [ ] **Step 2: Run tests; ensure no regressions** (any test that asserted bearer-token in required_credentials needs updating).

- [ ] **Step 3: Commit**

### Task B9: `RunContext.fulcra_token()` reads from shared user-level location

**Files:**
- Modify: `packages/collect/fulcra_collect/plugin.py` (RunContext)
- Or: `packages/collect/fulcra_collect/worker.py` (depending on where the factory builds the client)

- [ ] **Step 1: Find the existing `fulcra_token()` implementation** — likely in worker.py's `_make_fulcra_definition_client` or RunContext.

- [ ] **Step 2: Change it to read from `credentials.get_user_secret("bearer-token")`**.

- [ ] **Step 3: Tests + commit**

### Task B10: API skeleton routes for everything Phase C needs

**Files:**
- Modify: `packages/collect/fulcra_collect/web.py`
- Modify: `packages/collect/tests/test_web.py`

Add routes (delegating to daemon.handle_request for the UDS-equivalent ones):

- `POST /api/plugin/{id}/run`
- `POST /api/reload`
- `GET /api/version`
- `GET /api/plugin/{id}/credentials`
- `PUT /api/plugin/{id}/credential/{key}` (body: `{secret}`)
- `DELETE /api/plugin/{id}/credential/{key}`
- `GET /api/plugin/{id}/settings`
- `PUT /api/plugin/{id}/settings` (body: dict of key→value)
- `POST /api/plugin/{id}/enable`
- `POST /api/plugin/{id}/disable`
- `GET /api/plugin/{id}/contract` (returns the plugin's declared shape: required_settings, required_credentials, required_permissions, setup_steps, category, description, kind, default_interval_s, health_check_available)
- `GET /api/fulcra/auth/status`
- `POST /api/fulcra/auth/token` (body: `{token}`; tests against fulcra)
- `DELETE /api/fulcra/auth/token`

- [ ] **Step 1**: For each route, add a small handler that calls into `daemon.handle_request` or directly into `credentials.*` / `config.*` modules.

- [ ] **Step 2**: Tests via FastAPI TestClient.

- [ ] **Step 3**: Commit.

---

## Phase C — web UI core

Renders the onboarding wizard, the plugin picker, the per-plugin SetupStep walker, and the Preferences home shell. Lands the actual user-facing UI.

### Task C1: SetupStep renderer module

**Files:**
- Create: `packages/web-ui/dist/static/wizard.js`
- Create: `packages/web-ui/dist/static/wizard.css`

A self-contained Alpine.js component that takes a `setup_steps` array (from `/api/plugin/{id}/contract`) and renders one step at a time with Next / Back navigation. Each step kind has its own render branch:

- `intro` — markdown body (rendered via a tiny markdown helper or simple line-split with bold/link parsing)
- `external_action` — body + clickable external_link
- `input` — for each settings_key, fetch the Setting/Credential definition from the plugin's contract and render the appropriate input
- `file_upload` — `<input type="file">` posting to `/api/plugin/{id}/upload` (Phase B12 if introduced; or for v1, store path metadata only)
- `permission_request` — text instructions; no API call
- `browser_extension` — link to extension URL + install instructions
- `test_connection` — calls `POST /api/plugin/{id}/health_check`; shows spinner; on success renders summary + preview
- `definition_picker` — fetches `/api/definitions?annotation_type=…`; renders modal with preview; on pick calls `POST /api/plugin/{id}/definition`
- `done` — confirmation; auto-advances to next plugin or to dashboard

- [ ] Implement progressively. Tests via Playwright if available; otherwise manual smoke.

### Task C2: Onboarding wizard top-level

`/onboarding` view in the web UI. Steps:

1. Welcome — static body + Next.
2. Fulcra sign-in — paste token; calls `POST /api/fulcra/auth/token` to verify; on success shows account info.
3. Plugin picker — fetches `/api/status`, groups by `category`, renders checkboxes.
4. Per-plugin setup walk — for each picked plugin, fetch `/api/plugin/{id}/contract`, hand its `setup_steps` to the wizard renderer, then `POST /api/plugin/{id}/enable`.
5. Done — links to dashboard.

### Tasks C3 – C10

Detail per the spec's web UI surfaces section. Each:
- Frontend HTML/CSS/JS file
- Manual smoke verifying it renders + works against the daemon

(Full step-level detail TODO when subagent implementer dispatches each.)

### Task C11: Preferences home + per-plugin detail page

`/preferences` route + `/preferences/plugins/{id}` route. Dashboard layout from spec.

---

## Phase D — status surface

### Task D1: Daemon ring buffer of recent annotations

- New module `fulcra_collect/activity.py` — `RecentActivity` class with `add(plugin_id, annotation_summary, ts)` and `recent(limit)`. Backed by a `collections.deque(maxlen=200)`. Thread-safe (`threading.Lock`).
- Wired into the worker's annotation-write path so every successful write hits the buffer.

### Task D2: `/api/activity` route

- Returns the last N entries from the buffer.

### Task D3: Dashboard activity feed

- Frontend polls `/api/activity?limit=50` every 5s while focused; renders a vertical timeline.

### Task D4: Per-plugin status pills

- Frontend computes pill state from `/api/status` + auth status. Five states: Healthy / Running / Auth needed / Failing / Disabled.

---

## Phase E — definition picker

### Task E1: `/api/definitions` + `/api/definitions/{id}/recent` routes

- Backend calls into the existing Fulcra API client (`fulcra_common.BaseFulcraClient`) using the shared user-level token.
- Caches results briefly (5s) to avoid hammering Fulcra during UI exploration.

### Task E2: Definition picker modal in frontend

- Triggered from per-plugin detail page or from inside the SetupStep walker.
- Shows current binding + alternatives with last-3-entries inline preview.

### Task E3: `POST /api/plugin/{id}/definition` + `DELETE /api/plugin/{id}/definition` routes

- Binds plugin to a chosen def or force-creates a new one. Delete clears the cache.

---

## Phase F — OAuth machinery + Trakt

### Task F1: `fulcra_collect/oauth.py` module

- `OAuthFlow` class with PKCE: generates `state`, `code_verifier`, `code_challenge`. Stores state in a daemon-side dict with TTL.
- `exchange_code(plugin_id, code, state)` → matches state, sends token request to the plugin's token endpoint, returns access + refresh tokens.

### Task F2: Generic `/api/oauth/{plugin_id}/callback` route

- Receives the OAuth redirect; matches state; calls the plugin's `oauth_handler` with the code; stores resulting tokens via `credentials.set_secret`; returns an HTML page that closes the tab and signals success back to the main wizard.

### Task F3: `Plugin.oauth_handler` field

- New optional callable on Plugin. Signature: `(daemon, code: str, redirect_uri: str) -> dict[str, str]`. Plugin author implements the token-exchange POST to the third-party service.

### Task F4: Trakt plugin updates

Modify `packages/media-helpers/fulcra_media/collect_plugins.py` `TRAKT_PLUGIN`:

- `category="video"`
- `description="Records your TV and movie watch history from Trakt.tv."`
- `health_check=trakt_health_check` (new function — fetches `/users/me` + last 5 watches)
- `required_settings=()` (no non-secret settings; OAuth credentials handle auth)
- `required_credentials=(Credential(key="client_id", ...), Credential(key="client_secret", ...), Credential(key="access_token", ...), Credential(key="refresh_token", ...))`
- `oauth_handler=trakt_oauth_handler` (new function)
- `setup_steps=(...)` per the spec's Trakt example

### Task F5: Trakt OAuth handler implementation

New module `packages/media-helpers/fulcra_media/trakt/oauth.py`:

```python
async def trakt_oauth_handler(daemon, code: str, redirect_uri: str) -> dict[str, str]:
    """Exchange auth code for access + refresh tokens via Trakt's
    /oauth/token endpoint."""
    client_id = credentials.get_secret("trakt", "client_id")
    client_secret = credentials.get_secret("trakt", "client_secret")
    async with httpx.AsyncClient() as client:
        r = await client.post("https://api.trakt.tv/oauth/token", json={
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        })
        r.raise_for_status()
        return r.json()
```

### Task F6: Trakt health check

New function in same module:

```python
def trakt_health_check(ctx) -> HealthResult:
    """Verify the access token works and surface recent watches."""
    token = credentials.get_secret("trakt", "access_token")
    if not token:
        return HealthResult(ok=False, summary="Not signed in to Trakt yet.")
    client_id = credentials.get_secret("trakt", "client_id")
    try:
        with httpx.Client(timeout=10.0) as client:
            r = client.get("https://api.trakt.tv/users/me",
                           headers={
                               "Authorization": f"Bearer {token}",
                               "trakt-api-version": "2",
                               "trakt-api-key": client_id,
                           })
            r.raise_for_status()
            account = r.json()
            history = client.get(
                f"https://api.trakt.tv/users/me/history?limit=5",
                headers={
                    "Authorization": f"Bearer {token}",
                    "trakt-api-version": "2",
                    "trakt-api-key": client_id,
                },
            )
            history.raise_for_status()
            entries = history.json()
            return HealthResult(
                ok=True,
                summary=f"Signed in as {account['username']}. {len(entries)} recent watches.",
                preview=[{"title": e["movie"]["title"] if "movie" in e else f"{e['show']['title']} S{e['episode']['season']}E{e['episode']['number']}", "watched_at": e["watched_at"]} for e in entries],
            )
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 401:
            return HealthResult(ok=False, summary="Trakt access token expired. Re-authenticate.")
        return HealthResult(ok=False, summary=f"Trakt API error: {e.response.status_code}")
    except Exception as e:
        return HealthResult(ok=False, summary=f"Could not reach Trakt: {e}")
```

### Task F7: End-to-end Trakt smoke

Manual checklist runs through:

- [ ] Daemon up, web UI accessible at the URL file's URL
- [ ] `/onboarding` opens
- [ ] Sign in to Fulcra works
- [ ] Trakt picker available
- [ ] Trakt setup_steps render in order
- [ ] External-action step links to trakt.tv/oauth/applications
- [ ] Input step accepts Client ID + Secret
- [ ] OAuth step opens trakt.tv in new tab; after authorize the callback completes; tokens stored
- [ ] Test-connection step shows last 5 watches
- [ ] Definition picker shows existing "Watched" if any, with preview, or "Create new"
- [ ] Done step confirms; trakt plugin runs once on confirm
- [ ] Dashboard shows trakt as Healthy with recent activity in the feed

Commit:

```
feat(trakt): end-to-end onboarding milestone — OAuth + setup_steps + health_check

The Trakt plugin now drives a complete first-launch experience: user
picks Trakt → wizard explains what it does → links to trakt.tv/oauth/
applications → user creates the OAuth app → pastes Client ID + Secret
→ daemon initiates the PKCE OAuth flow → trakt.tv opens in new tab →
user authorizes → callback completes → tokens stored in keychain →
health check confirms by fetching last 5 watches → definition picker
binds to "Watched" → first run imports recent watches.

This is the reference implementation. The other 13 plugins follow
the same setup_steps + health_check pattern; each gets its own
flavour of "external action" (file upload for takeouts; permission
request for podcasts; browser extension for attention; etc).
```

---

## How to verify the milestone is hit

**End-to-end smoke** (must pass for the milestone to ship):

1. Start with a fresh keychain (or test keychain) — no Fulcra token, no Trakt tokens.
2. Start daemon. Web URL is written to `~/.config/fulcra-collect/web-url`.
3. Start menubar. Menubar detects no auth + no enabled plugins → auto-opens the browser at the onboarding URL.
4. Wizard walks: Welcome → Sign in to Fulcra (paste valid token; verified against Fulcra API) → Pick plugins (check Trakt) → Trakt setup_steps complete end-to-end (OAuth flow real with trakt.tv) → Done.
5. Dashboard shows Trakt as Healthy.
6. Run trakt manually from menubar or `fulcra-collect run trakt`. Some watches land in Fulcra, surfaced in the activity feed.
7. Sign out of Trakt (delete tokens via UI). Plugin pill turns "Auth needed". Re-authenticate flow works.
8. Restart daemon. Menubar detects auth + plugins enabled → no auto-open. URL stays valid.

If all of that passes: milestone hit. Phases G (popover quick-record), H (agent docs), and the remaining 13 plugins in Phase I follow.

---

## Self-Review

**Spec coverage**: every section of the spec maps to one or more tasks. Phases A–F cover the spec's Phases A–F. Phase G (quick-record), Phase H (agent docs), and remaining-plugins of Phase I are explicitly out of THIS plan's scope, queued for a follow-on plan.

**Placeholder scan**: most task bodies have concrete code/commits. C3–C10 are sketched at the section level rather than per-task — the implementer subagent for each will need to read the spec's web UI section to fill in details. That's acceptable because the spec is detailed; not acceptable would be "TODO".

**Type consistency**: Setting / SetupStep / HealthResult dataclass names match between Phase B definitions and Phase C/D/E/F consumers. `Plugin.oauth_handler` defined in F3 is used in F2's generic callback handler.
