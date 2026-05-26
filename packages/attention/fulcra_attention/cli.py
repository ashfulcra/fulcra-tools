"""click CLI entry point.

NOTE: the standalone HTTP relay (port 8771) has been removed; browser
extension events now go to the fulcra-collect daemon's
`/api/extension/attention` endpoint on its stable port. This CLI keeps
the bootstrap / setup / status / defs / adopt / reset commands for
multi-machine wrangling, but no longer runs a relay process of its own.
"""
from __future__ import annotations

import json as _json
import socket as _socket

import click

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


@cli.command(help="Register this machine's hostname tag (machine:<host>).")
@click.option(
    "--hostname",
    default=None,
    help="Override autodetected hostname for the `machine:<host>` tag.",
)
def setup(hostname: str | None) -> None:
    """Per-machine setup: registers a `machine:<host>` tag so events
    can be filtered by machine. No longer installs a launchd/systemd
    unit (the daemon owns the HTTP listener now); no longer manages a
    bearer token (set the extension-token via the Fulcra Collect web UI
    instead).
    """
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
    click.echo(f"Hostname: {short} (tag: machine:{short})")
    click.echo()
    click.echo(
        "Set an extension-token in the Fulcra Collect web UI "
        "(Attention plugin setup) and paste the same value into the "
        "browser extension's options page.",
    )


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


@cli.command(help="Point this machine's daemon at a specific Attention definition id.")
@click.argument("definition_id")
def adopt(definition_id: str) -> None:
    # Local-only: rewrites state.json so this machine's daemon forwards
    # events to an existing definition instead of its own. Used to merge
    # a machine onto another machine's definition. Copy the id from the
    # `defs` output. Does not touch Fulcra — restart the daemon to apply.
    s = state_mod.load(state_mod.DEFAULT_PATH)
    s.attention_definition_id = definition_id
    state_mod.save(s, state_mod.DEFAULT_PATH)
    click.echo(f"adopted: {definition_id}")
    click.echo("Restart the daemon for this to take effect.")


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
