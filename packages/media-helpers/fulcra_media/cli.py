"""Click entry point."""

from __future__ import annotations

import json
from pathlib import Path

import click
import httpx

from . import state as state_mod
from .cli_common import safe_exc_message
from .importers import netflix as netflix_importer
from .wizards.netflix import walkthrough as netflix_walkthrough
from .wizards.trakt import walkthrough as trakt_walkthrough
from .wizards.apple_podcasts import walkthrough as apple_podcasts_walkthrough
from .wizards.apple_tv import walkthrough as apple_tv_walkthrough
from .wizards.spotify import walkthrough as spotify_walkthrough
from .wizards.spotify_ifttt import walkthrough as spotify_ifttt_walkthrough
from .wizards.apple_takeout import walkthrough as apple_takeout_walkthrough
from .wizards.ifttt import walkthrough as ifttt_walkthrough
from .wizards.pipedream import walkthrough as pipedream_walkthrough
from .wizards.lastfm import walkthrough as lastfm_walkthrough
from .wizards.deezer import walkthrough as deezer_walkthrough
from .wizards.letterboxd import walkthrough as letterboxd_walkthrough
from .wizards.goodreads import walkthrough as goodreads_walkthrough
from .wizards.youtube import walkthrough as youtube_walkthrough
from .wizards.plex import walkthrough as plex_walkthrough
from .wizards.jellyfin import walkthrough as jellyfin_walkthrough
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


@cli.command(help="Configure v1 media targets; retained legacy IDs require explicit Event/UUID mappings.")
@click.option('--watched', default=None, metavar='Event/UUID')
@click.option('--listened', default=None, metavar='Event/UUID')
@click.option('--read', 'read_pin', default=None, metavar='Event/UUID')
def bootstrap(watched, listened, read_pin) -> None:
    from .media_config import configured_media
    try:
        with configured_media(STATE_PATH, bootstrap=True,
                pins={'watched': watched, 'listened': listened, 'read': read_pin}) as run:
            click.echo('v1 media configured for account=' + run.session.account_id)
    except Exception as exc:
        raise click.UsageError(safe_exc_message(exc)) from None


@cli.command(help="Retired. Preserve evidence; use bootstrap with explicit v1 mappings.")
@click.option('--confirm', is_flag=True)
@click.option('--keep-watched', is_flag=True)
@click.option('--keep-listened', is_flag=True)
@click.option('--keep-read', is_flag=True)
def reset(confirm, keep_watched, keep_listened, keep_read) -> None:
    raise click.UsageError('reset is retired; original data and evidence are preserved. '
                           'Use fulcra-media bootstrap with explicit v1 Event/UUID mappings.')


cli.add_command(setup_command, name="setup")


@cli.command(help="Print cached v1 authority and retained legacy selections; no live verification.")
def status() -> None:
    from .media_config import cached_status
    click.echo(json.dumps(cached_status(STATE_PATH), sort_keys=True))


@cli.group(help="Interactive walkthroughs for requesting source data.")
def wizard() -> None:
    pass


@cli.group(help="Import data from a source.", name="import")
def import_group() -> None:
    pass


wizard.add_command(netflix_walkthrough, name="netflix")
wizard.add_command(trakt_walkthrough, name="trakt")
wizard.add_command(apple_podcasts_walkthrough, name="apple-podcasts")
wizard.add_command(apple_tv_walkthrough, name="apple-tv")
wizard.add_command(spotify_walkthrough, name="spotify")
wizard.add_command(spotify_ifttt_walkthrough, name="spotify-ifttt")
wizard.add_command(apple_takeout_walkthrough, name="apple-takeout")
wizard.add_command(ifttt_walkthrough, name="ifttt")
wizard.add_command(pipedream_walkthrough, name="pipedream")
wizard.add_command(lastfm_walkthrough, name="lastfm")
wizard.add_command(deezer_walkthrough, name="deezer")
wizard.add_command(letterboxd_walkthrough, name="letterboxd")
wizard.add_command(goodreads_walkthrough, name="goodreads")
wizard.add_command(youtube_walkthrough, name="youtube")
wizard.add_command(plex_walkthrough, name="plex")
wizard.add_command(jellyfin_walkthrough, name="jellyfin")


