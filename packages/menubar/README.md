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

That button installs the daemon included inside the app; it does not need a
separate command-line installation or a Terminal `PATH`. On later launches the
app checks an existing service file. If it points at an older checkout or the
running daemon has an older version, the app rewrites the service file for the
current app. It restarts a running daemon and starts a repaired service that was
already down. In both cases it unloads any cached launchd job before loading the
rewritten service file. Settings, credentials, plugin state, and cursors remain
in place. If macOS refuses to unload the cached job, the app reports the failure
and does not claim the old process was upgraded.

The beta is signed, notarized, and built for Apple silicon. It includes its
Python runtime, Fulcra CLI, dashboard, and plugins, including Apple Notes, Apple Reminders, and Todoist. The 0.1.4 installer also includes the
[local folder and transcript plugins](../local-sources/README.md) and
[selected Notion pages](../notion/README.md). Notion live verification remains
pending. Notion currently uses an internal connection token and page
links, with preview on by default. Chrome
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

    uv run --all-packages --all-extras pytest packages/menubar/tests/ -q

The pure-model layer (daemon_client, model, polling, notifications)
runs everywhere — Linux CI included. The view layer (status_item,
popover, preferences) is exercised by manual smoke; see the checklist
below.

## Quick-record client (development)

The portable [request journal](fulcra_menubar/quick_record_requests.py) and
[client workflow](fulcra_menubar/quick_record_client.py) implement development
[protocol 2](../collect/README.md#quick-record-protocol-2-development).
They have no GUI, credential or network dependencies beyond the injected daemon
transport. Capture the full operation, globally unique request ID, account,
consent epoch, choice digest, time and note synchronously before acknowledging a
click or dispatching a worker. Use a **separate** private
`quick-record-client.sqlite`, never the server's action database.

A failed connection or lost response leaves that same request pending. Retrying
sends the saved body; a changed live account/epoch or replacement body fails
before socket IO. There is no automatic retry or implicit old-epoch consent.
The original note remains private in the saved request. Account-bound pending
pages include old-epoch items for explicit recovery and never return another
account's requests. Completed requests do not consume pending page limits.

Only a consistent protocol/account/epoch/action proof settles a record request.
Timer starts must prove the original timer/track/start; stop replies must prove
the original end time, consistent state and a stable server-assigned action ID.
A timer can have only one locally captured stop, even across separate request
IDs or app restarts. Recover it with `ClientRequestJournal.timer_stop` instead
of capturing a second stop. Once observed, the record ID and full datatype
remain fixed through pending-to-terminal response updates.
A submitted action remains pending. Verified, excluded and started outcomes are
distinct; a started timer is not a landed record. Terminal proofs are immutable.
Receipts retain bounded display metadata, including a nested timer allowlist,
without exception bodies or extra fields. Requests cap at 32 KiB, receipts at
4 KiB and pending pages at 100 rows (under 4 MiB of stored JSON).

The versioned daemon client explicitly bounds complete-choice responses at
8 MiB while requests and legacy replies remain at 64 KiB. Unknown, oversized or
unbound responses preserve the original pending request and fail visibly.
The development popover now uses this workflow by default. Socket work runs on
a background thread; a click captures its full request before starting that
worker. The view reloads account-scoped recent records and saved timers from the
daemon. Each timer is rendered by its own UUID and original start, including
unpinned/renamed tracks; failed stops stay visible with their first saved note
and window. Local pending requests have bounded cursor pages; **Find** separately
discovers saved server records and Undo beyond the recent-20 display.

Record checks use the same response-proof validation as initial requests. A
local saved intent is retried through its journal; other server reconciliation
uses the already observed action/timer target and window without fabricating a
new record body. Only a verified original offers Undo, and only verified native
deletion marks it undone. Submitted and excluded remain distinct.

Unsubmitted note/duration drafts are preserved by account/epoch/choice/timer
through rerenders; only a durably captured row clears its draft. Reopening or
refreshing while busy invalidates queued old-account results and schedules a new
handshake. The [view host](fulcra_menubar/_refresh_view.py) lazily loads its AppKit
subclass; plain `NSView` instances cannot hold the refresh callback. Dynamic rows
call [detach_view](fulcra_menubar/_objc_targets.py) before removal so old callback
targets and captured snapshots do not accumulate; foreign native targets remain
untouched.

Preferences and web settings use one complete scoped handshake and additive
revision-checked pin changes. Logical failures or changed-scope responses cannot
acknowledge a checkbox. Hidden pins survive; definition management is separate
and never treats a v1 Event UUID as an annotation-definition target. The web
client discards old pin responses after a newer account handshake. Reopening
cached Preferences or selecting its Annotations tab refreshes choices; a busy
load is invalidated and followed by a fresh handshake instead of dropped.

The client database has its own schema version 1. Missing tables, unversioned
development databases or future versions fail visibly without recreating empty
state; preserve the original file for explicit recovery. This does not migrate
or adopt the released app's unscoped favorites.

**Released 0.1.4 remains legacy until a reviewed packaged release. Explicit
old-epoch action/timer reauthorization, packaged interactive QA, and host cutover
remain release gates.** Current old-epoch pending work is visible for recovery
and cannot silently send under fresh consent. The actual AppKit popover and
preferences have rendered successfully against synthetic daemon responses;
that render does not prove signed-app permissions, real sources or cutover.

## Task plugins

The app bundles [Apple Reminders](../apple-reminders/README.md),
[Todoist](../todoist/README.md), and the shared task sync engine. It declares both macOS Reminders usage strings
and dispatches `--reminders-bridge` before importing the GUI. Native permission
is requested only from the setup wizard's **Allow access** button. Both plugins
have passed disposable-task completion checks against real Fulcra Files.

## Build the .app

The current beta bundle version is 0.1.4.

From the repository root on an Apple silicon Mac:

```sh
UV_PYTHON=3.13 bash packages/menubar/scripts/build_macos_app.sh
bash packages/menubar/scripts/verify_bundle.sh
```

The Briefcase build includes workspace wheels, Python, native command launchers,
and the web setup interface. Before bundled CLI, worker or GUI dispatch, the
entry point supplies its included `certifi` roots to OpenSSL when neither
`SSL_CERT_FILE` nor `SSL_CERT_DIR` is set. This lets the SDK's urllib-based
Auth0 flow verify HTTPS in the embedded runtime; certificate and hostname
verification stay enabled. An explicit operator trust store remains authoritative.
A missing bundled roots file fails before authentication. Source/venv launches
keep their existing Python trust configuration. It writes the app under
`packages/menubar/build/fulcra-menubar/macos/app/`. The build rejects embedded
builder-home paths and removes nonportable pip scripts before signing. It also
removes generated Python bytecode after build and bundle verification because
bytecode can retain a source path from the build machine. Dependency wheels can
include their upstream test suites, so the same sanitizer removes those files
while keeping runtime helpers such as `numpy.testing`. That currently trims
about 36 MiB from the arm64 app. A final read-only privacy check runs before
signing. The dock/Finder icon is the checked-in native `menubar-icon.icns`;
the smaller PNG beside it remains the monochrome menu-bar template. A release
asset test catches a missing native icon before Briefcase can substitute its
default.

For distribution, use `packages/menubar/scripts/release_dmg.sh` with an approved
Developer ID identity and a configured notary profile. It signs, notarizes,
staples, and checks the installer. The build also verifies a fresh copy of the
signed app, which catches invalid nested launcher signatures before the disk
image is made. If Apple rejects notarization or stapling, the script tries to
move the image to `*.NOT_NOTARIZED.dmg` so it cannot be mistaken for a release.
If that move fails, it deletes the canonical image; if the filesystem blocks
both operations, it exits with the exact unsafe path instead of claiming the
image was quarantined. An unnotarized development build is not the normal
installer; do not ask users to bypass Gatekeeper.

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

Bundle sanitization refuses test-only payloads whose leaf or any ancestor inside app_packages is a symlink, or whose resolved path escapes app_packages. This prevents pyarrow test pruning from deleting outside the bundle; check-only mode also fails closed.

The disk image includes [install instructions](resources/dmg/INSTALL.txt) for
all new sources. Notarization, stapling, and Gatekeeper acceptance are release
requirements; do not ask users to bypass Gatekeeper.
