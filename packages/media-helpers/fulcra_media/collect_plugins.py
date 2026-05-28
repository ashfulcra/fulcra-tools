"""Back-compat shim re-exporting the per-plugin objects from ``plugins/``.

This module used to hold all 16 fulcra-collect plugin definitions inline —
~3000 lines of plugin specs, wizard copy, and run functions. Each plugin
now lives in its own module under ``fulcra_media.plugins.<id>`` exposing a
single ``PLUGIN`` constant (matching the convention the sibling
``fulcra-attention`` and ``fulcra-dayone`` packages use).

The re-exports below preserve the old ``<NAME>_PLUGIN`` and ``<NAME>_*_SPEC``
identifiers so the existing test suite and any third-party code importing
``from fulcra_media.collect_plugins import LASTFM_PLUGIN`` keep working.

NOTE on monkeypatching: the per-plugin modules each import their own
``FulcraClient`` / ``_state_load`` / importer / ``library`` bindings, so
tests that need to stub those out must patch the per-plugin module path
(e.g. ``fulcra_media.plugins.lastfm.FulcraClient``) rather than this shim.
Patching this shim has no effect on plugin run functions because they no
longer live here.
"""
from __future__ import annotations

# Plugin objects (preserve historic <NAME>_PLUGIN aliases).
from .plugins.apple_music_takeout import PLUGIN as APPLE_MUSIC_TAKEOUT_PLUGIN
from .plugins.apple_podcasts import PLUGIN as APPLE_PODCASTS_PLUGIN
from .plugins.apple_podcasts_timemachine import PLUGIN as APPLE_PODCASTS_TIMEMACHINE_PLUGIN
from .plugins.apple_takeout import PLUGIN as APPLE_TAKEOUT_PLUGIN
from .plugins.deezer import PLUGIN as DEEZER_PLUGIN
from .plugins.generic_csv import PLUGIN as GENERIC_CSV_PLUGIN
from .plugins.generic_rss import PLUGIN as GENERIC_RSS_PLUGIN
from .plugins.goodreads import PLUGIN as GOODREADS_PLUGIN
from .plugins.lastfm import PLUGIN as LASTFM_PLUGIN
from .plugins.letterboxd import PLUGIN as LETTERBOXD_PLUGIN
from .plugins.media_webhook import PLUGIN as MEDIA_WEBHOOK_PLUGIN
from .plugins.netflix import PLUGIN as NETFLIX_PLUGIN
from .plugins.spotify_extended import PLUGIN as SPOTIFY_EXTENDED_PLUGIN
from .plugins.spotify_ifttt import PLUGIN as SPOTIFY_IFTTT_PLUGIN
from .plugins.trakt import PLUGIN as TRAKT_PLUGIN
from .plugins.youtube import PLUGIN as YOUTUBE_PLUGIN

# Annotation-definition spec dicts. Tests assert their exact shape; keep the
# old aliases exported so the test imports don't need to know about the
# per-plugin module split.
from .plugins.apple_music_takeout import APPLE_MUSIC_TAKEOUT_LISTENED_SPEC
from .plugins.apple_podcasts import APPLE_PODCASTS_LISTENED_SPEC
from .plugins.apple_podcasts_timemachine import APPLE_PODCASTS_TIMEMACHINE_LISTENED_SPEC
from .plugins.apple_takeout import APPLE_TAKEOUT_WATCHED_SPEC
from .plugins.deezer import DEEZER_LISTENED_SPEC
from .plugins.goodreads import GOODREADS_READ_SPEC
from .plugins.lastfm import LASTFM_LISTENED_SPEC
from .plugins.letterboxd import LETTERBOXD_WATCHED_SPEC
from .plugins.media_webhook import MEDIA_WEBHOOK_WATCHED_SPEC
from .plugins.netflix import NETFLIX_WATCHED_SPEC
from .plugins.spotify_extended import SPOTIFY_EXTENDED_LISTENED_SPEC
from .plugins.spotify_ifttt import SPOTIFY_IFTTT_LISTENED_SPEC
from .plugins.trakt import TRAKT_WATCHED_SPEC
from .plugins.youtube import YOUTUBE_WATCHED_SPEC

# Also re-export the Apple Podcasts permission helper — fulcra-collect's
# generic plugin runner used to look it up here when wiring permission_check;
# the new home is plugins.apple_podcasts but we keep the alias for any
# external code that imported it directly.
from .plugins.apple_podcasts import apple_podcasts_permission_check

__all__ = [
    # Plugins
    "APPLE_MUSIC_TAKEOUT_PLUGIN",
    "APPLE_PODCASTS_PLUGIN",
    "APPLE_PODCASTS_TIMEMACHINE_PLUGIN",
    "APPLE_TAKEOUT_PLUGIN",
    "DEEZER_PLUGIN",
    "GENERIC_CSV_PLUGIN",
    "GENERIC_RSS_PLUGIN",
    "GOODREADS_PLUGIN",
    "LASTFM_PLUGIN",
    "LETTERBOXD_PLUGIN",
    "MEDIA_WEBHOOK_PLUGIN",
    "NETFLIX_PLUGIN",
    "SPOTIFY_EXTENDED_PLUGIN",
    "SPOTIFY_IFTTT_PLUGIN",
    "TRAKT_PLUGIN",
    "YOUTUBE_PLUGIN",
    # Spec dicts
    "APPLE_MUSIC_TAKEOUT_LISTENED_SPEC",
    "APPLE_PODCASTS_LISTENED_SPEC",
    "APPLE_PODCASTS_TIMEMACHINE_LISTENED_SPEC",
    "APPLE_TAKEOUT_WATCHED_SPEC",
    "DEEZER_LISTENED_SPEC",
    "GOODREADS_READ_SPEC",
    "LASTFM_LISTENED_SPEC",
    "LETTERBOXD_WATCHED_SPEC",
    "MEDIA_WEBHOOK_WATCHED_SPEC",
    "NETFLIX_WATCHED_SPEC",
    "SPOTIFY_EXTENDED_LISTENED_SPEC",
    "SPOTIFY_IFTTT_LISTENED_SPEC",
    "TRAKT_WATCHED_SPEC",
    "YOUTUBE_WATCHED_SPEC",
    # Helpers
    "apple_podcasts_permission_check",
]