@import_group.command("netflix")
@click.argument("path", type=str)
@click.option("--check-only", is_flag=True, help="Don't post; report what would be posted.")
@click.option("--json", "json_mode", is_flag=True, help="Single-line JSON output.")
def import_netflix(path: str, check_only: bool, json_mode: bool) -> None:
    """Import a Netflix slim-variant CSV (local path or fulcra:/... URI)."""
    from .cli_common import emit_result, media_failure_envelope, import_result_to_dict
    from .media_config import configured_media
    from . import library
    run = result = None
    try:
        with configured_media(STATE_PATH, check_only=check_only) as run:
            resolved = Path(library.resolve(path))
            events = list(netflix_importer.parse_auto(resolved))
            result = run.import_events(events)
        envelope = import_result_to_dict('netflix', result, since_watermark=None,
            new_watermark=None, would_post=result.posted if check_only else None)
    except Exception as exc:
        envelope = media_failure_envelope('netflix', stage='import', message=safe_exc_message(exc),
            run=run, result=result, check_only=check_only)
    emit_result(envelope, json_mode=json_mode)


@import_group.command("trakt")
@click.option("--cluster-threshold", default=5, type=int,
              help="Mark >=N items sharing watched_at as timestamp_confidence: low")
@click.option("--clusters", "cluster_spec", default=None, metavar="POLICY",
              help="Cluster handling: 'drop', 'sentinel:YYYY', 'keep', or 'ask'. "
                   "Omitted: ask on TTY; require a choice only when clusters exist otherwise.")
@click.option("--check-only", is_flag=True)
@click.option("--json", "json_mode", is_flag=True)
@click.option("--twin-policy",
              type=click.Choice(["ask", "auto-discard", "keep"]),
              default=None,
              help="Loose content twins from current input or verified owned records: "
                   "ask (TTY default), auto-discard, or keep. Omitted JSON/non-TTY "
                   "keeps and warns when candidates exist. Explicit ask requires a TTY without JSON.")
