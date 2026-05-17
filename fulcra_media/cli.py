"""Click entry point."""

from __future__ import annotations

import json
from pathlib import Path

import click
import httpx

from . import library
from . import state as state_mod
from .cli_common import safe_exc_message
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
from .wizards.lastfm import walkthrough as lastfm_walkthrough
from .wizards.deezer import walkthrough as deezer_walkthrough
from .wizards.letterboxd import walkthrough as letterboxd_walkthrough
from .wizards.goodreads import walkthrough as goodreads_walkthrough
from .wizards.strava import walkthrough as strava_walkthrough
from .setup_wizard import setup as setup_command

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
@click.option("--keep-watched", is_flag=True, help="Don't soft-delete the Watched def.")
@click.option("--keep-listened", is_flag=True, help="Don't soft-delete the Listened def.")
@click.option("--keep-activity", is_flag=True, help="Don't soft-delete the Activity def.")
@click.option("--keep-read", is_flag=True, help="Don't soft-delete the Read def.")
def reset(confirm: bool, keep_watched: bool, keep_listened: bool,
          keep_activity: bool, keep_read: bool) -> None:
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
    if s.activity_definition_id and not keep_activity:
        if client.soft_delete_definition(s.activity_definition_id):
            deleted.append(f"activity={s.activity_definition_id}")
        s.activity_definition_id = None
    if s.read_definition_id and not keep_read:
        if client.soft_delete_definition(s.read_definition_id):
            deleted.append(f"read={s.read_definition_id}")
        s.read_definition_id = None
    # Watermarks are now meaningless; tag IDs survive (tags weren't deleted).
    s.watermarks = {}
    state_mod.save(s, STATE_PATH)
    from . import twin_cache
    twin_cache.clear()
    click.echo("soft-deleted: " + (", ".join(deleted) or "(nothing — defs were absent)"))
    click.echo("state cleared (including twin cache). Run `bootstrap` to create fresh definitions.")


cli.add_command(setup_command, name="setup")


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
wizard.add_command(lastfm_walkthrough, name="lastfm")
wizard.add_command(deezer_walkthrough, name="deezer")
wizard.add_command(letterboxd_walkthrough, name="letterboxd")
wizard.add_command(goodreads_walkthrough, name="goodreads")
wizard.add_command(strava_walkthrough, name="strava")


@import_group.command("netflix")
@click.argument("path", type=str)
@click.option("--check-only", is_flag=True, help="Don't post; report what would be posted.")
@click.option("--json", "json_mode", is_flag=True, help="Single-line JSON output.")
def import_netflix(path: str, check_only: bool, json_mode: bool) -> None:
    """Import a Netflix slim-variant CSV (local path or fulcra:/... URI)."""
    from .cli_common import emit_result, ImportEnvelope, run_and_emit, resolve_or_emit
    resolved = resolve_or_emit("netflix", path, json_mode=json_mode)
    if resolved is None:
        return
    s = state_mod.load(STATE_PATH)
    if not s.watched_definition_id:
        emit_result(
            ImportEnvelope(
                importer="netflix", ok=False,
                errors=[{"stage": "setup", "message": "Run `fulcra-media bootstrap` first."}],
            ),
            json_mode=json_mode,
        )
        return
    events = list(netflix_importer.parse_auto(resolved))
    run_and_emit("netflix", events, s,
                 tag_name="netflix", check_only=check_only, json_mode=json_mode)


@import_group.command("trakt")
@click.option("--cluster-threshold", default=5, type=int,
              help="Mark >=N items sharing watched_at as timestamp_confidence: low")
@click.option("--clusters", "cluster_spec", default=None, metavar="POLICY",
              help="Cluster handling: 'drop', 'sentinel:YYYY', 'keep', or 'ask'. "
                   "Default 'ask' on TTY, errors otherwise.")
@click.option("--check-only", is_flag=True)
@click.option("--json", "json_mode", is_flag=True)
@click.option("--twin-policy",
              type=click.Choice(["ask", "auto-discard", "keep"]),
              default=None,
              help="When a Trakt low-conf event has a high-conf twin in the local "
                   "cache (same content_fingerprint, previously imported): "
                   "ask (TTY default), auto-discard, or keep. "
                   "Errors non-interactively without an explicit choice.")
