"""click CLI entry point."""
from __future__ import annotations

import json as _json
import os as _os
import secrets as _secrets
import shutil as _shutil
import stat as _stat
from pathlib import Path as _Path

import click

from . import service_manager
from . import state as state_mod
from .fulcra import FulcraClient


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
def setup() -> None:
    s = state_mod.load(state_mod.DEFAULT_PATH)
    if not s.attention_definition_id:
        raise click.ClickException(
            "Run `fulcra-attention bootstrap` first — no Attention definition exists."
        )
    relay = _load_or_create_relay_json()
    exe = _shutil.which("fulcra-attention") or "fulcra-attention"
    path = service_manager.install(executable=exe)
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
            "tag_ids": s.tag_ids,
            "watermarks": s.watermarks,
        },
        indent=2, sort_keys=True,
    ))


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
