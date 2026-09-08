# fulcra-menubar

The Mac app for Fulcra Collect. It lives in the menu bar, shows import status,
and opens the dashboard for setup and settings.
It is the optional local connector UI in this [unsupported monorepo](../../README.md).
Fulcra itself and tools that call its API directly do not need the menu-bar app.

If a setting changes elsewhere while you are editing it, Collect keeps the saved
value and asks you to review it before retrying. Native enable and interval controls
restore the current value when that happens.

## Install

Use the [Mac download and setup guide](../../docs/collect.md#get-started-new-user).
Open the disk image, drag **Fulcra Collect** into **Applications**, then launch it
from there. Click its menu-bar icon and choose **Install & start daemon** if
prompted. Open the dashboard and sign in with Fulcra.

The beta is signed, notarized, and built for Apple silicon. It includes its
Python runtime, Fulcra CLI, dashboard, and plugins, including Apple Notes, Apple Reminders, and Todoist. Chrome
Attention is an optional extension in the disk image with separate setup.
[Release details and plugin status](../../docs/collect.md#plugin-status).

The commands below are for development.

## Run in dev mode

    cd path/to/fulcra-tools
    uv sync --all-packages --all-extras
    uv run --all-packages --all-extras python -m fulcra_menubar

Run the daemon in a second terminal with
`uv run --all-packages --all-extras fulcra-collect daemon`, or follow the
[source service setup](../collect/README.md#running-from-source). Launch the
menu-bar app from a logged-in macOS GUI session; SSH alone will not show it.
The menubar icon appears in the top-right of the screen; click for the
rumps menu, then "Open Fulcra Collect" for the popover.

## Tests

    uv run --package fulcra-menubar --extra dev pytest packages/menubar/tests/ -q

The pure-model layer (daemon_client, model, polling, notifications)
runs everywhere — Linux CI included. The view layer (status_item,
popover, preferences) is exercised by manual smoke; see the checklist
below.

## Task plugins in the unreleased 0.1.2 candidate

The 0.1.2 candidate bundles [Apple Reminders](../apple-reminders/README.md),
[Todoist](../todoist/README.md), and the shared task sync engine. It declares both macOS Reminders usage strings
and dispatches `--reminders-bridge` before importing the GUI. These additions are awaiting final validation and release. Native permission is requested
only from the setup wizard's **Allow access** button.

## Build the .app

From the repository root on an Apple silicon Mac:

```sh
UV_PYTHON=3.13 bash packages/menubar/scripts/build_macos_app.sh
bash packages/menubar/scripts/verify_bundle.sh
```

The Briefcase build includes workspace wheels, Python, native command launchers,
and the web setup interface. It writes the app under
`packages/menubar/build/fulcra-menubar/macos/app/`. The build rejects embedded
builder-home paths and removes nonportable pip scripts before signing.

For distribution, use `packages/menubar/scripts/release_dmg.sh` with an approved
Developer ID identity and a configured notary profile. It signs, notarizes,
staples, and checks the installer. An unnotarized development build is not the
normal installer; do not ask users to bypass Gatekeeper.

## Manual smoke checklist

Run before merging any view-layer change.

- [ ] Daemon stopped → popover shows bootstrap card with "Install &
      start daemon" enabled.
- [ ] Daemon stopped → menubar icon at 40% opacity.
- [ ] Daemon running, no failures → popover shows plugin list grouped
      by kind; status pill is "Healthy" (mint dot); icon is opaque, no
      badge.
- [ ] Force a plugin to ≥3 consecutive failures → popover row shows red
      dot + error line; status pill flips to "N failing"; menubar icon
      gets a red dot; a macOS notification appears (once per hour).
- [ ] Click "Run now" on a manual plugin → row updates, menubar icon
      pulses violet for ~the duration of the run.
- [ ] Preferences > Plugins → toggle Enable → `~/.config/fulcra-collect/config.toml`
      reflects the change; popover plugin list redraws.
- [ ] Preferences > Plugins → set interval → config.toml's
      `interval_overrides` updates; daemon reloads.
- [ ] Preferences > Plugins → Connect credential → daemon receives
      `set_credential`; `credential_status` flips to "set" on next
      tab redraw; Disconnect reverses it.
- [ ] Preferences > Notifications → Mute all → no notifications fire.
- [ ] Preferences > About → daemon version + plugin versions populate.
- [ ] Lid close / wake → next status poll fires immediately on wake.
- [ ] Quit from the rumps menu → app exits cleanly; daemon keeps
      running.

## Architecture

Two layers:

1. **Pure-model layer** — no PyObjC imports, full unit tests.
   - `daemon_client.py` — typed wrapper over
     `fulcra_collect.control.send_request`.
   - `model.py` — `StatusModel`: snapshot + in-flight + observer
     protocol + failure-transition observer.
   - `polling.py` — `PollingScheduler` (2s open / 10s closed,
     sleep-aware).
   - `notifications.py` — failure-notification de-dup
     (1/category/hour).
   - `theme/palette.py` — hex constants.

2. **View layer** — PyObjC; manual smoke only.
   - `app.py` — `rumps.App` subclass, wires everything.
   - `status_item.py` — menubar icon + badge + running pulse.
   - `popover/*` — the click-to-show popover (header, plugin rows,
     bootstrap card).
   - `preferences/*` — NSWindowController + tabs.
   - `theme/colors.py`, `theme/typography.py` — PyObjC NSColor / NSFont
     factories.

The daemon owns all plugin logic, scheduling, supervision, watermarks,
and credentials. This app is a thin client; it reads daemon state and
issues control-socket commands.

## Deep-linking into the web UI

The popover's header "?" button and per-row Configure button both open
the daemon's web UI at a `?route=...` URL using
`subprocess.run(["open", url], check=False)` — the standard macOS
default-browser opener. Added in SP4 (drift audit 2026-05-27) so
common follow-ups (read the docs, finish configuring a plugin) don't
require the user to context-switch to the dashboard and hunt for the
right entry point.

URLs emitted today:

- `/?route=docs` — `popover/header.py`, the "?" button next to the
  status pill.
- `/?route=configure&plugin=<id>` — `popover/plugin_row.py`, the per-row
  Configure button.

Both call sites construct the full URL via `daemon_url(path)` from
`fulcra_menubar/_daemon_url.py`. That helper reads the daemon's
well-known `~/.config/fulcra-collect/web-url` file (written by the
daemon at startup; respects the user's `[daemon] web_port` override)
and falls back to `http://127.0.0.1:<configured-port>` if the file
is unreadable. So custom-port deployments work end-to-end. If you
add a new deep-link emission site here, route it through `daemon_url`
rather than hardcoding the URL — otherwise the port-override path
silently breaks.

The consumer is [app.js](../web-ui/dist/static/app.js)'s `boot()` handler;
the [web UI README](../web-ui/README.md#url-param-deep-links) lists the routes.
An unsigned-in visit stashes its destination in session storage and replays it
on a later signed-in boot. Browsers that deny session storage can lose that
destination. If you add a new deep-link here, update the contract there too.

## Implementation

The current app uses Python, PyObjC, and rumps. A Swift port is a design direction;
it is not part of the released installer.
