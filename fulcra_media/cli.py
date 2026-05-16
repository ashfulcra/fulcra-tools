"""Click entry point."""

from __future__ import annotations

import json
from pathlib import Path

import click

from . import library
from . import state as state_mod
from .fulcra import FulcraClient
from .importers import netflix as netflix_importer
from .wizards.netflix import walkthrough as netflix_walkthrough

STATE_PATH = state_mod.DEFAULT_PATH


@click.group(
    help="Import media consumption (Watched/Listened) into Fulcra.",
    invoke_without_command=True,
)
@click.pass_context
def cli(ctx: click.Context) -> None:
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@cli.command(help="Create the Watched/Listened annotation definitions and service tags.")
def bootstrap() -> None:
    s = state_mod.load(STATE_PATH)
    client = FulcraClient()
    client.ensure_definitions(s)
    state_mod.save(s, STATE_PATH)
    click.echo(f"watched={s.watched_definition_id} listened={s.listened_definition_id}")


@cli.command(help="Print the cached state.json contents.")
def status() -> None:
    s = state_mod.load(STATE_PATH)
    click.echo(json.dumps(
        {
            "watched_definition_id": s.watched_definition_id,
            "listened_definition_id": s.listened_definition_id,
            "tag_ids": s.tag_ids,
            "watermarks": s.watermarks,
            "state_path": str(STATE_PATH),
        },
        indent=2,
        sort_keys=True,
    ))


@cli.group(help="Interactive walkthroughs for requesting source data.")
def wizard() -> None:
    pass


@cli.group(help="Import data from a source.", name="import")
def import_group() -> None:
    pass


wizard.add_command(netflix_walkthrough, name="netflix")


@import_group.command("netflix")
@click.argument("path", type=str)
def import_netflix(path: str) -> None:
    """Import a Netflix slim-variant CSV (local path or fulcra:/... URI)."""
    resolved = library.resolve(path)
    s = state_mod.load(STATE_PATH)
    if not s.watched_definition_id or "netflix" not in s.tag_ids:
        raise click.UsageError(
            "Run `fulcra-media bootstrap` first; need Watched definition + netflix tag."
        )
    events = list(netflix_importer.parse_slim(Path(resolved)))
    client = FulcraClient()
    result = client.run_import(events, s)
    state_mod.save(s, STATE_PATH)
    click.echo(
        f"netflix: total={result.total} skipped_existing={result.skipped_existing} "
        f"posted={result.posted} verified={result.verified}"
    )


if __name__ == "__main__":
    cli()