def import_trakt(cluster_threshold: int, cluster_spec: str | None,
                 check_only: bool, json_mode: bool,
                 twin_policy: str | None) -> None:
    """Import Trakt watch history via the Trakt API."""
    from fulcra_csv import apply_cluster_policy, apply_twin_decisions, find_low_conf_twins
    from . import twin_cache
    from .cli_common import emit_result, ImportEnvelope, run_and_emit
    from .importers import trakt as trakt_importer
    s = state_mod.load(STATE_PATH)
    if not s.watched_definition_id:
        emit_result(
            ImportEnvelope(importer="trakt", ok=False,
                           errors=[{"stage": "setup", "message": "Run bootstrap first."}]),
            json_mode=json_mode,
        )
        return
    try:
        items = list(trakt_importer.fetch_history())
    except (RuntimeError, httpx.HTTPError) as exc:
        emit_result(
            ImportEnvelope(importer="trakt", ok=False,
                           errors=[{"stage": "fetch", "message": safe_exc_message(exc)}]),
            json_mode=json_mode,
        )
        return
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
        if not json_mode:
            click.echo(f"cluster policy '{policy.action}': {affected} events affected", err=True)

    # Cross-batch twin dedup: low-conf events whose content_fingerprint matches
    # a previously-imported high-conf event in the local twin cache.
    events = _maybe_apply_twin_dedup(
        events, twin_policy=twin_policy, json_mode=json_mode,
    )

    run_and_emit("trakt", events, s,
                 tag_name="trakt", check_only=check_only, json_mode=json_mode)


def _maybe_apply_twin_dedup(events: list,
                            *, twin_policy: str | None,
                            json_mode: bool) -> list:
    """Look for low-conf events whose content_fingerprint matches a high-conf
    entry in the local twin cache, then apply the user's chosen policy."""
    from fulcra_csv import apply_twin_decisions, find_low_conf_twins
    from . import twin_cache

    cached = twin_cache.load_for_twin_lookup()
    pairs = find_low_conf_twins(events, extra_pool=cached)
    if not pairs:
        return events

    # Resolve the policy
    spec = twin_policy
    if spec is None:
        if json_mode or not click.get_text_stream("stdin").isatty():
            click.echo(
                f"warning: {len(pairs)} low-conf events have high-conf twins in the "
                "twin cache; pass --twin-policy ask|auto-discard|keep to handle them.",
                err=True,
            )
            return events
        spec = "ask"

    if spec == "keep":
        return events

    if spec == "auto-discard":
        to_drop = {twin_cache._source_id_of(low) for low, _high in pairs}
        return apply_twin_decisions(events, to_drop)

    # ask
    click.echo(
        f"\nDetected {len(pairs)} low-confidence events with high-confidence "
        f"twins from previous imports.", err=True,
    )
    for i, (low, high) in enumerate(pairs[:5], start=1):
        fp = low.external_ids.get("content_fingerprint", "?")
        click.echo(
            f"  {i}. {fp}  (low-conf {low.start_time.isoformat()} ↔ "
            f"high-conf source {high.external_ids.get('importer', '?')})",
            err=True,
        )
    if len(pairs) > 5:
        click.echo(f"  ... and {len(pairs) - 5} more", err=True)

    choice = click.prompt(
        "Discard the low-confidence twins?",
        type=click.Choice(["yes", "no"]), default="yes",
    )
    if choice == "yes":
        to_drop = {twin_cache._source_id_of(low) for low, _high in pairs}
        return apply_twin_decisions(events, to_drop)
    return events


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
@click.option("--check-only", is_flag=True)
@click.option("--json", "json_mode", is_flag=True)
def import_apple_podcasts(db_path: str | None, check_only: bool, json_mode: bool) -> None:
    """Import Apple Podcasts listening history from the on-device SQLite DB."""
    from .cli_common import emit_result, ImportEnvelope, run_and_emit
    from .importers import apple_podcasts as ap
    if db_path is None:
        db_path = str(ap.DEFAULT_DB_PATH)
    s = state_mod.load(STATE_PATH)
    if not s.listened_definition_id:
        emit_result(
            ImportEnvelope(importer="apple-podcasts", ok=False,
                           errors=[{"stage": "setup", "message": "Run bootstrap first."}]),
            json_mode=json_mode,
        )
        return
    events = list(ap.parse_db(Path(db_path)))
    run_and_emit("apple-podcasts", events, s,
                 tag_name="apple-podcasts", check_only=check_only, json_mode=json_mode)


