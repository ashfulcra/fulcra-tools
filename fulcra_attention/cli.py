"""click CLI entry point."""
from __future__ import annotations

import json as _json
import os as _os
import secrets as _secrets
import shutil as _shutil
import socket as _socket
import stat as _stat
from pathlib import Path as _Path

import click

from . import service_manager
from . import state as state_mod
from .fulcra import FulcraClient, sanitize_tag_value


@click.group(help="Capture browsing attention into Fulcra.")
def cli() -> None:
    pass


@cli.command(help="Create the Attention DurationAnnotation def + attention/web tags (idempotent).")
def bootstrap() -> None:
    s = state_mod.load(state_mod.DEFAULT_PATH)
    client = FulcraClient()
    client.ensure_definitions(s)
    state_mod.save(s, state_mod.DEFAULT_PATH)
    click.echo(f"attention={s.attention_definition_id}")


def _relay_json_path() -> _Path:
    return _Path(
        _os.environ.get("FULCRA_ATTENTION_RELAY_JSON")
        or _os.path.expanduser("~/.config/fulcra-attention/relay.json")
    )


def _load_or_create_relay_json(port: int = 8771) -> dict:
    path = _relay_json_path()
    if path.exists():
        return _json.loads(path.read_text())
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {"bearer_token": _secrets.token_urlsafe(32), "port": port}
    path.write_text(_json.dumps(body, indent=2, sort_keys=True))
    _os.chmod(path, _stat.S_IRUSR | _stat.S_IWUSR)  # 0600
    return body


@cli.command(help="Generate bearer token and install the relay as a system service.")
@click.option(
    "--hostname",
    default=None,
    help="Override autodetected hostname for the `machine:<host>` tag.",
)
def setup(hostname: str | None) -> None:
    s = state_mod.load(state_mod.DEFAULT_PATH)
    if not s.attention_definition_id:
        raise click.ClickException(
            "Run `fulcra-attention bootstrap` first — no Attention definition exists."
        )
    detected = (hostname or _socket.gethostname()).strip().lower()
    # Strip ".local" / ".lan" / etc. so the tag stays portable across
    # networks, then sanitize so unusual hostnames don't trip Fulcra's
    # tag-name validation (e.g. underscores, accented chars).
    short = sanitize_tag_value(detected.split(".", 1)[0] or detected)
    if not short:
        raise click.ClickException(
            f"hostname {detected!r} sanitises to empty — pass --hostname"
        )
    client = FulcraClient()
    client.ensure_machine_tag(short, s)
    s.hostname = short
    state_mod.save(s, state_mod.DEFAULT_PATH)
    relay = _load_or_create_relay_json()
    exe = _shutil.which("fulcra-attention") or "fulcra-attention"
    path = service_manager.install(executable=exe)
    click.echo(f"Hostname:     {short} (tag: machine:{short})")
    click.echo(f"Bearer token: {relay['bearer_token']}")
    click.echo(f"Port:         {relay['port']}")
    click.echo(f"Service file: {path}")
    click.echo()
    click.echo("Paste the bearer token into the Chrome extension popup.")


@cli.command(help="Print the cached state.json contents.")
def status() -> None:
    s = state_mod.load(state_mod.DEFAULT_PATH)
    click.echo(_json.dumps(
        {
            "attention_definition_id": s.attention_definition_id,
            "hostname": s.hostname,
            "tag_ids": s.tag_ids,
            "watermarks": s.watermarks,
        },
        indent=2, sort_keys=True,
    ))


@cli.command(help="List every Attention definition on the account (for multi-machine cleanup).")
def defs() -> None:
    s = state_mod.load(state_mod.DEFAULT_PATH)
    client = FulcraClient()
    rows = client.list_attention_definitions()
    if not rows:
        click.echo("No Attention definitions on this account.")
        return
    # Oldest first so duplicates created by successive bootstraps read
    # in chronological order.
    rows.sort(key=lambda d: d.get("created_at") or "")
    for d in rows:
        flags = []
        if d.get("deleted_at"):
            flags.append("soft-deleted")
        if d.get("id") == s.attention_definition_id:
            flags.append("THIS MACHINE")
        suffix = f"  [{', '.join(flags)}]" if flags else ""
        click.echo(f"{d.get('id')}  created={d.get('created_at', '?')}{suffix}")


@cli.command(help="Point this machine's relay at a specific Attention definition id.")
@click.argument("definition_id")
def adopt(definition_id: str) -> None:
    # Local-only: rewrites state.json so this machine's relay forwards
    # events to an existing definition instead of its own. Used to merge
    # a machine onto another machine's definition. Copy the id from the
    # `defs` output. Does not touch Fulcra — restart the relay to apply.
    s = state_mod.load(state_mod.DEFAULT_PATH)
    s.attention_definition_id = definition_id
    state_mod.save(s, state_mod.DEFAULT_PATH)
    click.echo(f"adopted: {definition_id}")
    click.echo("Restart the relay for this to take effect.")


@cli.command(help="Soft-delete the Attention def + clear local state.")
@click.option("--confirm", is_flag=True,
              help="Required. Confirms you understand orphaned events stay visible.")
def reset(confirm: bool) -> None:
    if not confirm:
        raise click.UsageError(
            "Pass --confirm. This soft-deletes the Attention definition; "
            "previously-ingested events stay visible (Fulcra has no per-event delete)."
        )
    s = state_mod.load(state_mod.DEFAULT_PATH)
    client = FulcraClient()
    if s.attention_definition_id:
        client.soft_delete_definition(s.attention_definition_id)
        click.echo(f"soft-deleted: {s.attention_definition_id}")
        s.attention_definition_id = None
    s.watermarks = {}
    state_mod.save(s, state_mod.DEFAULT_PATH)


@cli.command(help="Foreground-run the relay (intended for launchd/systemd).")
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option("--port", default=None, type=int,
              help="Override port from ~/.config/fulcra-attention/relay.json.")
def relay(host: str, port: int | None) -> None:
    from .relay import ReceiverContext, make_server
    cfg = _load_or_create_relay_json()
    actual_port = port or cfg["port"]
    s = state_mod.load(state_mod.DEFAULT_PATH)
    if not s.attention_definition_id:
        raise click.ClickException("run `fulcra-attention bootstrap` first")
    client = FulcraClient()
    ctx = ReceiverContext(client=client, state=s, bearer_token=cfg["bearer_token"])
    server = make_server(host=host, port=actual_port, context=ctx)
    click.echo(f"listening on http://{host}:{actual_port}/attention")
    server.serve_forever()