def import_trakt(cluster_threshold: int, cluster_spec: str | None,
                 check_only: bool, json_mode: bool,
                 twin_policy: str | None) -> None:
    """Import Trakt watch history via the Trakt API."""
    from fulcra_csv import apply_cluster_policy
    from .cli_common import emit_result, media_failure_envelope, import_result_to_dict
    from .media_config import configured_media
    from .importers import trakt as trakt_importer

    run = result = None
    stage = "args"
    interactive = not json_mode and click.get_text_stream("stdin").isatty()
    try:
        _validate_cluster_spec(cluster_spec, cluster_threshold)
        if not interactive and (twin_policy == "ask" or cluster_spec == "ask"):
            raise ValueError("interactive choice requires a TTY without JSON")
        stage = "setup"
        with configured_media(STATE_PATH, check_only=check_only) as run:
            from fulcra_collect.trakt_source import current_work_scope
            with current_work_scope(run.ctx, run.session, run.ctx._media_local_authority):
                stage = "auth"
                from fulcra_collect.trakt_identity import CurrentIdentity, ProbeWindow
                bounds = trakt_importer._ScanBounds()
                initial_probe = ProbeWindow(bounds)
                auth = trakt_importer.TraktAuth(_authority=run.ctx._trakt_authority,
                                                _local=run.ctx._trakt_local)
                binding = auth.binding
                identity = CurrentIdentity(binding)
                run.ctx._register_current_work_guard(identity.check_local_after_authority)
                initial_probe.check()
                headers = auth.headers(timeout=bounds.timeout(), _remaining=bounds.timeout)
                initial_probe.check()
                identity.observe(bounds, window=initial_probe, transport=None)
                stage = "fetch"
                items = list(trakt_importer.fetch_history_with_headers(
                    headers, strict_scan=True, _bounds=bounds, _before_request=binding.check_before_source))
                identity.observe(bounds, terminal=True)
                events = list(trakt_importer.normalize_history(items, cluster_threshold=cluster_threshold))
                identity.check_before_current()
                stage = "args"
                policy = _resolve_cluster_policy(events, cluster_spec=cluster_spec,
                    cluster_size_threshold=cluster_threshold, interactive=interactive)
                if policy:
                    identity.check_before_current()
                    events = apply_cluster_policy(events, policy)
                stage = "import"
                approval = None
                choice = twin_policy
                if choice in (None, "ask"):
                    identity.check_before_current()
                    offer = run.inspect_twins(events)
                    if offer.pairs:
                        if not interactive:
                            click.echo(f"warning: {len(offer.pairs)} low-confidence events have "
                                       "high-confidence twins; keeping them. Pass --twin-policy "
                                       "auto-discard|keep to choose explicitly.", err=True)
                            choice = "keep"
                        else:
                            for i, pair in enumerate(offer.pairs, 1):
                                click.echo(f"{i}. {pair.incoming} ↔ {pair.high} "
                                           f"[{pair.origin}; {pair.fingerprint}]", err=True)
                            stage = "args"
                            answer = click.prompt("Discard the offered low-confidence twins?",
                                type=click.Choice(["yes", "no"]), default="yes")
                            choice = "auto-discard" if answer == "yes" else "keep"
                            approval = offer if answer == "yes" else None
                    else:
                        choice = "keep"
                stage = "import"
                identity.check_before_current()
                result = run.import_events(events, twin_policy=choice, approved_twins=approval)
                identity.check_before_current()
        envelope = import_result_to_dict("trakt", result, since_watermark=None,
            new_watermark=None, would_post=result.posted if check_only else None)
    except (Exception, click.Abort) as exc:
        if isinstance(exc, (click.Abort, EOFError)):
            stage = "args"
        elif (stage == "fetch" and isinstance(exc, httpx.HTTPStatusError)
              and exc.response.status_code in (401, 403)):
            stage = "auth"
        messages = {"args": "Invalid or cancelled Trakt import options.",
                    "setup": "Unable to open configured media authority; preserve pending evidence.",
                    "auth": "Unable to load or refresh Trakt credentials.",
                    "fetch": "Unable to fetch or normalize Trakt history.",
                    "import": "Unable to complete Trakt import; preserve pending evidence."}
        from fulcra_collect.trakt_identity import MISMATCH
        public_message = MISMATCH if str(exc) == MISMATCH else messages[stage]
        envelope = media_failure_envelope("trakt", stage=stage, message=public_message,
            run=run, result=result, check_only=check_only)
    emit_result(envelope, json_mode=json_mode)


def _validate_cluster_spec(spec, threshold):
    """Validate supplied syntax even when the source contains no clusters."""
    from fulcra_csv import ClusterPolicy
    if spec is None or spec == "ask":
        return
    if spec in ("drop", "keep"):
        ClusterPolicy(action=spec, cluster_size_threshold=threshold)
        return
    if spec.startswith("sentinel:"):
        ClusterPolicy(action="sentinel", sentinel_year=int(spec.split(":", 1)[1]),
                      cluster_size_threshold=threshold)
        return
    raise ValueError("invalid cluster policy")