@import_group.command("apple-podcasts-timemachine")
@click.option("--check-only", is_flag=True)
@click.option("--json", "json_mode", is_flag=True)
def import_apple_podcasts_timemachine(check_only: bool, json_mode: bool) -> None:
    """Recover Apple Podcasts replay history by walking Time Machine snapshots."""
    from .cli_common import emit_result, ImportEnvelope, run_and_emit
    from .importers import apple_podcasts as ap
    s = state_mod.load(STATE_PATH)
    if not s.listened_definition_id:
        emit_result(
            ImportEnvelope(importer="apple-podcasts-timemachine", ok=False,
                           errors=[{"stage": "setup", "message": "Run bootstrap first."}]),
            json_mode=json_mode,
        )
        return
    snapshots = ap.find_timemachine_snapshots()
    if not snapshots:
        emit_result(
            ImportEnvelope(
                importer="apple-podcasts-timemachine", ok=False,
                errors=[{"stage": "fetch",
                         "message": "No Time Machine backups with Apple Podcasts data found. "
                                    "Verify tmutil listbackups + Time Machine mount + FDA."}],
            ),
            json_mode=json_mode,
        )
        return
    if not json_mode:
        click.echo(f"Walking {len(snapshots)} Time Machine snapshots...", err=True)
    all_events = []
    for snap in snapshots:
        if not json_mode:
            click.echo(f"  {snap}", err=True)
        all_events.extend(ap.parse_db(snap))
    run_and_emit("apple-podcasts-timemachine", all_events, s,
                 tag_name="apple-podcasts", check_only=check_only, json_mode=json_mode)


@import_group.command("spotify-extended")
@click.argument("path", type=str)
@click.option("--check-only", is_flag=True)
@click.option("--json", "json_mode", is_flag=True)
def import_spotify_extended(path: str, check_only: bool, json_mode: bool) -> None:
    """Import Spotify Extended Streaming History from a GDPR-export zip."""
    from .cli_common import emit_result, ImportEnvelope, run_and_emit, resolve_or_emit
    from .importers import spotify as sp
    resolved = resolve_or_emit("spotify-extended", path, json_mode=json_mode)
    if resolved is None:
        return
    s = state_mod.load(STATE_PATH)
    if not s.listened_definition_id:
        emit_result(
            ImportEnvelope(importer="spotify-extended", ok=False,
                           errors=[{"stage": "setup", "message": "Run bootstrap first."}]),
            json_mode=json_mode,
        )
        return
    events = list(sp.parse_extended_zip(resolved))
    run_and_emit("spotify-extended", events, s,
                 tag_name="spotify", check_only=check_only, json_mode=json_mode)


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
@click.option("--check-only", is_flag=True)
@click.option("--json", "json_mode", is_flag=True)
def import_generic_csv(
    path: str, service: str, category: str,
    ts_col: str, title_col: str, subtitle_col: str, id_col: str,
    duration_col: str | None, end_col: str | None,
    confidence: str, tz_name: str, fingerprint: str,
    check_only: bool, json_mode: bool,
) -> None:
    """Import an arbitrary CSV (IFTTT, Pipedream, manual export) as Watched/Listened."""
    from fulcra_csv import ColumnMap
    from .cli_common import emit_result, ImportEnvelope, run_and_emit, resolve_or_emit
    from .importers.generic_csv import parse_media_csv

    resolved = resolve_or_emit(f"generic-csv:{service}", path, json_mode=json_mode)
    if resolved is None:
        return
    s = state_mod.load(STATE_PATH)
    target_def = (
        s.watched_definition_id if category == "watched" else s.listened_definition_id
    )
    if not target_def:
        emit_result(
            ImportEnvelope(importer=f"generic-csv:{service}", ok=False,
                           errors=[{"stage": "setup",
                                    "message": f"Run bootstrap first; need {category} definition."}]),
            json_mode=json_mode,
        )
        return

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
            emit_result(
                ImportEnvelope(importer=f"generic-csv:{service}", ok=False,
                               errors=[{"stage": "args",
                                        "message": f"unknown timezone {tz_name!r}: {exc}"}]),
                json_mode=json_mode,
            )
            return

    fp_kind = None if fingerprint == "none" else (None if fingerprint == "auto" else fingerprint)
    from .importers.generic_csv import _FP_AUTO
    fp_arg = _FP_AUTO if fingerprint == "auto" else fp_kind

    events = list(parse_media_csv(
        resolved,
        service=service, category=category,
        column_map=cm, tz=tz, confidence=confidence,
        fingerprint_kind=fp_arg,
    ))
    run_and_emit(f"generic-csv:{service}", events, s,
                 tag_name=service, check_only=check_only, json_mode=json_mode)


@import_group.command("spotify-ifttt")
@click.argument("path", type=str)
@click.option("--tz", "tz_name", default="UTC",
              help="IANA timezone IFTTT rendered the timestamps in (e.g. America/New_York)")
