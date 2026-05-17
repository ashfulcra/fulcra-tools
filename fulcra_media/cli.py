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
from .wizards.ifttt import walkthrough as ifttt_walkthrough
from .wizards.pipedream import walkthrough as pipedream_walkthrough

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


@cli.command(help=(
    "Soft-delete Watched and Listened defs and clear local state. "
    "Events under the deleted defs stay visible in queries (Fulcra has no "
    "per-event delete); the next bootstrap creates new defs whose UUIDs "
    "naturally namespace fresh imports."
))
@click.option("--confirm", is_flag=True, required=False,
              help="Required. Confirms you understand orphaned events stay visible.")
@click.option("--keep-watched", is_flag=True, help="Only soft-delete the Listened def.")
@click.option("--keep-listened", is_flag=True, help="Only soft-delete the Watched def.")
def reset(confirm: bool, keep_watched: bool, keep_listened: bool) -> None:
    if not confirm:
        raise click.UsageError(
            "Pass --confirm. This soft-deletes the annotation definitions; "
            "previously-ingested events stay visible in queries (Fulcra has no "
            "per-event delete). To do a clean re-import, run `reset` then "
            "`bootstrap` — the new defs get fresh UUIDs that namespace future "
            "events apart from the orphaned ones."
        )
    s = state_mod.load(STATE_PATH)
    client = FulcraClient()
    deleted: list[str] = []
    if s.watched_definition_id and not keep_watched:
        if client.soft_delete_definition(s.watched_definition_id):
            deleted.append(f"watched={s.watched_definition_id}")
        s.watched_definition_id = None
    if s.listened_definition_id and not keep_listened:
        if client.soft_delete_definition(s.listened_definition_id):
            deleted.append(f"listened={s.listened_definition_id}")
        s.listened_definition_id = None
    # Watermarks are now meaningless; tag IDs survive (tags weren't deleted).
    s.watermarks = {}
    state_mod.save(s, STATE_PATH)
    click.echo("soft-deleted: " + (", ".join(deleted) or "(nothing — defs were absent)"))
    click.echo("state cleared. Run `bootstrap` to create fresh definitions.")


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
wizard.add_command(ifttt_walkthrough, name="ifttt")
wizard.add_command(pipedream_walkthrough, name="pipedream")


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
@click.option("--clusters", "cluster_spec", default=None, metavar="POLICY",
              help="Cluster handling: 'drop', 'sentinel:YYYY', 'keep', or 'ask'. "
                   "Default 'ask' on TTY, errors otherwise.")
def import_trakt(cluster_threshold: int, cluster_spec: str | None) -> None:
    """Import Trakt watch history via the Trakt API."""
    from fulcra_csv import apply_cluster_policy
    from .importers import trakt as trakt_importer
    s = state_mod.load(STATE_PATH)
    if not s.watched_definition_id:
        raise click.UsageError("Run `fulcra-media bootstrap` first.")
    items = list(trakt_importer.fetch_history())
    events = list(trakt_importer.normalize_history(items, cluster_threshold=cluster_threshold))

    policy = _resolve_cluster_policy(
        events, cluster_spec=cluster_spec,
        cluster_size_threshold=cluster_threshold,
    )
    if policy:
        before = len(events)
        events = apply_cluster_policy(events, policy)
        affected = before - len(events) if policy.action == "drop" else sum(
            1 for e in events if e.external_ids.get("sentinel_applied")
        )
        click.echo(f"cluster policy '{policy.action}': {affected} events affected")

    client = FulcraClient()
    client.ensure_tag("trakt", s)
    state_mod.save(s, STATE_PATH)
    result = client.run_import(events, s)
    state_mod.save(s, STATE_PATH)
    click.echo(
        f"trakt: total={result.total} skipped_existing={result.skipped_existing} "
        f"posted={result.posted} verified={result.verified}"
    )