def _resolve_cluster_policy(
    events: list,
    *,
    cluster_spec: str | None,
    cluster_size_threshold: int,
    interactive: bool | None = None,
):
    """Resolve --clusters into a ClusterPolicy. None means no cluster preprocessing.

    Modes:
      'drop'             — drop all cluster members
      'sentinel:YYYY'    — shift cluster members to Jan 1, YYYY
      'keep'             — leave at original timestamps
      'ask' (or None on TTY) — interactive prompt
    """
    from fulcra_csv import ClusterPolicy, cluster_size_of

    _validate_cluster_spec(cluster_spec, cluster_size_threshold)
    if interactive is None:
        interactive = click.get_text_stream("stdin").isatty()
    if cluster_spec == "ask" and not interactive:
        raise ValueError("interactive cluster choice requires a TTY")
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
        if not interactive:
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
    from .cli_common import emit_result, media_failure_envelope, import_result_to_dict
    from .media_config import configured_media
    from .importers import apple_podcasts as ap
    importer = "apple-podcasts"
    stage = "import"
    run = result = None
    try:
        with configured_media(STATE_PATH, check_only=check_only) as run:
            stage = 'snapshot'
            events = list(ap.parse_db(Path(db_path if db_path is not None else ap.DEFAULT_DB_PATH)))
            stage = "import"
            result = run.import_events(events)
        envelope = import_result_to_dict(importer, result, since_watermark=None,
            new_watermark=None, would_post=result.posted if check_only else None)
    except Exception as exc:
        envelope = media_failure_envelope(importer, stage=stage, message=safe_exc_message(exc),
            run=run, result=result, check_only=check_only)
    emit_result(envelope, json_mode=json_mode)


@import_group.command("apple-podcasts-timemachine")
@click.option("--check-only", is_flag=True)
@click.option("--json", "json_mode", is_flag=True)
def import_apple_podcasts_timemachine(check_only: bool, json_mode: bool) -> None:
    """Recover Apple Podcasts replay history by walking Time Machine snapshots."""
    from .cli_common import emit_result, media_failure_envelope, import_result_to_dict
    from .media_config import configured_media
    from .importers import apple_podcasts as ap
    importer = "apple-podcasts-timemachine"
    stage = "import"
    run = result = None
    try:
        with configured_media(STATE_PATH, check_only=check_only) as run:
            stage = 'fetch'
            snapshots = ap.find_timemachine_snapshots()
            if not snapshots:
                raise RuntimeError('No Time Machine backups with Apple Podcasts data found. '
                                   'Verify tmutil listbackups + Time Machine mount + FDA.')
            if not json_mode:
                click.echo(f"Walking {len(snapshots)} Time Machine snapshots...", err=True)
            events = []
            stage = 'snapshot'
            for snap in snapshots:
                if not json_mode:
                    click.echo(f"  {snap}", err=True)
                events.extend(ap.parse_db(snap))
            stage = "import"
            result = run.import_events(events)
        envelope = import_result_to_dict(importer, result, since_watermark=None,
            new_watermark=None, would_post=result.posted if check_only else None)
    except Exception as exc:
        envelope = media_failure_envelope(importer, stage=stage, message=safe_exc_message(exc),
            run=run, result=result, check_only=check_only)
    emit_result(envelope, json_mode=json_mode)


@import_group.command("spotify-extended")
@click.argument("path", type=str)
@click.option("--check-only", is_flag=True)
@click.option("--json", "json_mode", is_flag=True)
def import_spotify_extended(path: str, check_only: bool, json_mode: bool) -> None:
    """Import Spotify Extended Streaming History from a GDPR-export zip."""
    from .cli_common import emit_result, media_failure_envelope, import_result_to_dict
    from .media_config import configured_media
    from . import library
    from .importers import spotify as sp
    importer = "spotify-extended"
    stage = "import"
    run = result = None
    try:
        with configured_media(STATE_PATH, check_only=check_only) as run:
            stage = 'args'
            resolved = Path(library.resolve(path))
            stage = 'import'
            events = list(sp.parse_extended_zip(resolved))
            stage = "import"
            result = run.import_events(events)
        envelope = import_result_to_dict(importer, result, since_watermark=None,
            new_watermark=None, would_post=result.posted if check_only else None)
    except Exception as exc:
        envelope = media_failure_envelope(importer, stage=stage, message=safe_exc_message(exc),
            run=run, result=result, check_only=check_only)
    emit_result(envelope, json_mode=json_mode)