@click.option("--check-only", is_flag=True)
@click.option("--json", "json_mode", is_flag=True)
def import_spotify_ifttt(path: str, tz_name: str, check_only: bool, json_mode: bool) -> None:
    """Import the legacy IFTTT->GDrive Spotify zip (multiple overlapping xlsx files).

    Use this only for backfilling pre-Extended-history plays — for ongoing
    capture, prefer `import spotify-extended` (full ms_played data).
    """
    from zoneinfo import ZoneInfo
    from .cli_common import emit_result, ImportEnvelope, run_and_emit, resolve_or_emit
    from .importers import spotify_ifttt as si
    resolved = resolve_or_emit("spotify-ifttt", path, json_mode=json_mode)
    if resolved is None:
        return
    s = state_mod.load(STATE_PATH)
    if not s.listened_definition_id:
        emit_result(
            ImportEnvelope(importer="spotify-ifttt", ok=False,
                           errors=[{"stage": "setup", "message": "Run bootstrap first."}]),
            json_mode=json_mode,
        )
        return
    try:
        tz = ZoneInfo(tz_name)
    except Exception as exc:
        emit_result(
            ImportEnvelope(importer="spotify-ifttt", ok=False,
                           errors=[{"stage": "args", "message": f"unknown timezone {tz_name!r}: {exc}"}]),
            json_mode=json_mode,
        )
        return
    events = list(si.parse_ifttt_zip(resolved, tz=tz))
    run_and_emit("spotify-ifttt", events, s,
                 tag_name="spotify", check_only=check_only, json_mode=json_mode)


@import_group.command("apple-takeout")
@click.argument("path", type=str)
@click.option("--check-only", is_flag=True)
@click.option("--json", "json_mode", is_flag=True)
def import_apple_takeout(path: str, check_only: bool, json_mode: bool) -> None:
    """Import Apple Data & Privacy takeout — Apple TV Playback Activity CSV.

    Accepts the Playback Activity.csv file directly, or a path to the unzipped
    apple_data_export tree (we'll find the CSV inside).
    """
    from .cli_common import emit_result, ImportEnvelope, run_and_emit, resolve_or_emit
    from .importers import apple_takeout as at
    resolved_path = resolve_or_emit("apple-takeout", path, json_mode=json_mode)
    if resolved_path is None:
        return
    if resolved_path.is_dir():
        candidates = list(resolved_path.rglob("Playback Activity.csv"))
        if not candidates:
            emit_result(
                ImportEnvelope(importer="apple-takeout", ok=False,
                               errors=[{"stage": "args",
                                        "message": f"No 'Playback Activity.csv' under {resolved_path}"}]),
                json_mode=json_mode,
            )
            return
        resolved_path = candidates[0]
    s = state_mod.load(STATE_PATH)
    if not s.watched_definition_id:
        emit_result(
            ImportEnvelope(importer="apple-takeout", ok=False,
                           errors=[{"stage": "setup", "message": "Run bootstrap first."}]),
            json_mode=json_mode,
        )
        return
    events = list(at.parse_playback_csv(resolved_path))
    run_and_emit("apple-takeout", events, s,
                 tag_name="apple-tv", check_only=check_only, json_mode=json_mode)


@import_group.command("lastfm")
@click.option("--since", "since_iso", default=None,
              help="ISO 8601 datetime override; defaults to stored watermark.")
@click.option("--max-pages", default=None, type=int,
              help="Cap pagination — useful for large first-run backfills.")
@click.option("--check-only", is_flag=True,
              help="Don't post; just count new items and report.")
@click.option("--json", "json_mode", is_flag=True,
              help="Machine-readable single-line JSON output.")
@click.option("--watermark-overlap-hours", default=1, type=int,
              help="When using the watermark, fetch this many hours BEFORE it "
                   "to catch late server-side reordering. Default 1.")
