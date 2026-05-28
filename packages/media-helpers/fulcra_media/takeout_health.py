"""Health checks for the five file-/takeout-driven media plugins:
netflix, spotify-extended, youtube, apple-takeout, apple-music-takeout.

Each check resolves the configured ``path`` setting, makes sure the file
or folder exists, then calls the plugin's importer to pull off the first
few events as a preview. The preview lands in the wizard's
test_connection step so the user can confirm they handed us the right
file before clicking Next — the alternative is to fill in the path,
click through to the dashboard, hit Run now, and only then see a
"file not found" / "wrong shape" error in the activity log.

We deliberately don't honour the plugin's ``since`` / ``until`` cutoffs
here. The point of the preview is just "did you pick the right file?",
not "what will tonight's scheduled run import?" — silencing the preview
because the user's `since=1y` filter happens to exclude their 5-year-old
takeout would be confusing, not helpful.
"""
from __future__ import annotations

from itertools import islice
from pathlib import Path

from fulcra_collect.plugin import HealthResult

from . import library
from .importers import apple_music_takeout as apple_music_takeout_importer
from .importers import apple_takeout as apple_takeout_importer
from .importers import netflix as netflix_importer
from .importers import spotify as spotify_importer
from .importers import youtube as youtube_importer

# Cap the preview parse at 3 events — the wizard list shows ~5, but
# parsing more than we render wastes time on a multi-GB takeout that
# the user is staring at a spinner for.
_PREVIEW_LIMIT = 3


def _resolve(ctx, *, label: str) -> tuple[Path | None, HealthResult | None]:
    """Resolve ctx.config["path"]. Returns ``(path, None)`` on success
    or ``(None, HealthResult)`` describing the failure.

    Splits the no-config path / missing-file path from the success path
    so the per-plugin checks below stay focused on the "parse the first
    few events" step they actually own.
    """
    raw = ctx.config.get("path") if hasattr(ctx, "config") else None
    if not raw:
        return None, HealthResult(
            ok=False,
            summary=f"Configure the {label} path on the previous step first.",
        )
    try:
        resolved = library.resolve(raw)
    except Exception as exc:
        return None, HealthResult(
            ok=False,
            summary=f"Couldn't resolve the {label} path: {exc}.",
        )
    if not resolved.exists():
        return None, HealthResult(
            ok=False,
            summary=(
                f"No file or folder at {resolved}. "
                f"Double-check the {label} path you entered."
            ),
        )
    return resolved, None


def _events_to_preview(events: list) -> list[dict]:
    """Render NormalizedEvents as wizard preview rows. ``title`` and
    ``subtitle`` come from the event; ``watched_at`` is the ISO start
    time. Matches the shape used by lastfm_health and apple_podcasts_health
    so the test_connection template renders all of them identically."""
    preview = []
    for ev in events:
        # NormalizedEvent.note often holds the artist/show ("Reply All —
        # Hard Fork"); fall back to "" if the importer didn't set it.
        subtitle = (ev.note or "").strip()
        # The note frequently includes the title prefixed ("Title — Note");
        # we don't try to strip that here — the renderer only shows title,
        # subtitle is a hint and a duplicate is harmless.
        preview.append({
            "title": ev.title or "?",
            "subtitle": subtitle,
            "watched_at": ev.start_time.isoformat() if ev.start_time else "",
        })
    return preview


def _try_parse(parser, *args, **kwargs) -> tuple[list, HealthResult | None]:
    """Run a parser, take the first N events, surface any error as a
    HealthResult. Centralises the try/except so the per-plugin checks
    are 5-line wrappers."""
    try:
        events = list(islice(parser(*args, **kwargs), _PREVIEW_LIMIT))
    except FileNotFoundError as exc:
        return [], HealthResult(
            ok=False,
            summary=f"File not found: {exc}.",
        )
    except (ValueError, RuntimeError) as exc:
        return [], HealthResult(
            ok=False,
            summary=f"Couldn't parse the file: {exc}.",
        )
    except Exception as exc:
        return [], HealthResult(
            ok=False,
            summary=f"Unexpected: {type(exc).__name__}: {exc}",
        )
    return events, None


def _ok(events: list, label: str) -> HealthResult:
    """Build the success HealthResult — same summary shape across plugins
    so the user sees consistent "We found X events…" wording everywhere.

    When the parser succeeded but yielded zero events, ok=True with a
    nudge (same convention as apple_podcasts_health for the empty-DB
    case) — the file is shaped correctly but doesn't have anything to
    import yet.
    """
    if not events:
        return HealthResult(
            ok=True,
            summary=(
                f"{label} parsed cleanly, but the first few rows didn't "
                f"produce any events. The file may be empty or filtered out."
            ),
        )
    return HealthResult(
        ok=True,
        summary=(
            f"{label} parsed cleanly. Showing the first "
            f"{len(events)} event{'s' if len(events) != 1 else ''}."
        ),
        preview=_events_to_preview(events),
    )


def netflix_health_check(ctx) -> HealthResult:
    """Resolve the ViewingActivity.csv path and preview the first 3 events."""
    path, err = _resolve(ctx, label="Netflix CSV")
    if err is not None:
        return err
    events, err = _try_parse(netflix_importer.parse_auto, path)
    if err is not None:
        return err
    return _ok(events, "Netflix CSV")


def spotify_extended_health_check(ctx) -> HealthResult:
    """Resolve the Spotify Extended export zip and preview the first 3 events."""
    path, err = _resolve(ctx, label="Spotify export zip")
    if err is not None:
        return err
    events, err = _try_parse(spotify_importer.parse_extended_zip, path)
    if err is not None:
        return err
    return _ok(events, "Spotify export")


def youtube_health_check(ctx) -> HealthResult:
    """Resolve the watch-history.json path and preview the first 3 events."""
    path, err = _resolve(ctx, label="YouTube watch-history.json")
    if err is not None:
        return err
    events, err = _try_parse(youtube_importer.parse_takeout_json, path)
    if err is not None:
        return err
    return _ok(events, "YouTube takeout")


def apple_takeout_health_check(ctx) -> HealthResult:
    """Resolve the Apple TV takeout (file/dir/zip) and preview the first 3 events.

    Doesn't apply the plugin's ``since`` / ``until`` cutoffs — see this
    module's docstring for the rationale.
    """
    path, err = _resolve(ctx, label="Apple TV takeout")
    if err is not None:
        return err
    events, err = _try_parse(apple_takeout_importer.parse_any, path)
    if err is not None:
        return err
    return _ok(events, "Apple TV takeout")


def apple_music_takeout_health_check(ctx) -> HealthResult:
    """Resolve the Apple Music takeout (file/dir/zip) and preview the first
    3 events. Same ``since`` / ``until`` notes as apple_takeout above."""
    path, err = _resolve(ctx, label="Apple Music takeout")
    if err is not None:
        return err
    events, err = _try_parse(apple_music_takeout_importer.parse_any, path)
    if err is not None:
        return err
    return _ok(events, "Apple Music takeout")
