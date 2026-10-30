"""Spotify IFTTT/GDrive backfill — manual file plugin (one-shot backfill tool).

NOTE (2026-05-24): this plugin is intentionally NOT registered as a
``fulcra_collect.plugins`` entry-point in ``pyproject.toml``. It was a
one-time backfill tool and does not belong in the default menubar plugin
list. The code remains here for manual backfill use via
``uv run --package fulcra-media-helpers ...``.
"""
from __future__ import annotations

from zoneinfo import ZoneInfo

from fulcra_collect.plugin import Plugin, RunContext

from .. import library
from ..collect_v1 import media_destinations, media_pending, media_run
from ..fulcra import FulcraClient
from ..importers import spotify_ifttt as spotify_ifttt_importer
from ..state import load as _state_load
from ._common import DURATION_SPEC, import_events, resolve_path


# Legacy spec alias retained for imports; v1 setup uses media_destinations.
SPOTIFY_IFTTT_LISTENED_SPEC: dict = DURATION_SPEC


@media_run
def _run_spotify_ifttt(ctx: RunContext) -> None:

    resolved = resolve_path(ctx, library)
    tz = ZoneInfo(ctx.config.get("tz", "UTC"))
    events = list(spotify_ifttt_importer.parse_ifttt_zip(resolved, tz=tz))
    import_events(
        ctx, events, "spotify",
        fulcra_client_cls=FulcraClient,
        state_load=_state_load,
    )


PLUGIN = Plugin(
    id="spotify-ifttt",
    name="Spotify IFTTT/GDrive backfill",
    kind="manual",
    collect_mode="historical",
    run=_run_spotify_ifttt,
    record_data_types=media_destinations("listened"),
    record_pending=media_pending,
    default_interval=None,
    canonical_definition_name="Listened",
    required_credentials=(),
)