@import_group.command("youtube")
@click.argument("path", type=str)
@click.option("--check-only", is_flag=True)
@click.option("--json", "json_mode", is_flag=True)
def import_youtube(path: str, check_only: bool, json_mode: bool) -> None:
    """Import a Google Takeout YouTube watch-history.json file."""
    from .cli_common import emit_result, media_failure_envelope, import_result_to_dict
    from .media_config import configured_media
    from . import library
    from .importers import youtube as yt
    importer = "youtube"
    stage = "import"
    run = result = None
    try:
        with configured_media(STATE_PATH, check_only=check_only) as run:
            stage = 'args'
            resolved = Path(library.resolve(path))
            stage = 'import'
            events = list(yt.parse_takeout_json(resolved))
            stage = "import"
            result = run.import_events(events)
        envelope = import_result_to_dict(importer, result, since_watermark=None,
            new_watermark=None, would_post=result.posted if check_only else None)
    except Exception as exc:
        envelope = media_failure_envelope(importer, stage=stage, message=safe_exc_message(exc),
            run=run, result=result, check_only=check_only)
    emit_result(envelope, json_mode=json_mode)


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
    from .cli_common import emit_result, media_failure_envelope, import_result_to_dict
    from .media_config import configured_media
    from . import library
    from fulcra_csv import ColumnMap
    from .importers.generic_csv import parse_media_csv, _FP_AUTO
    importer = f"generic-csv:{service}"
    stage = "import"
    run = result = None
    try:
        with configured_media(STATE_PATH, check_only=check_only) as run:
            stage = 'args'
            resolved = Path(library.resolve(path))
            cm = ColumnMap(timestamp=ts_col, title=title_col, subtitle=subtitle_col or None,
                           source_id=id_col or None, duration_seconds=duration_col, end_time=end_col)
            if tz_name == 'UTC':
                from datetime import timezone
                tz = timezone.utc
            else:
                from zoneinfo import ZoneInfo
                tz = ZoneInfo(tz_name)
            fp_arg = _FP_AUTO if fingerprint == 'auto' else (None if fingerprint == 'none' else fingerprint)
            stage = 'import'
            events = list(parse_media_csv(resolved, service=service, category=category,
                column_map=cm, tz=tz, confidence=confidence, fingerprint_kind=fp_arg))
            stage = "import"
            result = run.import_events(events)
        envelope = import_result_to_dict(importer, result, since_watermark=None,
            new_watermark=None, would_post=result.posted if check_only else None)
    except Exception as exc:
        envelope = media_failure_envelope(importer, stage=stage, message=safe_exc_message(exc),
            run=run, result=result, check_only=check_only)
    emit_result(envelope, json_mode=json_mode)


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
    from .cli_common import emit_result, media_failure_envelope, import_result_to_dict
    from .media_config import configured_media
    from . import library
    from zoneinfo import ZoneInfo
    from .importers import spotify_ifttt as si
    importer = "spotify-ifttt"
    stage = "import"
    run = result = None
    try:
        with configured_media(STATE_PATH, check_only=check_only) as run:
            stage = 'args'
            resolved = Path(library.resolve(path))
            tz = ZoneInfo(tz_name)
            stage = 'import'
            events = list(si.parse_ifttt_zip(resolved, tz=tz))
            stage = "import"
            result = run.import_events(events)
        envelope = import_result_to_dict(importer, result, since_watermark=None,
            new_watermark=None, would_post=result.posted if check_only else None)
    except Exception as exc:
        envelope = media_failure_envelope(importer, stage=stage, message=safe_exc_message(exc),
            run=run, result=result, check_only=check_only)
    emit_result(envelope, json_mode=json_mode)


