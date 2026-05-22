# fulcra-collect

The background hub for the Fulcra local helper tools. It discovers
helper *plugins*, schedules the periodic imports, supervises the
long-running services (the attention relay, the media webhook), stores
their credentials in the OS keychain, and exposes status and control
over a local socket.

This package is **sub-project 1**: the headless core. The menubar/tray
UI and the signed installer are later sub-projects.

## How it works

- Plugins are discovered via the `fulcra_collect.plugins` entry-point
  group — any installed package that registers there is found.
- Each plugin declares a kind: `service` (a long-lived server),
  `scheduled` (a periodic import), or `manual` (run on request).
- The `fulcra-collect daemon` process runs the scheduler and the service
  supervisor; each plugin run executes in an isolated worker subprocess.

## Usage

```bash
fulcra-collect install            # install the launchd/systemd agent
fulcra-collect daemon             # (or) run the hub in the foreground
fulcra-collect status             # every plugin: kind, enabled, last run
fulcra-collect enable lastfm      # enable a plugin
fulcra-collect set-credential lastfm api-key
fulcra-collect set-interval lastfm 1800
fulcra-collect run dayone         # trigger a manual plugin now
```

## Develop

```bash
uv sync --all-extras
uv run --package fulcra-collect pytest packages/collect
```
