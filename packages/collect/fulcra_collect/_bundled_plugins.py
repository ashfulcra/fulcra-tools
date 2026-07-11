"""Static plugin manifest for the frozen (py2app) build.

A py2app freeze strips the per-package ``.dist-info`` metadata that
``importlib.metadata.entry_points`` reads, so entry-point discovery
returns nothing inside the bundle. This manifest is the fallback:
``registry.discover()`` imports these directly when entry points come
back empty.

KEEP IN SYNC with every workspace pyproject's
``[project.entry-points."fulcra_collect.plugins"]`` table — the test
``test_manifest_matches_entry_points`` fails CI if this drifts.
"""
from __future__ import annotations

# (plugin_id, "import.module:attr")
BUNDLED_PLUGINS: tuple[tuple[str, str], ...] = (
    ("lastfm", "fulcra_media.plugins.lastfm:PLUGIN"),
    ("deezer", "fulcra_media.plugins.deezer:PLUGIN"),
    ("trakt", "fulcra_media.plugins.trakt:PLUGIN"),
    ("netflix", "fulcra_media.plugins.netflix:PLUGIN"),
    ("spotify-extended", "fulcra_media.plugins.spotify_extended:PLUGIN"),
    ("youtube", "fulcra_media.plugins.youtube:PLUGIN"),
    ("apple-takeout", "fulcra_media.plugins.apple_takeout:PLUGIN"),
    ("apple-music-takeout", "fulcra_media.plugins.apple_music_takeout:PLUGIN"),
    ("generic-rss", "fulcra_media.plugins.generic_rss:PLUGIN"),
    ("letterboxd", "fulcra_media.plugins.letterboxd:PLUGIN"),
    ("goodreads", "fulcra_media.plugins.goodreads:PLUGIN"),
    ("apple-podcasts", "fulcra_media.plugins.apple_podcasts:PLUGIN"),
    ("apple-podcasts-timemachine",
     "fulcra_media.plugins.apple_podcasts_timemachine:PLUGIN"),
    ("apple-tv", "fulcra_media.plugins.apple_tv:PLUGIN"),
    ("generic-csv", "fulcra_media.plugins.generic_csv:PLUGIN"),
    ("media-webhook", "fulcra_media.plugins.media_webhook:PLUGIN"),
    ("dayone", "fulcra_dayone.collect_plugin:PLUGIN"),
    ("attention-relay", "fulcra_attention.collect_plugin:PLUGIN"),
    ("gmail", "fulcra_gmail.collect_plugin:PLUGIN"),
)