@import_group.command("apple-takeout")
@click.argument("path", type=str)
@click.option("--check-only", is_flag=True)
@click.option("--json", "json_mode", is_flag=True)
def import_apple_takeout(path: str, check_only: bool, json_mode: bool) -> None:
    """Import Apple Data & Privacy takeout — Apple TV Playback Activity CSV.

    Accepts the Playback Activity.csv file directly, or a path to the unzipped
    apple_data_export tree (we'll find the CSV inside).
    """
    from .cli_common import emit_result, media_failure_envelope, import_result_to_dict
    from .media_config import configured_media
    from . import library
    from .importers import apple_takeout as at
    importer = "apple-takeout"
    stage = "import"
    run = result = None
    try:
        with configured_media(STATE_PATH, check_only=check_only) as run:
            stage = 'args'
            resolved_path = Path(library.resolve(path))
            if resolved_path.is_dir():
                candidates = list(resolved_path.rglob('Playback Activity.csv'))
                if not candidates:
                    raise ValueError(f"No 'Playback Activity.csv' under {resolved_path}")
                resolved_path = candidates[0]
            stage = 'import'
            events = list(at.parse_playback_csv(resolved_path))
            stage = "import"
            result = run.import_events(events)
        envelope = import_result_to_dict(importer, result, since_watermark=None,
            new_watermark=None, would_post=result.posted if check_only else None)
    except Exception as exc:
        envelope = media_failure_envelope(importer, stage=stage, message=safe_exc_message(exc),
            run=run, result=result, check_only=check_only)
    emit_result(envelope, json_mode=json_mode)


@import_group.command("lastfm")
@click.option("--since", "since_iso", default=None,
              help="ISO 8601 datetime override; defaults to scoped private cursor (no legacy seed).")
@click.option("--max-pages", default=None, type=int,
              help="Cap pagination (1–10000); any configured cap withholds cursor.")
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
    """Import a strict snapshot with the configured writer and scoped private cursor.

    Redirect/resource refusals fail the scan. Caps withhold progress; finite
    snapshots cannot guarantee older history or late edits. No legacy seed.
    """
    from .poll_cli import run_poll

    run_poll(
        "lastfm", STATE_PATH, since_iso=since_iso,
        check_only=check_only, json_mode=json_mode,
        max_pages=max_pages, overlap=watermark_overlap_hours,
    )


@import_group.command("deezer")
@click.option("--since", "since_iso", default=None,
              help="ISO 8601 datetime override; defaults to scoped private cursor (no legacy seed).")
@click.option("--max-pages", default=None, type=int,
              help="Cap pagination (1–10000); any configured cap withholds cursor.")
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
    """Import a strict snapshot with the configured writer and scoped private cursor.

    Redirect/resource refusals fail the scan. Caps withhold progress; finite
    snapshots cannot guarantee older history or late edits. No legacy seed.
    """
    from .poll_cli import run_poll

    run_poll(
        "deezer", STATE_PATH, since_iso=since_iso,
        check_only=check_only, json_mode=json_mode,
        max_pages=max_pages, overlap=watermark_overlap_hours,
    )


@import_group.command("generic-rss")
@click.argument("feed_url")
@click.option("--service", required=True,
              help="Service tag to record on each event (e.g. blog, letterboxd, goodreads)")
@click.option("--category", type=click.Choice(["watched", "listened"]),
              required=True)
@click.option("--since", "since_iso", default=None,
              help="ISO 8601 datetime; defaults to scoped private cursor (no legacy seed).")
@click.option("--max-entries", default=None, type=int,
              help="Cap selected entries; any configured cap withholds cursor.")
@click.option("--check-only", is_flag=True)
@click.option("--json", "json_mode", is_flag=True)
def import_generic_rss(
    feed_url: str, service: str, category: str,
    since_iso: str | None, max_entries: int | None,
    check_only: bool, json_mode: bool,
) -> None:
    """Import a strict snapshot with the configured writer and scoped private cursor.

    Redirect/resource refusals fail the scan. Caps withhold progress; finite
    snapshots cannot guarantee older history or late edits. No legacy seed.
    """
    from .poll_cli import run_poll

    run_poll(
        "generic-rss", STATE_PATH, since_iso=since_iso,
        check_only=check_only, json_mode=json_mode,
        feed_url=feed_url, service=service, category=category, max_entries=max_entries,
    )