def import_lastfm(
    since_iso: str | None,
    max_pages: int | None,
    check_only: bool,
    json_mode: bool,
    watermark_overlap_hours: int,
) -> None:
    """Import Last.fm scrobbles via user.getRecentTracks (public, API-key auth)."""
    from datetime import datetime, timedelta, timezone
    from . import watermarks
    from .cli_common import emit_result, import_result_to_dict, ImportEnvelope
    from .importers import lastfm as lastfm_importer

    s = state_mod.load(STATE_PATH)
    if not s.listened_definition_id:
        emit_result(
            ImportEnvelope(
                importer="lastfm", ok=False,
                errors=[{"stage": "setup", "message": "Run `fulcra-media bootstrap` first."}],
            ),
            json_mode=json_mode,
        )
        return

    try:
        creds = lastfm_importer.load_creds()
    except FileNotFoundError:
        emit_result(
            ImportEnvelope(
                importer="lastfm", ok=False,
                errors=[{"stage": "auth",
                         "message": f"Missing {lastfm_importer.CREDS_PATH}. "
                                    "Run `fulcra-media wizard lastfm` for setup."}],
            ),
            json_mode=json_mode,
        )
        return

    # Resolve since: explicit flag > watermark > None (full backfill)
    since: datetime | None = None
    if since_iso:
        try:
            since = datetime.fromisoformat(
                since_iso.replace("Z", "+00:00")
            )
            if since.tzinfo is None:
                since = since.replace(tzinfo=timezone.utc)
        except ValueError as exc:
            emit_result(
                ImportEnvelope(
                    importer="lastfm", ok=False,
                    errors=[{"stage": "args",
                             "message": f"Invalid --since: {exc}"}],
                ),
                json_mode=json_mode,
            )
            return
    else:
        wm = watermarks.get_iso(s, "lastfm")
        if wm is not None:
            since = wm - timedelta(hours=watermark_overlap_hours)

    since_str = since.isoformat() if since else None

    # Fetch + normalize
    try:
        raw_tracks = list(lastfm_importer.fetch_recent_tracks(
            creds, since=since, max_pages=max_pages,
        ))
    except (RuntimeError, httpx.HTTPError) as exc:
        emit_result(
            ImportEnvelope(
                importer="lastfm", ok=False, since_watermark=since_str,
                errors=[{"stage": "fetch", "message": safe_exc_message(exc)}],
            ),
            json_mode=json_mode,
        )
        return
    events = list(lastfm_importer.normalize_history(raw_tracks))

    # Ingest (or check-only)
    client = FulcraClient()
    if not check_only:
        client.ensure_tag("lastfm", s)
    state_mod.save(s, STATE_PATH)
    result = client.run_import(events, s, check_only=check_only)

    new_watermark_iso: str | None = None
    if events and not check_only and result.posted > 0:
        max_ts = max(e.start_time for e in events)
        watermarks.set_iso(s, "lastfm", max_ts)
        new_watermark_iso = max_ts.isoformat()
    state_mod.save(s, STATE_PATH)

    envelope = import_result_to_dict(
        "lastfm", result,
        since_watermark=since_str,
        new_watermark=new_watermark_iso,
        would_post=result.posted if check_only else None,
    )
    emit_result(envelope, json_mode=json_mode)


@import_group.command("deezer")
@click.option("--since", "since_iso", default=None,
              help="ISO 8601 datetime override; defaults to stored watermark.")
@click.option("--max-pages", default=None, type=int,
              help="Cap pagination — useful for large first-run backfills.")
@click.option("--check-only", is_flag=True,
              help="Don't post; just count new items and report.")
@click.option("--json", "json_mode", is_flag=True,
              help="Machine-readable single-line JSON output.")
@click.option("--watermark-overlap-hours", default=1, type=int,
              help="When using the watermark, fetch this many hours BEFORE it "
                   "to catch late server-side reordering. Default 1.")
def import_deezer(
    since_iso: str | None,
    max_pages: int | None,
    check_only: bool,
    json_mode: bool,
    watermark_overlap_hours: int,
) -> None:
    """Import Deezer listening history via /user/me/history (OAuth token)."""
    from datetime import datetime, timedelta, timezone
    from . import watermarks
    from .cli_common import emit_result, import_result_to_dict, ImportEnvelope
    from .importers import deezer as deezer_importer

    s = state_mod.load(STATE_PATH)
    if not s.listened_definition_id:
        emit_result(
            ImportEnvelope(
                importer="deezer", ok=False,
                errors=[{"stage": "setup", "message": "Run `fulcra-media bootstrap` first."}],
            ),
            json_mode=json_mode,
        )
        return

    try:
        creds = deezer_importer.load_creds()
    except FileNotFoundError:
        emit_result(
            ImportEnvelope(
                importer="deezer", ok=False,
                errors=[{"stage": "auth",
                         "message": f"Missing {deezer_importer.CREDS_PATH}. "
                                    "Run `fulcra-media wizard deezer` for setup."}],
            ),
            json_mode=json_mode,
        )
        return

    # Resolve since: explicit flag > watermark > None (full backfill)
    since: datetime | None = None
    if since_iso:
        try:
            since = datetime.fromisoformat(since_iso.replace("Z", "+00:00"))
            if since.tzinfo is None:
                since = since.replace(tzinfo=timezone.utc)
        except ValueError as exc:
            emit_result(
                ImportEnvelope(
                    importer="deezer", ok=False,
                    errors=[{"stage": "args", "message": f"Invalid --since: {exc}"}],
                ),
                json_mode=json_mode,
            )
            return
    else:
        wm = watermarks.get_iso(s, "deezer")
        if wm is not None:
            since = wm - timedelta(hours=watermark_overlap_hours)

    since_str = since.isoformat() if since else None

    try:
        raw_tracks = list(deezer_importer.fetch_history(
            creds, since=since, max_pages=max_pages,
        ))
    except (RuntimeError, httpx.HTTPError) as exc:
        emit_result(
            ImportEnvelope(
                importer="deezer", ok=False, since_watermark=since_str,
                errors=[{"stage": "fetch", "message": safe_exc_message(exc)}],
            ),
            json_mode=json_mode,
        )
        return
    events = list(deezer_importer.normalize_history(raw_tracks))

    client = FulcraClient()
    if not check_only:
        client.ensure_tag("deezer", s)
    state_mod.save(s, STATE_PATH)
    result = client.run_import(events, s, check_only=check_only)

    new_watermark_iso: str | None = None
    if events and not check_only and result.posted > 0:
        max_ts = max(e.start_time for e in events)
        watermarks.set_iso(s, "deezer", max_ts)
        new_watermark_iso = max_ts.isoformat()
    state_mod.save(s, STATE_PATH)

    envelope = import_result_to_dict(
        "deezer", result,
        since_watermark=since_str,
        new_watermark=new_watermark_iso,
        would_post=result.posted if check_only else None,
    )
    emit_result(envelope, json_mode=json_mode)


