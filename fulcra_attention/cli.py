"""click CLI entry point."""
from __future__ import annotations

import click

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