@import_group.command("letterboxd")
@click.option("--username", required=True, help="Letterboxd username")
@click.option("--since", "since_iso", default=None,
              help="ISO 8601 datetime; defaults to scoped private cursor (no legacy seed).")
@click.option("--max-entries", default=None, type=int)
@click.option("--check-only", is_flag=True)
@click.option("--json", "json_mode", is_flag=True)
def import_letterboxd(
    username: str, since_iso: str | None, max_entries: int | None,
    check_only: bool, json_mode: bool,
) -> None:
    """Import a strict snapshot with the configured writer and scoped private cursor.

    Redirect/resource refusals fail the scan. Caps withhold progress; finite
    snapshots cannot guarantee older history or late edits. No legacy seed.
    """
    from .poll_cli import run_poll

    run_poll(
        "letterboxd", STATE_PATH, since_iso=since_iso,
        check_only=check_only, json_mode=json_mode,
        username=username, max_entries=max_entries,
    )


@import_group.command("goodreads")
@click.option("--user-id", required=True,
              help="Your Goodreads numeric user id (the number in profile URL).")
@click.option("--since", "since_iso", default=None,
              help="ISO 8601 datetime; defaults to scoped private cursor (no legacy seed).")
@click.option("--max-entries", default=None, type=int,
              help="Cap selected entries; any configured cap withholds cursor.")
@click.option("--check-only", is_flag=True)
@click.option("--json", "json_mode", is_flag=True)
def import_goodreads(
    user_id: str, since_iso: str | None, max_entries: int | None,
    check_only: bool, json_mode: bool,
) -> None:
    """Import a strict snapshot with the configured writer and scoped private cursor.

    Redirect/resource refusals fail the scan. Caps withhold progress; finite
    snapshots cannot guarantee older history or late edits. No legacy seed.
    """
    from .poll_cli import run_poll

    run_poll(
        "goodreads", STATE_PATH, since_iso=since_iso,
        check_only=check_only, json_mode=json_mode,
        user_id=user_id, max_entries=max_entries,
    )


def _wait_for_webhook_health(server, server_thread, *, timeout: float = 5.0
                             ) -> bool:
    """Return once ``server`` has actually served its local health endpoint.

    A bound/listening socket is not sufficient readiness evidence because
    ``BaseServer.shutdown`` may race with ``serve_forever`` initialization.
    This probe stays loopback-local and bounds every attempt.
    """
    import http.client
    import time

    host, port = server.server_address[:2]
    connect_host = host
    if host in ("", "0.0.0.0"):
        connect_host = "127.0.0.1"
    elif host == "::":
        connect_host = "::1"

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not server_thread.is_alive():
            return False
        remaining = deadline - time.monotonic()
        conn = http.client.HTTPConnection(
            connect_host, port, timeout=max(0.05, min(0.25, remaining))
        )
        try:
            conn.request("GET", "/health")
            response = conn.getresponse()
            response.read()
            if response.status == 200:
                return True
        except (OSError, TimeoutError, http.client.HTTPException):
            time.sleep(0.01)
        finally:
            conn.close()
    return False


@cli.command("webhook", help="Long-running HTTP server for Plex/Jellyfin webhooks.")
@click.option("--host", default="127.0.0.1", show_default=True,
              help="Bind address. Default loopback only.")
@click.option("--port", default=8765, show_default=True, type=int,
              help="Bind port.")
@click.option("--bearer-token", default=None,
              help="If set, require Authorization: Bearer <token> from clients "
                   "(or ?token=<token> for Plex's no-custom-headers webhook config). "
                   "Required when --host is non-loopback.")
@click.option("--json", "json_mode", is_flag=True,
              help="One JSON line per significant lifecycle event on stdout; "
                   "clean stderr for logs.")
