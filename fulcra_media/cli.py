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
from .wizards.trakt import walkthrough as trakt_walkthrough
from .wizards.apple_podcasts import walkthrough as apple_podcasts_walkthrough
from .wizards.spotify import walkthrough as spotify_walkthrough
from .wizards.spotify_ifttt import walkthrough as spotify_ifttt_walkthrough
from .wizards.apple_takeout import walkthrough as apple_takeout_walkthrough

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
wizard.add_command(trakt_walkthrough, name="trakt")
wizard.add_command(apple_podcasts_walkthrough, name="apple-podcasts")
wizard.add_command(spotify_walkthrough, name="spotify")
wizard.add_command(spotify_ifttt_walkthrough, name="spotify-ifttt")
wizard.add_command(apple_takeout_walkthrough, name="apple-takeout")


@import_group.command("netflix")
@click.argument("path", type=str)
def import_netflix(path: str) -> None:
    """Import a Netflix slim-variant CSV (local path or fulcra:/... URI)."""
    resolved = library.resolve(path)
    s = state_mod.load(STATE_PATH)
    if not s.watched_definition_id:
        raise click.UsageError(
            "Run `fulcra-media bootstrap` first; need Watched definition."
        )
    events = list(netflix_importer.parse_auto(Path(resolved)))
    client = FulcraClient()
    client.ensure_tag("netflix", s)
    state_mod.save(s, STATE_PATH)
    result = client.run_import(events, s)
    state_mod.save(s, STATE_PATH)
    click.echo(
        f"netflix: total={result.total} skipped_existing={result.skipped_existing} "
        f"posted={result.posted} verified={result.verified}"
    )


@import_group.command("trakt")
@click.option("--cluster-threshold", default=5, type=int,
              help="Mark >=N items sharing watched_at as timestamp_confidence: low")
def import_trakt(cluster_threshold: int) -> None:
    """Import Trakt watch history via the Trakt API."""
    from .importers import trakt as trakt_importer
    s = state_mod.load(STATE_PATH)
    if not s.watched_definition_id:
        raise click.UsageError("Run `fulcra-media bootstrap` first.")
    items = list(trakt_importer.fetch_history())
    events = list(trakt_importer.normalize_history(items, cluster_threshold=cluster_threshold))
    client = FulcraClient()
    client.ensure_tag("trakt", s)
    state_mod.save(s, STATE_PATH)
    result = client.run_import(events, s)
    state_mod.save(s, STATE_PATH)
    click.echo(
        f"trakt: total={result.total} skipped_existing={result.skipped_existing} "
        f"posted={result.posted} verified={result.verified}"
    )


@import_group.command("apple-podcasts")
@click.option("--db", "db_path",
              default=None,
              help="Path to MTLibrary.sqlite (default: macOS standard location)")
def import_apple_podcasts(db_path: str | None) -> None:
    """Import Apple Podcasts listening history from the on-device SQLite DB."""
    from .importers import apple_podcasts as ap
    if db_path is None:
        db_path = str(ap.DEFAULT_DB_PATH)
    s = state_mod.load(STATE_PATH)
    if not s.listened_definition_id:
        raise click.UsageError("Run `fulcra-media bootstrap` first.")
    events = list(ap.parse_db(Path(db_path)))
    client = FulcraClient()
    client.ensure_tag("apple-podcasts", s)
    state_mod.save(s, STATE_PATH)
    result = client.run_import(events, s)
    state_mod.save(s, STATE_PATH)
    click.echo(
        f"apple-podcasts: total={result.total} skipped_existing={result.skipped_existing} "
        f"posted={result.posted} verified={result.verified}"
    )


@import_group.command("apple-podcasts-timemachine")
def import_apple_podcasts_timemachine() -> None:
    """Recover Apple Podcasts replay history by walking Time Machine snapshots.

    Each snapshot has its own ZLASTDATEPLAYED for each episode, so events
    that the live DB has overwritten resurface from older backups. Idempotency
    on (ZUUID, ZLASTDATEPLAYED) means duplicates across snapshots are skipped.
    """
    from .importers import apple_podcasts as ap
    s = state_mod.load(STATE_PATH)
    if not s.listened_definition_id:
        raise click.UsageError("Run `fulcra-media bootstrap` first.")
    snapshots = ap.find_timemachine_snapshots()
    if not snapshots:
        click.echo(
            "No Time Machine backups with Apple Podcasts data found. "
            "Run `tmutil listbackups` to verify backups are visible, "
            "and make sure your Time Machine destination is mounted.",
            err=True,
        )
        raise click.exceptions.Exit(1)
    click.echo(f"Walking {len(snapshots)} Time Machine snapshots...")
    all_events = []
    for snap in snapshots:
        click.echo(f"  {snap}")
        all_events.extend(ap.parse_db(snap))
    client = FulcraClient()
    client.ensure_tag("apple-podcasts", s)
    state_mod.save(s, STATE_PATH)
    result = client.run_import(all_events, s)
    state_mod.save(s, STATE_PATH)
    click.echo(
        f"apple-podcasts-timemachine: total={result.total} skipped_existing={result.skipped_existing} "
        f"posted={result.posted} verified={result.verified}"
    )


