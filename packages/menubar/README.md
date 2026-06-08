# fulcra-menubar

macOS menubar UI for `fulcra-collect`. Python + PyObjC + rumps v1; a
Swift rewrite follows once the UX is locked.

> **First time here?** See [docs/TESTING.md](../../docs/TESTING.md) for
> the end-to-end walkthrough: install, start the daemon, paste your
> Fulcra token, and walk Trakt onboarding step by step.

## Run in dev mode

    cd path/to/fulcra-tools
    uv sync --extra macos --package fulcra-menubar
    uv run --package fulcra-menubar python -m fulcra_menubar

The daemon must be running (`fulcra-collect install` to set up the
launchd/systemd user agent, or `fulcra-collect daemon` in a foreground
terminal for dev). The
menubar icon appears in the top-right of the screen; click for the
rumps menu, then "Open Fulcra Collect" for the popover.

## Tests

    uv run pytest packages/menubar/tests/ -q

The pure-model layer (daemon_client, model, polling, notifications)
runs everywhere — Linux CI included. The view layer (status_item,
popover, preferences) is exercised by manual smoke; see the checklist
below.

## Build the .app

    uv sync --extra macos --extra build --package fulcra-menubar
    cd packages/menubar
    uv run python setup.py py2app -A      # alias build for dev
    uv run python setup.py py2app         # distributable build

The unsigned `.app` lands in `packages/menubar/dist/Fulcra Collect.app`.
The first launch will trip Gatekeeper (right-click → Open to bypass).
Code-signing and notarization land in sub-project 3.

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

The consumer side lives in `packages/web-ui/dist/static/app.js`'s
`boot()` handler — see that package's README ("URL-param deep-links")
for the full list of supported routes and the auth-gate caveat (the
web UI must be signed in for the deep-link to land on the requested
route; unauth users see signin first and lose the param). If you add
a new deep-link here, update the contract there too.

## Path to Swift

Per the spec, this Python build is the UX laboratory. Once the
popover layout, Preferences structure, notification triggers, palette,
bootstrap copy, and icon assets are locked, the Swift port begins as
sub-project 2.5. The Python file boundaries were chosen to map 1:1 to
Swift files — see the spec's "UX lock and the Swift handoff" section.