def _resolve_since(
    since_iso: str | None,
    *,
    importer_name: str,
    watermark_key: str,
    state,
    json_mode: bool,
    watermark_overlap_hours: int = 1,
):
    """Resolve --since: explicit flag > watermark - overlap > None (full backfill).

    Returns (since_dt, since_iso_str, did_emit_error). If did_emit_error is
    True, the caller must return immediately — an envelope has already been
    written.
    """
    from datetime import datetime, timedelta, timezone
    from . import watermarks
    from .cli_common import emit_result, ImportEnvelope

    if since_iso:
        try:
            since = datetime.fromisoformat(since_iso.replace("Z", "+00:00"))
            if since.tzinfo is None:
                since = since.replace(tzinfo=timezone.utc)
        except ValueError as exc:
            emit_result(
                ImportEnvelope(
                    importer=importer_name, ok=False,
                    errors=[{"stage": "args",
                             "message": f"Invalid --since: {exc}"}],
                ),
                json_mode=json_mode,
            )
            return None, None, True
        return since, since.isoformat(), False

    wm = watermarks.get_iso(state, watermark_key)
    if wm is not None:
        since = wm - timedelta(hours=watermark_overlap_hours)
        return since, since.isoformat(), False
    return None, None, False


@import_group.command("generic-rss")
@click.argument("feed_url")
@click.option("--service", required=True,
              help="Service tag to record on each event (e.g. blog, letterboxd, goodreads)")
@click.option("--category", type=click.Choice(["watched", "listened"]),
              required=True)
@click.option("--since", "since_iso", default=None,
              help="ISO 8601 datetime; defaults to per-feed watermark.")
@click.option("--max-entries", default=None, type=int,
              help="Cap how many entries to process from this fetch.")
@click.option("--check-only", is_flag=True)
@click.option("--json", "json_mode", is_flag=True)
def import_generic_rss(
    feed_url: str, service: str, category: str,
    since_iso: str | None, max_entries: int | None,
    check_only: bool, json_mode: bool,
) -> None:
    """Import any RSS/Atom feed as Watched/Listened events.

    Per-feed watermarks (keyed off the feed URL) live in state.watermarks
    so distinct feeds don't clobber each other.
    """
    from . import watermarks
    from .cli_common import (
        emit_result, import_result_to_dict, ImportEnvelope,
    )
    from .importers import generic_rss as rss_importer

    importer_label = f"generic-rss:{service}"
    watermark_key = f"generic-rss:{feed_url}"

    s = state_mod.load(STATE_PATH)
    target_def = (
        s.watched_definition_id if category == "watched"
        else s.listened_definition_id
    )
    if not target_def:
        emit_result(
            ImportEnvelope(
                importer=importer_label, ok=False,
                errors=[{"stage": "setup",
                         "message": f"Run `fulcra-media bootstrap` first; need {category} definition."}],
            ),
            json_mode=json_mode,
        )
        return

    since, since_str, err = _resolve_since(
        since_iso, importer_name=importer_label,
        watermark_key=watermark_key, state=s, json_mode=json_mode,
    )
    if err:
        return

    try:
        all_events = list(rss_importer.normalize_feed(
            feed_url, service=service, category=category,
        ))
    except (RuntimeError, httpx.HTTPError) as exc:
        emit_result(
            ImportEnvelope(
                importer=importer_label, ok=False, since_watermark=since_str,
                errors=[{"stage": "fetch", "message": safe_exc_message(exc)}],
            ),
            json_mode=json_mode,
        )
        return

    # Client-side timestamp filter (most RSS feeds have no native since param).
    if since is not None:
        all_events = [e for e in all_events if e.start_time >= since]
    if max_entries is not None:
        all_events = all_events[:max_entries]

    client = FulcraClient()
    if not check_only:
        client.ensure_tag(service, s)
    state_mod.save(s, STATE_PATH)
    result = client.run_import(all_events, s, check_only=check_only)

    new_watermark_iso: str | None = None
    if all_events and not check_only and result.posted > 0:
        max_ts = max(e.start_time for e in all_events)
        watermarks.set_iso(s, watermark_key, max_ts)
        new_watermark_iso = max_ts.isoformat()
    state_mod.save(s, STATE_PATH)

    envelope = import_result_to_dict(
        importer_label, result,
        since_watermark=since_str,
        new_watermark=new_watermark_iso,
        would_post=result.posted if check_only else None,
    )
    emit_result(envelope, json_mode=json_mode)