@import_group.command("spotify-extended")
@click.argument("path", type=str)
def import_spotify_extended(path: str) -> None:
    """Import Spotify Extended Streaming History from a GDPR-export zip."""
    from .importers import spotify as sp
    resolved = library.resolve(path)
    s = state_mod.load(STATE_PATH)
    if not s.listened_definition_id:
        raise click.UsageError("Run `fulcra-media bootstrap` first.")
    events = list(sp.parse_extended_zip(Path(resolved)))
    client = FulcraClient()
    client.ensure_tag("spotify", s)
    state_mod.save(s, STATE_PATH)
    result = client.run_import(events, s)
    state_mod.save(s, STATE_PATH)
    click.echo(
        f"spotify-extended: total={result.total} skipped_existing={result.skipped_existing} "
        f"posted={result.posted} verified={result.verified}"
    )


@import_group.command("spotify-ifttt")
@click.argument("path", type=str)
@click.option("--tz", "tz_name", default="UTC",
              help="IANA timezone IFTTT rendered the timestamps in (e.g. America/New_York)")
def import_spotify_ifttt(path: str, tz_name: str) -> None:
    """Import the legacy IFTTT->GDrive Spotify zip (multiple overlapping xlsx files).

    Use this only for backfilling pre-Extended-history plays — for ongoing
    capture, prefer `import spotify-extended` (full ms_played data).
    """
    from zoneinfo import ZoneInfo
    from .importers import spotify_ifttt as si
    resolved = library.resolve(path)
    s = state_mod.load(STATE_PATH)
    if not s.listened_definition_id:
        raise click.UsageError("Run `fulcra-media bootstrap` first.")
    try:
        tz = ZoneInfo(tz_name)
    except Exception as exc:
        raise click.UsageError(f"unknown timezone {tz_name!r}: {exc}") from exc
    events = list(si.parse_ifttt_zip(Path(resolved), tz=tz))
    client = FulcraClient()
    client.ensure_tag("spotify", s)
    state_mod.save(s, STATE_PATH)
    result = client.run_import(events, s)
    state_mod.save(s, STATE_PATH)
    click.echo(
        f"spotify-ifttt: total={result.total} skipped_existing={result.skipped_existing} "
        f"posted={result.posted} verified={result.verified}"
    )


@import_group.command("apple-takeout")
@click.argument("path", type=str)
def import_apple_takeout(path: str) -> None:
    """Import Apple Data & Privacy takeout — Apple TV Playback Activity CSV.

    Accepts the Playback Activity.csv file directly, or a path to the unzipped
    apple_data_export tree (we'll find the CSV inside).
    """
    from .importers import apple_takeout as at
    resolved = library.resolve(path)
    resolved_path = Path(resolved)
    if resolved_path.is_dir():
        # Find Playback Activity.csv inside the tree
        candidates = list(resolved_path.rglob("Playback Activity.csv"))
        if not candidates:
            raise click.UsageError(
                f"No 'Playback Activity.csv' found under {resolved_path}"
            )
        resolved_path = candidates[0]
    s = state_mod.load(STATE_PATH)
    if not s.watched_definition_id:
        raise click.UsageError("Run `fulcra-media bootstrap` first.")
    events = list(at.parse_playback_csv(resolved_path))
    client = FulcraClient()
    client.ensure_tag("apple-tv", s)
    state_mod.save(s, STATE_PATH)
    result = client.run_import(events, s)
    state_mod.save(s, STATE_PATH)
    click.echo(
        f"apple-takeout: total={result.total} skipped_existing={result.skipped_existing} "
        f"posted={result.posted} verified={result.verified}"
    )


if __name__ == "__main__":
    cli()
