# fulcra-collect menubar — design (sub-project 2: macOS menubar UI)

**Date:** 2026-05-22
**Status:** Draft. Pending user review.

## Context and decomposition

This is sub-project 2 in the three-part `fulcra-collect` roadmap laid out
in `2026-05-22-fulcra-collect-design.md`:

1. **Headless hub core + plugin API.** *Shipped.* The `fulcra-collect`
   daemon discovers plugins via entry points, schedules them, supervises
   service plugins, stores credentials in the OS keychain, and answers
   JSON requests over a Unix domain socket. Seventeen plugins are
   registered today.
2. **Menubar / tray UI** — *this spec.* The user-facing shell on top of
   sub-project 1.
3. **Public packaging** — code-signing, notarization, installer,
   auto-update. Its own spec later.

This spec covers macOS only. A Linux tray app will be a separate project
later — it shares nothing with this one except the wire protocol (JSON
over UDS), which is already defined by sub-project 1. Windows is out of
scope.

The UI is a **thin client**. It owns no plugin logic, no scheduling, no
credentials, no watermark state. It reads the daemon's snapshot and
issues control-socket commands. The daemon remains fully usable
headlessly via the `fulcra-collect` CLI; nothing in this spec changes
that interface.

## Goal

A macOS menubar app — `Fulcra Collect.app` — that:

- Shows at a glance whether the hub is healthy.
- Lists every discovered plugin with its kind, last-run time, current
  status, and (if any) last error.
- Lets the user fire a manual or scheduled plugin on demand, reload
  config, enable/disable plugins, and connect credentials.
- Notifies the user when a plugin has been failing consecutively.
- Reads **light**: pure-white surfaces with Fulcra's brand accents
  (violet, mint, cyan, plus the brand gradient) tuned to remain legible
  on white. The dark-themed brand surfaces the user knows from the
  public site are not the model for this app — white is.

## Package