@import_group.command("letterboxd")
@click.option("--username", required=True, help="Letterboxd username")
@click.option("--since", "since_iso", default=None,
              help="ISO 8601 datetime; defaults to stored watermark.")
@click.option("--max-entries", default=None, type=int)
@click.option("--check-only", is_flag=True)
@click.option("--json", "json_mode", is_flag=True)
def import_letterboxd(
    username: str, since_iso: str | None, max_entries: int | None,
    check_only: bool, json_mode: bool,
) -> None:
    """Import Letterboxd diary entries via the public RSS feed."""
    from . import watermarks
    from .cli_common import (
        emit_result, import_result_to_dict, ImportEnvelope,
    )
    from .importers import letterboxd as lb

    s = state_mod.load(STATE_PATH)
    if not s.watched_definition_id:
        emit_result(
            ImportEnvelope(
                importer="letterboxd", ok=False,
                errors=[{"stage": "setup",
                         "message": "Run `fulcra-media bootstrap` first."}],
            ),
            json_mode=json_mode,
        )
        return

    since, since_str, err = _resolve_since(
        since_iso, importer_name="letterboxd",
        watermark_key="letterboxd", state=s, json_mode=json_mode,
    )
    if err:
        return

    try:
        all_events = list(lb.fetch_diary(username))
    except (RuntimeError, httpx.HTTPError) as exc:
        emit_result(
            ImportEnvelope(
                importer="letterboxd", ok=False, since_watermark=since_str,
                errors=[{"stage": "fetch", "message": safe_exc_message(exc)}],
            ),
            json_mode=json_mode,
        )
        return

    if since is not None:
        all_events = [e for e in all_events if e.start_time >= since]
    if max_entries is not None:
        all_events = all_events[:max_entries]

    client = FulcraClient()
    if not check_only:
        client.ensure_tag("letterboxd", s)
    state_mod.save(s, STATE_PATH)
    result = client.run_import(all_events, s, check_only=check_only)

    new_watermark_iso: str | None = None
    if all_events and not check_only and result.posted > 0:
        max_ts = max(e.start_time for e in all_events)
        watermarks.set_iso(s, "letterboxd", max_ts)
        new_watermark_iso = max_ts.isoformat()
    state_mod.save(s, STATE_PATH)

    envelope = import_result_to_dict(
        "letterboxd", result,
        since_watermark=since_str,
        new_watermark=new_watermark_iso,
        would_post=result.posted if check_only else None,
    )
    emit_result(envelope, json_mode=json_mode)


@import_group.command("goodreads")
@click.option("--user-id", required=True,
              help="Your Goodreads numeric user id (the number in profile URL).")
@click.option("--since", "since_iso", default=None,
              help="ISO 8601 datetime; defaults to stored watermark.")
@click.option("--max-entries", default=None, type=int,
              help="Cap how many entries to process from this fetch.")
@click.option("--check-only", is_flag=True)
@click.option("--json", "json_mode", is_flag=True)
def import_goodreads(
    user_id: str, since_iso: str | None, max_entries: int | None,
    check_only: bool, json_mode: bool,
) -> None:
    """Import Goodreads 'read' shelf entries via the public RSS feed."""
    from . import watermarks
    from .cli_common import (
        emit_result, import_result_to_dict, ImportEnvelope,
    )
    from .importers import goodreads as gr

    watermark_key = f"goodreads:{user_id}"

    s = state_mod.load(STATE_PATH)
    if not s.read_definition_id:
        emit_result(
            ImportEnvelope(
                importer="goodreads", ok=False,
                errors=[{"stage": "setup",
                         "message": "Run `fulcra-media bootstrap` first; "
                                    "need read definition."}],
            ),
            json_mode=json_mode,
        )
        return

    since, since_str, err = _resolve_since(
        since_iso, importer_name="goodreads",
        watermark_key=watermark_key, state=s, json_mode=json_mode,
    )
    if err:
        return

    try:
        all_events = list(gr.fetch_diary(user_id))
    except (RuntimeError, httpx.HTTPError) as exc:
        emit_result(
            ImportEnvelope(
                importer="goodreads", ok=False, since_watermark=since_str,
                errors=[{"stage": "fetch", "message": safe_exc_message(exc)}],
            ),
            json_mode=json_mode,
        )
        return

    if since is not None:
        all_events = [e for e in all_events if e.start_time >= since]
    if max_entries is not None:
        all_events = all_events[:max_entries]

    client = FulcraClient()
    if not check_only:
        client.ensure_tag("goodreads", s)
    state_mod.save(s, STATE_PATH)
    result = client.run_import(all_events, s, check_only=check_only)

    new_watermark_iso: str | None = None
    if all_events and not check_only and result.posted > 0:
        max_ts = max(e.start_time for e in all_events)
        watermarks.set_iso(s, watermark_key, max_ts)
        new_watermark_iso = max_ts.isoformat()
    state_mod.save(s, STATE_PATH)

    envelope = import_result_to_dict(
        "goodreads", result,
        since_watermark=since_str,
        new_watermark=new_watermark_iso,
        would_post=result.posted if check_only else None,
    )
    emit_result(envelope, json_mode=json_mode)