def _resolve_cluster_policy(
    events: list,
    *,
    cluster_spec: str | None,
    cluster_size_threshold: int,
):
    """Resolve --clusters into a ClusterPolicy. None means no cluster preprocessing.

    Modes:
      'drop'             — drop all cluster members
      'sentinel:YYYY'    — shift cluster members to Jan 1, YYYY
      'keep'             — leave at original timestamps
      'ask' (or None on TTY) — interactive prompt
    """
    from fulcra_csv import ClusterPolicy, cluster_size_of

    cluster_count = sum(
        1 for e in events if cluster_size_of(e) >= cluster_size_threshold
    )
    if cluster_count == 0:
        return None  # nothing to do

    # Compact summary of detected clusters for the user
    from collections import Counter
    cluster_dates = Counter(
        e.start_time.date().isoformat() for e in events
        if cluster_size_of(e) >= cluster_size_threshold
    )
    summary = ", ".join(f"{d} ({n})" for d, n in cluster_dates.most_common(4))
    if len(cluster_dates) > 4:
        summary += f", and {len(cluster_dates) - 4} more dates"

    spec = cluster_spec
    if spec is None:
        if not click.get_text_stream("stdin").isatty():
            raise click.UsageError(
                f"Detected {cluster_count} cluster events on {len(cluster_dates)} dates "
                f"(largest: {summary}). Pass --clusters drop|sentinel:YYYY|keep "
                "to handle them non-interactively."
            )
        spec = "ask"

    if spec == "ask":
        click.echo(
            f"\nDetected {cluster_count} events flagged as cluster members "
            f"(timestamp_confidence: low, ≥{cluster_size_threshold} sharing one watched_at)."
        )
        click.echo(f"Dates: {summary}\n")
        click.echo("These are typically signup-day backfill artifacts with synthetic")
        click.echo("timestamps. Three handling options:")
        click.echo("  drop      — discard them entirely")
        click.echo("  sentinel  — keep them but shift to a date far in the past (e.g. 2015)")
        click.echo("  keep      — leave at original (low-confidence) timestamps")
        choice = click.prompt(
            "Choice", type=click.Choice(["drop", "sentinel", "keep"]),
            default="sentinel",
        )
        if choice == "sentinel":
            year = click.prompt("Sentinel year", type=int, default=2015)
            return ClusterPolicy(
                action="sentinel", sentinel_year=year,
                cluster_size_threshold=cluster_size_threshold,
            )
        return ClusterPolicy(action=choice, cluster_size_threshold=cluster_size_threshold)

    # Non-interactive parse
    if spec.startswith("sentinel:"):
        try:
            year = int(spec.split(":", 1)[1])
        except ValueError as exc:
            raise click.UsageError(f"--clusters sentinel:YYYY needs a year: {spec!r}") from exc
        return ClusterPolicy(
            action="sentinel", sentinel_year=year,
            cluster_size_threshold=cluster_size_threshold,
        )
    if spec in ("drop", "keep"):
        return ClusterPolicy(action=spec, cluster_size_threshold=cluster_size_threshold)
    raise click.UsageError(
        f"--clusters must be 'drop', 'sentinel:YYYY', 'keep', or 'ask', got {spec!r}"
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


@import_group.command("generic-csv")
@click.argument("path", type=str)
@click.option("--service", required=True, help="Service tag (e.g. spotify, netflix, youtube)")
@click.option("--category", type=click.Choice(["watched", "listened"]), required=True)
@click.option("--ts-col", default="timestamp", show_default=True)
@click.option("--title-col", default="title", show_default=True)
@click.option("--subtitle-col", default="artist", show_default=True,
              help="Subtitle column (artist for music, show for podcasts/tv)")
@click.option("--id-col", "id_col", default="id", show_default=True,
              help="Optional per-content id column — included in the hash, not used verbatim")
@click.option("--duration-col", default=None,
              help="Optional duration (seconds) column; else 1s sentinel")
@click.option("--end-col", default=None, help="Optional explicit end_time column")
@click.option("--confidence", type=click.Choice(["high", "medium", "low"]), default="medium")
@click.option("--tz", "tz_name", default="UTC")
@click.option("--fingerprint",
              type=click.Choice(["auto", "music", "movie", "tv", "podcast", "none"]),
              default="auto",
              help="content_fingerprint kind (auto picks music/movie from --category)")
def import_generic_csv(
    path: str, service: str, category: str,
    ts_col: str, title_col: str, subtitle_col: str, id_col: str,
    duration_col: str | None, end_col: str | None,
    confidence: str, tz_name: str, fingerprint: str,
) -> None:
    """Import an arbitrary CSV (IFTTT, Pipedream, manual export) as Watched/Listened."""
    from fulcra_csv import ColumnMap
    from .importers.generic_csv import parse_media_csv

    resolved = library.resolve(path)
    s = state_mod.load(STATE_PATH)
    target_def = (
        s.watched_definition_id if category == "watched" else s.listened_definition_id
    )
    if not target_def:
        raise click.UsageError(f"Run `fulcra-media bootstrap` first; need {category} definition.")

    cm = ColumnMap(
        timestamp=ts_col,
        title=title_col,
        subtitle=subtitle_col or None,
        source_id=id_col or None,
        duration_seconds=duration_col,
        end_time=end_col,
    )
    if tz_name == "UTC":
        from datetime import timezone as _tz
        tz = _tz.utc
    else:
        from zoneinfo import ZoneInfo
        try:
            tz = ZoneInfo(tz_name)
        except Exception as exc:
            raise click.UsageError(f"unknown timezone {tz_name!r}: {exc}") from exc

    fp_kind = None if fingerprint == "none" else (None if fingerprint == "auto" else fingerprint)
    from .importers.generic_csv import _FP_AUTO
    fp_arg = _FP_AUTO if fingerprint == "auto" else fp_kind

    events = list(parse_media_csv(
        Path(resolved),
        service=service, category=category,
        column_map=cm, tz=tz, confidence=confidence,
        fingerprint_kind=fp_arg,
    ))
    client = FulcraClient()
    client.ensure_tag(service, s)
    state_mod.save(s, STATE_PATH)
    result = client.run_import(events, s)
    state_mod.save(s, STATE_PATH)
    click.echo(
        f"generic-csv ({service}/{category}): total={result.total} "
        f"skipped_existing={result.skipped_existing} "
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
