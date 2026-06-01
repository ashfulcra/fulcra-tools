"""The `fulcra-collect` command-line interface.

`daemon` and `install` act locally; the rest talk to a running daemon
over the control socket. `_worker` is the internal worker entrypoint.
"""
from __future__ import annotations

import logging
import os
import shutil
import sys

import click

from . import config as config_mod
from . import credentials, worker
from .control import send_request
from .daemon import Daemon


def _socket_path():
    return config_mod.config_dir() / "control.sock"


def _configure_logging() -> None:
    """Install a single stderr handler on the root logger at INFO.

    Without this the daemon ran with no root handler, so INFO records
    (the ``web UI: ...`` startup line, anything below WARNING) hit
    Python's last-resort handler and were dropped — ``daemon.out.log``
    stayed empty even while the server answered requests, which hid live
    401s. launchd captures stderr to ``~/Library/Logs/fulcra-collect/``,
    so a stderr handler is what surfaces in the log files.

    Level is overridable via ``FULCRA_COLLECT_LOG_LEVEL`` (e.g. DEBUG).
    Idempotent: repeated calls re-use the handler we tagged rather than
    stacking duplicates, so restarting the server in-process stays clean.
    """
    level_name = os.environ.get("FULCRA_COLLECT_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    root = logging.getLogger()
    root.setLevel(level)
    for handler in root.handlers:
        if getattr(handler, "_fulcra_collect", False):
            handler.setLevel(level)
            return
    handler = logging.StreamHandler(sys.stderr)
    handler.setLevel(level)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    )
    handler._fulcra_collect = True  # type: ignore[attr-defined]
    root.addHandler(handler)


@click.group()
def cli() -> None:
    """Background hub for the Fulcra local helpers."""


@cli.command()
def daemon() -> None:
    """Run the hub core in the foreground (the launchd/systemd entrypoint)."""
    _configure_logging()
    Daemon().serve()


@cli.command()
def install() -> None:
    """Install the launchd/systemd user agent for the daemon."""
    from . import service_manager
    exe = shutil.which("fulcra-collect") or "fulcra-collect"
    path = service_manager.install(executable=exe)
    click.echo(f"installed service file: {path}")


@cli.command()
def status() -> None:
    """Show every plugin's kind, enabled flag, and last run."""
    try:
        reply = send_request(_socket_path(), {"cmd": "status"})
    except ConnectionError:
        raise click.ClickException(
            "fulcra-collect daemon is not running — start it with "
            "`fulcra-collect daemon` or install it with `fulcra-collect install`."
        )
    for p in reply["plugins"]:
        flag = "on " if p["enabled"] else "off"
        last = p["last_outcome"] or "never run"
        click.echo(f"  [{flag}] {p['id']:<20} {p['kind']:<10} {last}")
    for name, err in reply.get("load_errors", {}).items():
        click.echo(f"  load error: {name}: {err}", err=True)


@cli.command()
@click.argument("plugin_id")
def run(plugin_id: str) -> None:
    """Trigger one run of a plugin now."""
    reply = send_request(_socket_path(), {"cmd": "run", "plugin": plugin_id})
    if not reply.get("ok"):
        raise click.ClickException(reply.get("error", "run failed"))
    click.echo(f"triggered: {plugin_id}")


def _toggle(plugin_id: str, *, on: bool) -> None:
    cfg = config_mod.load()
    cfg.enable(plugin_id) if on else cfg.disable(plugin_id)
    config_mod.save(cfg)
    try:
        send_request(_socket_path(), {"cmd": "reload"})
    except ConnectionError:
        pass  # daemon not running — config is saved; it'll read it on next start


@cli.command()
@click.argument("plugin_id")
def enable(plugin_id: str) -> None:
    """Enable a plugin."""
    _toggle(plugin_id, on=True)
    click.echo(f"enabled: {plugin_id}")


@cli.command()
@click.argument("plugin_id")
def disable(plugin_id: str) -> None:
    """Disable a plugin."""
    _toggle(plugin_id, on=False)
    click.echo(f"disabled: {plugin_id}")


@cli.command(name="set-interval")
@click.argument("plugin_id")
@click.argument("seconds", type=int)
def set_interval(plugin_id: str, seconds: int) -> None:
    """Override a scheduled plugin's cadence (in seconds)."""
    cfg = config_mod.load()
    cfg.set_interval(plugin_id, seconds)
    config_mod.save(cfg)
    try:
        send_request(_socket_path(), {"cmd": "reload"})
    except ConnectionError:
        pass
    click.echo(f"{plugin_id}: interval set to {seconds}s")


@cli.command(name="set-credential")
@click.argument("plugin_id")
@click.argument("key")
def set_credential(plugin_id: str, key: str) -> None:
    """Store a plugin secret in the OS keychain (prompts, hidden input)."""
    value = click.prompt(f"{plugin_id}/{key}", hide_input=True)
    credentials.set_secret(plugin_id, key, value)
    click.echo(f"stored {plugin_id}/{key}")


@cli.command(name="_worker", hidden=True)
@click.argument("plugin_id")
def _worker(plugin_id: str) -> None:
    """Internal — the worker-subprocess entrypoint."""
    # Same logging setup as the daemon: the worker is a separate process,
    # so without this its INFO records would hit the no-handler last-resort
    # path and vanish — the same gap the daemon fix closes.
    _configure_logging()
    sys.exit(worker.main([plugin_id]))


@cli.group()
def plugin() -> None:
    """Per-plugin state and configuration commands."""


@plugin.command("reset-definition")
@click.argument("plugin_id")
def reset_definition(plugin_id: str) -> None:
    """Clear the cached Fulcra definition id for a plugin so the next
    run re-resolves (and possibly adopts a different definition)."""
    from . import state
    st = state.load(plugin_id)
    st.definition_id = None
    state.save(st)
    click.echo(f"Cleared definition_id cache for {plugin_id!r}.")
