"""The `fulcra-collect` command-line interface.

`daemon` and `install` act locally; the rest talk to a running daemon
over the control socket. `_worker` is the internal worker entrypoint.
"""
from __future__ import annotations

import shutil
import sys

import click

from . import config as config_mod
from . import credentials, worker
from .control import send_request
from .daemon import Daemon


def _socket_path():
    return config_mod.config_dir() / "control.sock"


@click.group()
def cli() -> None:
    """Background hub for the Fulcra local helpers."""


@cli.command()
def daemon() -> None:
    """Run the hub core in the foreground (the launchd/systemd entrypoint)."""
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
    sys.exit(worker.main([plugin_id]))
