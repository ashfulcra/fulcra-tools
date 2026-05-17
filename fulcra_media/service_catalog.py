"""Data-driven catalog of media services + their integration pathways.

Sourced from research at docs/superpowers/research/2026-05-17-media-service-pathways.md.
The `fulcra-media setup` wizard reads this catalog to drive its interactive
picker — new services entered here automatically appear in setup.

Field meanings:
    key:        stable identifier (snake-case)
    label:      user-facing display name
    category:   "music" | "video" | "podcasts" | "books" | "self-hosted"
    rank:       1=best, 4=worst for this category (sort order in setup)
    pathway:    "api" | "webhook" | "gdpr" | "pipedream" | "ifttt" |
                "local-db" | "scrape" | "rss" | "via-lastfm" | "via-trakt" |
                "via-generic-csv" | "dead"
    import_cmd: subcommand under `fulcra-media import` (None = no importer yet)
    wizard:     subcommand under `fulcra-media wizard` (None = no wizard yet)
    blurb:      one-line description shown in setup
    available:  True if an importer ships today; False = planned/manual route
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ServiceEntry:
    key: str
    label: str
    category: str
    rank: int
    pathway: str
    import_cmd: str | None
    wizard: str | None
    blurb: str
    available: bool = True


SERVICES: list[ServiceEntry] = [
    # ---- Music ----
    ServiceEntry(
        key="lastfm", label="Last.fm", category="music", rank=1, pathway="api",
        import_cmd="lastfm", wizard="lastfm",
        blurb="Universal scrobble aggregator. Covers Spotify/Apple Music/Tidal/"
              "YouTube Music/Amazon Music/SoundCloud/Pandora via in-app or "
              "Web Scrobbler. Username + API key, no OAuth.",
    ),
    ServiceEntry(
        key="deezer", label="Deezer", category="music", rank=2, pathway="api",
        import_cmd="deezer", wizard="deezer",
        blurb="Direct OAuth API with real user.history endpoint. Cleaner than "
              "Spotify (no per-call 50-track cap). Manual token mint via "
              "developers.deezer.com.",
    ),
    ServiceEntry(
        key="spotify-extended", label="Spotify Extended (GDPR)",
        category="music", rank=3, pathway="gdpr",
        import_cmd="spotify-extended", wizard="spotify",
        blurb="Spotify's official 'Extended streaming history' — request via "
              "privacy settings. Full history; one-shot, ~3-5 days to deliver.",
    ),
    ServiceEntry(
        key="spotify-ifttt", label="Spotify → IFTTT → Google Drive (legacy)",
        category="music", rank=4, pathway="ifttt",
        import_cmd="spotify-ifttt", wizard="spotify-ifttt",
        blurb="If you wired the legacy IFTTT applet years ago, your back history "
              "is in Drive as xlsx. This importer handles the multi-file overlap.",
    ),
    ServiceEntry(
        key="generic-csv-music", label="Generic CSV (music)",
        category="music", rank=5, pathway="via-generic-csv",
        import_cmd="generic-csv", wizard="ifttt",
        blurb="Bring any CSV (IFTTT/Pipedream/hand-rolled) with timestamp + "
              "track + artist columns. See `wizard ifttt` or `wizard pipedream`.",
    ),

    # ---- Video / TV ----
    ServiceEntry(
        key="trakt", label="Trakt", category="video", rank=1, pathway="api",
        import_cmd="trakt", wizard="trakt",
        blurb="Direct API. Also catches Apple TV+ via the Universal Trakt "
              "Scrobbler browser extension. Cluster handling auto-detects "
              "signup-day backfill artifacts.",
    ),
    ServiceEntry(
        key="netflix", label="Netflix", category="video", rank=2, pathway="gdpr",
        import_cmd="netflix", wizard="netflix",
        blurb="Slim CSV (in-app per-profile download) or full GDPR export "
              "(10-column rich variant with timestamps + durations).",
    ),
    ServiceEntry(
        key="apple-takeout", label="Apple TV (privacy export)",
        category="video", rank=3, pathway="gdpr",
        import_cmd="apple-takeout", wizard="apple-takeout",
        blurb="privacy.apple.com → request data → Apple TV Playback Activity "
              "CSV. EU/UK/JP users can schedule recurring exports.",
    ),
    ServiceEntry(
        key="youtube", label="YouTube (Google Takeout)",
        category="video", rank=5, pathway="gdpr",
        import_cmd="youtube", wizard="youtube",
        blurb="Google Takeout watch-history.json. Supports scheduled exports "
              "every 2 months, so works for ongoing capture too. No duration "
              "data — 1-second sentinel.",
    ),
    ServiceEntry(
        key="generic-csv-video", label="Generic CSV (video)",
        category="video", rank=6, pathway="via-generic-csv",
        import_cmd="generic-csv", wizard="ifttt",
        blurb="For everything else (Hulu/Disney+/Max/Prime Video/Peacock) "
              "— privacy request, then convert to CSV and import.",
    ),

    # ---- Podcasts ----
    ServiceEntry(
        key="apple-podcasts", label="Apple Podcasts", category="podcasts",
        rank=1, pathway="local-db",
        import_cmd="apple-podcasts", wizard="apple-podcasts",
        blurb="Reads the macOS Podcasts app's MTLibrary.sqlite. Time Machine "
              "subcommand recovers history from older snapshots.",
    ),
    ServiceEntry(
        key="spotify-podcasts", label="Spotify (podcasts via Extended)",
        category="podcasts", rank=2, pathway="gdpr",
        import_cmd="spotify-extended", wizard="spotify",
        blurb="Spotify Extended Streaming History includes podcast episode "
              "plays alongside music.",
    ),

    # ---- Self-hosted ----
    ServiceEntry(
        key="plex", label="Plex", category="self-hosted", rank=1,
        pathway="webhook", import_cmd="webhook", wizard="plex",
        blurb="Long-running webhook receiver. Plex (Pass) or Tautulli POSTs "
              "media.scrobble events here; we translate and ingest.",
        available=True,
    ),
    ServiceEntry(
        key="jellyfin", label="Jellyfin", category="self-hosted",
        rank=2, pathway="webhook", import_cmd="webhook", wizard="jellyfin",
        blurb="Long-running webhook receiver. jellyfin-plugin-webhook POSTs "
              "PlaybackStop events here; we translate and ingest.",
        available=True,
    ),

    # ---- Physical activity / workouts ----
    ServiceEntry(
        key="strava", label="Strava", category="activity", rank=1, pathway="api",
        import_cmd="strava", wizard="strava",
        blurb="Direct OAuth API. Workouts (runs, rides, swims, ...) imported as "
              "Activity events. Webhook subscription available for real-time push.",
    ),

    # ---- Books / reading (future) ----
    ServiceEntry(
        key="letterboxd", label="Letterboxd (RSS)", category="video", rank=4,
        pathway="rss", import_cmd="letterboxd", wizard="letterboxd",
        blurb="Public diary RSS feed; polls hourly. API is closed beta so we "
              "scrape /<user>/rss/ — fingerprints films for cross-source dedup.",
        available=True,
    ),
    ServiceEntry(
        key="goodreads", label="Goodreads", category="books", rank=1,
        pathway="rss", import_cmd="goodreads", wizard="goodreads",
        blurb="RSS feed of the 'read' shelf. API was killed in Dec 2020, "
              "but every public shelf still has a stable RSS feed. "
              "Polls /review/list_rss/<user_id>?shelf=read.",
        available=True,
    ),
]


def services_for_category(category: str) -> list[ServiceEntry]:
    """Return services in `category`, sorted by rank then label."""
    return sorted(
        (s for s in SERVICES if s.category == category),
        key=lambda s: (s.rank, s.label),
    )


def categories() -> list[str]:
    """Distinct ordered category list."""
    seen: list[str] = []
    for s in SERVICES:
        if s.category not in seen:
            seen.append(s.category)
    return seen


def get(key: str) -> ServiceEntry | None:
    """Look up a single service by key."""
    for s in SERVICES:
        if s.key == key:
            return s
    return None