- Product name: **Fulcra Collect.app**
- Directory: `packages/menubar/` (a standalone Xcode project; *not* a
  `uv` workspace member — Python doesn't enter this directory).
- Bundle identifier: `com.fulcradynamics.collect.menubar`
- Distribution: a `.app` produced by `xcodebuild`. Signing and
  notarization land in sub-project 3.
- Language: Swift 5.10+ targeting **macOS 13 (Ventura)** or newer.
- Frameworks: SwiftUI for the popover and Preferences window; AppKit for
  `NSStatusItem`; `Network.framework` for the UDS socket; the
  `UserNotifications` framework for failure alerts.

### Stack decision (Swift vs. Python+rumps)

The rest of the monorepo is Python, so picking Swift here is a
deliberate departure. The trade-off:

- **Swift / SwiftUI / AppKit** (chosen). Native menubar UX, easy to nail
  the brand-on-white aesthetic, ships as a small `.app`. Cost: a new
  toolchain (Xcode) and a new language in the repo. Sub-project 3's
  code-signing/notarization story is also more straightforward for a
  signed `.app` than for a Python-bundled binary.
- **Python + `rumps`** (rejected). Stays in-language, but `rumps` gives
  a basic `NSStatusItem` only — a custom popover with rows, buttons,
  badges, and tinted accents is hand-written PyObjC. The line count to
  reach parity with a SwiftUI implementation is roughly the same, with
  worse polish and worse tooling.
- **Tauri (Rust + webview)** (rejected). Brings full HTML/CSS, but adds
  Rust to the repo and a webview process per menubar window. Overweight
  for a status display.

The decision is reversible: the daemon's wire protocol is the contract.
A Python rewrite later would not change a line of sub-project 1.

## Communication with the daemon

The app speaks the JSON request/response protocol the daemon already
exposes (`packages/collect/fulcra_collect/daemon.py:56-65`). No new
transport.

The control socket lives at `~/.config/fulcra-collect/control.sock`.
Filesystem-permissioned (0600), local, owner-only. The menubar app
connects as the same user as the daemon — anything else fails by design.

### Connecting

On launch and after every connection error, the app:

1. Opens an `NWConnection` over `.unix(path: ...)`.
2. Sends `{"cmd":"status"}\n` and reads one newline-terminated JSON
   response.
3. If `connect()` refuses or the file doesn't exist, the app enters a
   **Daemon stopped** state and the popover shows the bootstrap card
   (see "Bootstrap" below).

### Polling

The polling schedule has two regimes:

- **Popover open:** poll `status` every 2 seconds. The user wants live
  feedback; this is the only window where that matters.
- **Popover closed:** poll every 10 seconds. Just often enough to update
  the menubar icon badge and fire failure notifications.

Polling is suspended while the machine is asleep — the app subscribes to
`NSWorkspace.willSleepNotification` / `didWakeNotification`. On wake,
the next tick fires immediately.

### Existing commands (used as-is from sub-project 1)

| Command | Response | Used by |
|---------|----------|---------|
| `{"cmd":"status"}` | `{ok, plugins:[…], load_errors:{…}}` — per-plugin: `id, name, kind, enabled, last_run, last_outcome, last_error, consecutive_failures`. | Every poll. The popover and the menubar icon both observe the same snapshot. |
| `{"cmd":"run","plugin":"<id>"}` | `{ok, started}` | The "Run now" button. |
| `{"cmd":"reload"}` | `{ok}` | The "Reload config" footer button, and after every config-file edit from Preferences. |

### New commands (added to sub-project 1 by this spec)

The menubar's Preferences pane needs to read which credentials a plugin
has, write secrets to the keychain, clear them, and report a version
string for the About pane. These belong on the daemon — the keychain is
its keychain. This spec adds four small handlers to
`daemon.handle_request`:

| Command | Response | Notes |
|---------|----------|-------|
| `{"cmd":"version"}` | `{ok, daemon_version, plugins:{"<id>": "<pkg_version>"}}` | Read from each plugin's distribution metadata. Cheap, cached at startup. |
| `{"cmd":"credential_status","plugin":"<id>"}` | `{ok, credentials:{"<key>": "set"\|"missing"}}` | Reports which `required_credentials` are present in the keychain, **never** their values. |
| `{"cmd":"set_credential","plugin":"<id>","key":"<key>","secret":"<secret>"}` | `{ok}` | Thin pass-through to `credentials.set`. |
| `{"cmd":"delete_credential","plugin":"<id>","key":"<key>"}` | `{ok}` | Thin pass-through to `credentials.delete`. |

Secrets cross the socket in plaintext. The socket is local, mode 0600,
owner-only, and never reachable from another process or machine — so
this is no weaker than the keychain access already granted to processes
running as the user.

These four handlers are a small pre-work item in sub-project 1, not a
new sub-project. They are listed at the bottom of this spec under
"Required pre-work in sub-project 1" so the implementation plan picks
them up first.

## Menubar status item

An `NSStatusItem` carrying a monochrome Fulcra mark as a **template
image** so macOS tints it to match the menubar's light/dark theme. The
icon adopts four states, layered on top of the template:

- **Idle** — the mark, no overlay.
- **Running** — a subtle pulsing violet glow (`#6B5BEE` at low alpha)
  drawn as a separate `CALayer` so the template stays monochrome. The
  app's *in-flight set* drives this: a plugin id is added when "Run now"
  is fired and removed when the next `status` poll shows its `last_run`
  has advanced past the trigger time.
- **Has failure** — a small red dot (`#DC2626`) in the bottom-right of
  the icon whenever any plugin in `status.plugins[]` has
  `consecutive_failures > 0`.
- **Daemon stopped** — the mark at 40% opacity, tooltip "Fulcra Collect
  daemon not running".

A left click opens the popover. A right click opens a small fallback
menu (Reload config, Open Preferences, Quit) for users who prefer not
to see the popover.

## Popover

A SwiftUI popover anchored to the menubar item. Width 360pt, max height
600pt (scrolls), white background, 14pt corner radius, standard macOS
material shadow.

```
┌───────────────────────────────────────────────────────────┐
│  Fulcra Collect                              ●  Healthy   │
│  17 plugins · 14 scheduled · 2 services · 1 manual        │
├───────────────────────────────────────────────────────────┤
│  Services                                                 │
│    attention-relay        ●  Running      ⋯               │
│    media-webhook          ●  Running      ⋯               │
│                                                           │
│  Scheduled                                                │
│    lastfm                 ●  2 min ago    Run now         │
│    spotify-extended       ●  4 min ago    Run now         │
│    trakt                  ⚠  12 min ago   Run now         │
│      ↳ last error: "401 unauthorized — reconnect"         │
│    …                                                      │
│                                                           │
│  Manual                                                   │
│    dayone                 –  Never run    Run now         │
│    apple-takeout          –  Never run    Run now         │
├───────────────────────────────────────────────────────────┤
│  Reload config   Preferences…                       Quit  │
└───────────────────────────────────────────────────────────┘
```

### Header

- **Title:** "Fulcra Collect", 16pt semibold, primary text colour.
- **Status pill** (right), one of:
  - **Healthy** — mint dot (`#1E8F5D`) when every enabled plugin has
    `consecutive_failures == 0`.
  - **N failing** — red dot (`#DC2626`) when one or more enabled
    plugins are in failure.
  - **Running…** — pulsing violet dot (`#6B5BEE`) when the in-flight
    set is non-empty.
  - **Daemon stopped** — grey dot (`#9CA3AF`) when the socket is
    unreachable.
- **Subtitle:** counts by kind, in secondary text colour, 12pt.

### Plugin list

Sectioned by kind (Services, Scheduled, Manual). Each row is 44pt tall:

- **Left:** a 10pt status dot — mint for ok, amber for "running",
  red for failing, grey for disabled.
- **Centre:** plugin `name` in 14pt; `id` underneath in 11pt secondary
  for disambiguation when names collide.
- **Right:**
  - **Scheduled:** relative `last_run` ("2 min ago", "—" if never) plus
    a "Run now" button.
  - **Manual:** "Never run" / "<when>" plus a "Run now" button.
  - **Service:** a "Running" / "Restarting" / "Crashed" pill — no Run
    button (services aren't fired ad-hoc).
- If `last_error` is non-empty, an inline disclosure row underneath
  shows the error in 11pt monospaced. Tap-to-copy.

The "Run now" button is filled violet (`#6B5BEE`) with white text. On
tap it shows a brief spinner, the plugin id joins the in-flight set,
and the next status poll resolves it.

Disabled plugins render at 40% opacity and have no "Run now" button.

### Footer

- **Reload config** — sends `{"cmd":"reload"}`. Brief toast on success.
- **Preferences…** — opens the Preferences window.
- **Quit** — terminates the menubar app *only*. The daemon keeps
  running. A one-line confirmation dialog the first time prevents users
  from quitting and expecting to stop the hub.

## Preferences window

Standard macOS preferences window, ~640pt wide, three tabs.

### Plugins tab

Same list as the popover but each row is expanded:

- **Enable** toggle — edits the `enabled = [...]` list in
  `~/.config/fulcra-collect/config.toml`, then sends `reload`. The UI
  edits config through this one file path only; it never writes
  anywhere else.
- **Interval** input (scheduled plugins only) — hours and minutes;
  writes to `intervals.<id>` in `config.toml`; `reload` follows.
- **Credentials** — one row per `required_credential` declared by the
  plugin (id + human label, e.g. "Last.fm session key"). Each row is
  either:
  - "Connected" with a "Disconnect" button → `delete_credential`, or
  - A masked text field plus a "Connect" button → `set_credential`.
  The "set" / "missing" status comes from `credential_status` and
  re-polls on tab open and after every change.
- **Run now** button (same as popover).

A plugin's `Permission` declarations are listed **read-only** —
explaining what the plugin will ask the OS for the first time it runs.
Permission *grants* are not managed by this UI; macOS owns those
dialogs and `fulcra-collect doctor` (CLI) inspects them.

### Notifications tab

- **Notify me when a plugin fails repeatedly** — toggle, defaults on.
- Threshold is `consecutive_failures ≥ 3`. Hardcoded for v1.
- Each plugin can produce at most one notification per hour
  (de-duplication, in-process).
- A second toggle: **Mute all** — overrides everything.

### About tab

- App version, daemon version (from `{"cmd":"version"}`), per-plugin
  package versions.
- Config-file path with **Show in Finder**.
- State-directory path with **Show in Finder**.
- **Open Activity Logs** — opens the daemon's launchd stdout/stderr
  files (typically `~/Library/Logs/com.fulcradynamics.collect.log`,
  whatever the daemon's `StandardOutPath` is) in Console.app.
- **Launch at login** toggle — managed via `SMAppService.mainApp`.

## Bootstrap (daemon not installed or not running)

When the socket is unreachable, the popover renders an onboarding card
instead of the plugin list:

```
┌───────────────────────────────────────────────────────────┐
│  Fulcra Collect is not running.                           │
│                                                           │
│  The Fulcra Collect daemon hosts your local importers     │
│  and is required for this menubar.                        │
│                                                           │
│       [  Install & start daemon  ]                        │
│                                                           │
│  Already installed? Try:                                  │
│    fulcra-collect service start                           │
└───────────────────────────────────────────────────────────┘
```

The button shells out to:

```
fulcra-collect service install
fulcra-collect service start
```

via `Process` with stdout/stderr captured into a small log sheet
(useful for diagnosing "not on PATH" cases).

The app does **not** bundle the daemon binary. The user must have
`fulcra-collect` on `PATH` (installed via `uv tool install
fulcra-collect` or, later, the homebrew bottle from sub-project 3). If
`fulcra-collect` is not found, the card swaps to an "Install
fulcra-collect first" link to the README.

This keeps the menubar and the daemon properly decoupled: the .app is
just a UI; the Python tool is the engine.

## Notifications

Native macOS notifications via `UserNotifications`. The app requests
authorization on first launch.

Triggers (each de-duplicated to at most one per plugin per hour):

- A plugin's `consecutive_failures` crossed 3.
- The daemon process exited while the menubar was running (socket
  transitions from connected to refused).

Each notification carries a single **Open** action that surfaces the
popover.

The **Mute all** toggle in Preferences > Notifications skips the post.
Authorization denial is silent — never blocks the UI.

## Visual design

**The background is white.** Not "system material that resolves to white
in light mode", not "off-white #FAFAFA", not "follows the system
appearance". Every surface this app draws — popover, Preferences window,
sheets, alerts — is `#FFFFFF`. The Fulcra brand surfaces the user has
been shown live on dark; this app inverts that and renders the brand
accents on white, by design.

The accents are Fulcra's existing brand colours, sampled from the
reference materials the user provided (the outline violet/mint buttons
on the public site, the filled mint CTAs on the product page, the
cyan→teal→violet hex gradient on the same page). They are kept exactly
as the brand uses them where contrast on white allows; the brighter
violet and the mid-cyan get a small luminance shift so the buttons
don't glow on a white surface.

### Palette

| Token | Hex | Use |
|-------|-----|-----|
| `--bg` | `#FFFFFF` | popover and Preferences background — never overridden |
| `--bg-elev` | `#F7F8FA` | section headers, hover state, pressed row |
| `--border` | `#E5E7EB` | hairlines between rows |
| `--text` | `#0B0D17` | primary text |
| `--text-secondary` | `#5A6072` | subtitles, ids, timestamps |
| `--text-tertiary` | `#9CA3AF` | disabled, hint text |
| `--accent-violet` | `#6B5BEE` | primary buttons, "Run now", in-flight glow |
| `--accent-violet-hover` | `#5045E5` | hover/pressed |
| `--accent-violet-tint` | `#F1EFFE` | violet surface fills |
| `--accent-mint` | `#2D8267` | "Healthy" pill, success dot, filled mint CTAs (matches the filled green buttons in the reference) |
| `--accent-mint-hover` | `#226A53` | hover/pressed |
| `--accent-mint-tint` | `#E5F4EE` | success surface fills |
| `--accent-cyan` | `#10C7BE` | inline links, info hints, the lighter stop of the brand gradient |
| `--accent-cyan-deep` | `#0E9E97` | links on white where `#10C7BE` is too light |
| `--brand-gradient` | `linear-gradient(135deg, #10C7BE 0%, #4F7BE8 50%, #8B5BEE 100%)` | used sparingly — the running-pulse on the menubar icon, the bootstrap card's accent stripe. Not used as a background. |
| `--warning` | `#B7791F` | amber for "running" and warning state |
| `--error` | `#DC2626` | failure dot, error text |

These hexes are this spec's **first proposal**, sampled from the
reference materials. They should be cross-checked against the Fulcra
brand kit during implementation; if the kit disagrees, the kit wins and
this table updates. The white background is **not** negotiable in that
review — that is the design choice for this app and the reason the
accents are tuned the way they are.

### Typography

- System font (SF Pro Display) at standard SwiftUI scales.
- 16pt semibold — popover title.
- 14pt regular — row labels, body.
- 12pt regular — subtitles, secondary metadata.
- 11pt monospaced (SF Mono) — error strings, ids on disambiguation
  lines.

### Iconography

SF Symbols throughout (`circle.fill`, `arrow.clockwise`, `gear`,
`square.and.pencil`, `xmark.circle`). The menubar item is a custom
template PDF asset (the Fulcra mark, monochrome) shipped in the asset
catalogue.

## Components

```
packages/menubar/
  FulcraCollect.xcodeproj/
  FulcraCollect/
    App/
      FulcraCollectApp.swift         # @main, NSStatusItem, popover wiring
      AppDelegate.swift              # popover lifecycle, sleep/wake
    Daemon/
      DaemonClient.swift             # NWConnection + JSON request/response
      DaemonModel.swift              # @Observable status snapshot
      PollingScheduler.swift         # 2s open / 10s closed, sleep-aware
    UI/
      StatusItemView.swift           # menubar icon + badge overlay
      Popover/
        PopoverRoot.swift
        Header.swift
        PluginRow.swift
        BootstrapCard.swift
      Preferences/
        PreferencesWindow.swift
        PluginsTab.swift
        NotificationsTab.swift
        AboutTab.swift
    Notifications/
      NotificationCentre.swift       # debounced post helpers
    Theme/
      Palette.swift                  # the colour tokens above
      Typography.swift
    Assets.xcassets/
      MenubarIcon                    # template PDF
      AppIcon
  FulcraCollectTests/
    DaemonClientTests.swift          # fake UDS server fixture
    PollingSchedulerTests.swift
    NotificationCentreTests.swift    # de-dup logic
```

`Daemon/` is a pure model layer: JSON in, structs out, no UI. SwiftUI
views observe it. This is the only layer with unit tests; the UI is
exercised by manual smoke and by snapshot screenshots at three states.

## Data flow

1. `FulcraCollectApp` instantiates `DaemonClient` and `DaemonModel` at
   launch.
2. `PollingScheduler` calls `DaemonClient.status()` on its schedule.
   The decoded snapshot is published into `DaemonModel`
   (`@Observable`).
3. `StatusItemView` observes `DaemonModel` to choose its rendering
   (idle / running / failure / down).
4. Clicking the status item shows the popover, which observes the same
   model.
5. Clicking **Run now** calls `DaemonClient.run(pluginId:)` and adds
   the id to the in-flight set; the next status poll removes it when
   `last_run` advances.
6. Failure-threshold detection runs after every status update: a plugin
   transitioning from `consecutive_failures < 3` to `>= 3` triggers a
   notification (de-duped per hour).
7. Preferences config edits write to
   `~/.config/fulcra-collect/config.toml` using a Swift TOML library
   (TOMLKit), then send `{"cmd":"reload"}`. Two clients (CLI + UI)
   editing the same file is acceptable: writes are short, the schema is
   small, and the daemon re-reads on reload.

## Error handling

- **Socket refused or file missing:** "Daemon stopped" state.
  `PollingScheduler` already throttles to 10s closed / 2s open, so no
  retry storm.
- **Socket open but request times out (5s):** render the most recent
  good snapshot with a "Stale" banner; retry next tick.
- **JSON decode error:** log to the app's local log file, surface the
  offending JSON in About > Open Activity Logs.
- **`set_credential` / `delete_credential` fails:** toast with the
  error string; the row remains in its previous "Connected" / "Missing"
  state until the next `credential_status` poll resolves it.
- **Notification permission denied:** silently skip notifications;
  never block the UI.

The daemon is the source of truth. The UI never invents state.

## Testing

- **`DaemonClientTests`** — spins up an in-process fake daemon over a
  `socketpair`, answers canned JSON to each command. Verifies request
  framing, JSON decoding, and the 5s timeout.
- **`PollingSchedulerTests`** — uses a fake clock to verify the
  2s-open / 10s-closed cadence and sleep/wake suspension.
- **`NotificationCentreTests`** — one-per-plugin-per-hour de-dup, and
  "Mute all" suppresses everything.
- **Snapshot tests** — three popover states (healthy, running,
  failing) and the bootstrap card, via `swift-snapshot-testing`. Run
  in `xcodebuild test`.
- **Manual smoke** — listed in the implementation plan as a final
  checklist (popover renders, Run now fires the daemon, a forced
  failure produces a notification, Preferences edits round-trip
  through `reload`).

CI does not run the menubar tests against a real daemon — that would
need a real keychain entry and a Fulcra token. The fake UDS fixture
covers the protocol; the rest is local-machine validation.

## Deployment

Sub-project 2 produces `Fulcra Collect.app` via `xcodebuild`. The
implementation plan stops at "builds, runs locally, talks to my daemon"
— that is sub-project 2's done line.

Sub-project 3 (separate spec) adds code-signing, notarization, the
hardened runtime, a homebrew cask, and auto-update. Until then, the app
runs from "Build and Run" in Xcode or from a locally-installed unsigned
build.

On first launch, the app offers to set itself as a login item
(`SMAppService.mainApp`). Removing it is a toggle in Preferences >
About.

The app does **not** bundle the daemon. The bootstrap card teaches the
user to install `fulcra-collect` if it isn't on PATH.

## Required pre-work in sub-project 1

These four small handlers are added to `fulcra_collect/daemon.py`
*before* menubar implementation starts. Each is a thin pass-through
over code that already exists in the daemon:

1. `{"cmd":"version"}` → reads from `importlib.metadata` for the
   `fulcra-collect` distribution and each registered plugin's
   distribution. Cached at daemon startup.
2. `{"cmd":"credential_status","plugin":"<id>"}` → for each
   `required_credential` declared by the plugin, calls
   `credentials.has(plugin_id, key)` (a new tiny helper over
   `keyring.get_password`) and returns `"set"` or `"missing"` — never
   the value.
3. `{"cmd":"set_credential", …}` → `credentials.set(plugin_id, key,
   secret)`. Wrapped in the same UDS-mode-0600 guard the rest of the
   handlers rely on.
4. `{"cmd":"delete_credential", …}` → `credentials.delete(plugin_id,
   key)`.

These four ship with their own unit tests (round-trip through a fake
keychain) and a small CHANGELOG note in `packages/collect/`. The plan
treats them as task zero of the menubar work.

## Out of scope

- **Linux tray.** Separate stack, separate UI. Will share only the JSON
  protocol.
- **Windows.** Not a target.
- **Code-signing, notarization, auto-update, homebrew cask.**
  Sub-project 3.
- **Visual plugin install / uninstall.** Plugins ship as Python
  distributions; install via `uv tool install` (or the future bottle).
- **Permission-grant management UI.** macOS owns those dialogs;
  `fulcra-collect doctor` is where they're inspected.
- **Multi-machine dashboard.** Each menubar is local to its hub. A user
  with two laptops runs two menubars.
- **Custom plugin views.** A plugin can't ship its own UI for v1; all
  rows look the same. This is a deliberate constraint to keep the
  status surface uniform.

## Open questions

- **Brand-kit verification.** The palette table above is a synthesized
  first proposal. Confirm hexes against the Fulcra brand kit during
  implementation; if they disagree, update this table.
- **Restarting vs. Crashed pill.** Is a single red badge on the menubar
  icon enough to differentiate a service that's flapping from one
  that's fully crashed? Recommendation: yes for v1, single badge. The
  per-row pill in the popover carries the nuance.
- **Heartbeat field on the daemon.** Does sub-project 1 need to add a
  `last_seen` timestamp to status responses for the UI's "Stale"
  detection? Recommendation: not yet — a successful status response is
  by definition fresh, and the 5s timeout already detects a stuck
  daemon.
- **Multiple menubar instances per user.** Two `Fulcra Collect.app`
  processes for the same user are harmless (both just poll). Document
  in the README; don't enforce singleton behaviour.

## Future (not in scope, recorded for later)

- Light/dark theme switch in the popover. (v1 is light-only by design;
  the brand-on-white is the entire visual hook.)
- A Linux GTK tray app that consumes the same JSON protocol.
- Per-plugin custom rows (a plugin ships a tiny SwiftUI snippet via a
  manifest field, the menubar renders it). Heavy lift; out for now.
- An iOS / iPadOS companion that connects to the daemon over Tailscale.
- Integration with the Fulcra Context macOS app (if/when it exists) so
  the menubar can hand off to a richer view.