def webhook_serve(host: str, port: int, bearer_token: str | None,
                  json_mode: bool) -> None:
    """Serve threaded HTTP with one private configured-v1 authority owner."""
    import json as _json
    import signal
    import sys
    import threading

    from .cli_common import emit_result, ImportEnvelope
    from .webhook_owner import MediaOwner, ServeLoop
    from .webhook_receiver import make_server

    def fail(failed_stage, message):
        envelope = ImportEnvelope(importer="webhook", ok=False, errors=[{
            "stage": failed_stage, "message": message,
        }])
        try:
            emit_result(envelope, json_mode=json_mode)
        except Exception:
            # A broken stdout must not bypass cleanup or turn exit 2 into 1.
            # Best-effort stderr keeps the same scrubbed structured envelope.
            from dataclasses import asdict
            try:
                click.echo(_json.dumps(asdict(envelope)), err=True)
                sys.stderr.flush()
            except Exception:
                pass
            raise click.exceptions.Exit(2) from None

    if host not in ("127.0.0.1", "localhost") and not bearer_token:
        fail("args", "non-loopback --host requires --bearer-token")
        return

    wake = threading.Event()
    owner = MediaOwner(STATE_PATH, wake)
    server = loop = None
    previous = {}
    stage = "setup"
    failure = None
    cleanup_failed = False

    def handle(_signum, _frame):
        wake.set()

    try:
        owner.start()
        stage = "bind"
        server = make_server(host=host, port=port, state=state_mod.State(), client=owner,
                             bearer_token=bearer_token,
                             log_stream=sys.stderr if not json_mode else None)
        stage = "ready"
        for signum in (signal.SIGTERM, signal.SIGINT):
            previous[signum] = signal.signal(signum, handle)
        loop = ServeLoop(server, wake)
        loop.start()
        if not _wait_for_webhook_health(server, loop.thread):
            raise RuntimeError("webhook health handshake failed")
        while not loop.entered.wait(0.01):
            if loop.done.is_set() or owner.failure is not None:
                raise RuntimeError("webhook loop failed before readiness")
        if owner.failure is not None or loop.done.is_set():
            raise RuntimeError("webhook failed before readiness")
        bound_host, bound_port = server.server_address[:2]
        if json_mode:
            click.echo(_json.dumps({"ok": True, "stage": "ready", "host": bound_host,
                                    "port": bound_port,
                                    "bearer_token_required": bool(bearer_token)}))
        else:
            click.echo(f"fulcra-media webhook listening on http://{bound_host}:{bound_port}", err=True)
        sys.stdout.flush()
        stage = "shutdown"
        wake.wait()
        if owner.failure is not None or loop.failure is not None or loop.done.is_set():
            raise RuntimeError("webhook owner or serve loop failed")
    except BaseException as exc:
        failure = exc
    finally:
        owner.stop_admission()
        try:
            if loop is not None:
                loop.close()
        except BaseException as exc:
            cleanup_failed = True
            failure = failure or exc
        try:
            if server is not None:
                server.server_close()
        except BaseException as exc:
            cleanup_failed = True
            failure = failure or exc
        owner.close()
        if owner.failure is not None or (loop is not None and loop.failure is not None):
            failure = failure or owner.failure or loop.failure
            cleanup_failed = cleanup_failed or owner.cleanup_failure is not None
        for signum, handler in previous.items():
            try:
                signal.signal(signum, handler)
            except BaseException as exc:
                cleanup_failed = True
                failure = failure or exc

    if failure is not None:
        # Exception details can include authority URLs/tokens or record payloads.
        # Lifecycle diagnostics intentionally classify only the bounded stage.
        fail("shutdown" if cleanup_failed else stage,
             "webhook lifecycle failed; preserve original authority and pending evidence")
        return
    try:
        summary = server.context.health()
        summary["stage"] = "shutdown"
        if json_mode:
            click.echo(_json.dumps(summary))
        else:
            click.echo(f"shutdown: received={summary['received']} posted={summary['posted']}", err=True)
        sys.stdout.flush()
    except Exception:
        fail("shutdown", "webhook shutdown output failed")


if __name__ == "__main__":
    cli()