@import_group.command("strava")
@click.option("--since", "since_iso", default=None,
              help="ISO 8601 datetime; defaults to stored watermark.")
@click.option("--max-pages", default=None, type=int,
              help="Cap pagination — useful for large first-run backfills.")
@click.option("--check-only", is_flag=True,
              help="Don't post; just count new items and report.")
@click.option("--json", "json_mode", is_flag=True,
              help="Machine-readable single-line JSON output.")
def import_strava(
    since_iso: str | None,
    max_pages: int | None,
    check_only: bool,
    json_mode: bool,
) -> None:
    """Import Strava activities via /athlete/activities (OAuth, 6h refresh)."""
    from datetime import datetime, timezone
    from . import watermarks
    from .cli_common import emit_result, import_result_to_dict, ImportEnvelope
    from .importers import strava as strava_importer

    s = state_mod.load(STATE_PATH)
    if not s.activity_definition_id:
        emit_result(
            ImportEnvelope(
                importer="strava", ok=False,
                errors=[{"stage": "setup",
                         "message": "Run `fulcra-media bootstrap` first; "
                                    "need activity definition."}],
            ),
            json_mode=json_mode,
        )
        return

    # Auth: load creds + refresh if the 6h token has rolled over.
    try:
        auth = strava_importer.StravaAuth()
    except FileNotFoundError:
        emit_result(
            ImportEnvelope(
                importer="strava", ok=False,
                errors=[{"stage": "auth",
                         "message": f"Missing {strava_importer.CREDS_PATH}. "
                                    "Run `fulcra-media wizard strava` for setup."}],
            ),
            json_mode=json_mode,
        )
        return

    # Resolve since: explicit flag > watermark (no overlap subtraction —
    # Strava's `after` is a strict GT, source-id dedup handles boundary).
    since: datetime | None = None
    if since_iso:
        try:
            since = datetime.fromisoformat(since_iso.replace("Z", "+00:00"))
            if since.tzinfo is None:
                since = since.replace(tzinfo=timezone.utc)
        except ValueError as exc:
            emit_result(
                ImportEnvelope(
                    importer="strava", ok=False,
                    errors=[{"stage": "args",
                             "message": f"Invalid --since: {exc}"}],
                ),
                json_mode=json_mode,
            )
            return
    else:
        wm = watermarks.get_iso(s, "strava")
        if wm is not None:
            since = wm

    since_str = since.isoformat() if since else None

    try:
        auth.refresh_if_needed()
        raw_activities = list(strava_importer.fetch_activities(
            auth.creds, since=since, max_pages=max_pages,
        ))
    except (RuntimeError, httpx.HTTPError) as exc:
        emit_result(
            ImportEnvelope(
                importer="strava", ok=False, since_watermark=since_str,
                errors=[{"stage": "fetch", "message": safe_exc_message(exc)}],
            ),
            json_mode=json_mode,
        )
        return
    events = list(strava_importer.normalize_activities(raw_activities))

    client = FulcraClient()
    if not check_only:
        client.ensure_tag("strava", s)
    state_mod.save(s, STATE_PATH)
    result = client.run_import(events, s, check_only=check_only)

    new_watermark_iso: str | None = None
    if events and not check_only and result.posted > 0:
        max_ts = max(e.start_time for e in events)
        watermarks.set_iso(s, "strava", max_ts)
        new_watermark_iso = max_ts.isoformat()
    state_mod.save(s, STATE_PATH)

    envelope = import_result_to_dict(
        "strava", result,
        since_watermark=since_str,
        new_watermark=new_watermark_iso,
        would_post=result.posted if check_only else None,
    )
    emit_result(envelope, json_mode=json_mode)


if __name__ == "__main__":
    cli()
