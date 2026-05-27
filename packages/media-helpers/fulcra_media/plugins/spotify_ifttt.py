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
from ..fulcra import FulcraClient
from ..importers import spotify_ifttt as spotify_ifttt_importer
from ..state import DEFAULT_PATH as STATE_PATH
from ..state import load as _state_load
from ..state import save as _state_save
from ._common import DURATION_SPEC, ensure_media_def, import_events, resolve_path


# Identical structure to LASTFM_LISTENED_SPEC and SPOTIFY_EXTENDED_LISTENED_SPEC.
SPOTIFY_IFTTT_LISTENED_SPEC: dict = DURATION_SPEC


def _run_spotify_ifttt(ctx: RunContext) -> None:
    # Ensure the "Listened" annotation definition is known before importing.
    # On a fresh install (machine 2) the media state file may have no
    # listened_definition_id because bootstrap was never run on this machine.
    # The shared resolver adopts Machine 1's existing "Listened" definition
    # rather than creating a duplicate.
    media_state = _state_load(STATE_PATH)
    ensure_media_def(ctx, media_state, attr="listened_definition_id",
                     spec=SPOTIFY_IFTTT_LISTENED_SPEC,
                     canonical_name="Listened",
                     state_save=_state_save)

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
    run=_run_spotify_ifttt,
    default_interval=None,
    canonical_definition_name="Listened",
    required_credentials=(),
)
