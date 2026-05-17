# Four Importers + Cross-Source Dedup — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Checkbox syntax for steps.

**Goal:** Ship Trakt, Apple Podcasts, Spotify Extended, and Apple Data Privacy takeout importers, each producing per-event idempotent annotations with rich `external_ids` content metadata that supports cross-source dedup at query time.

**Architecture:** Same pipeline as Netflix — each importer produces `NormalizedEvent` instances; `FulcraClient.run_import` handles ingest. Each event carries a new `content_fingerprint` external_id (normalized `tv:<show-slug>:s<NN>e<NN>` or `movie:<title-slug>:<year>` or `music:<artist>-<track>` or `podcast:<show-slug>:<episode-guid>`) so consumers can group cross-source duplicates.

**Tech stack:** Python 3.11+, Click 8.x, httpx, dateparser, pytest. Add `sqlite3` (stdlib) for Apple Podcasts. Add `zipfile` (stdlib) for Spotify Extended and Apple takeout.

**Spec reference:** `docs/superpowers/specs/2026-05-16-fulcra-media-helpers-design.md` §§ 3.1, 3.3, 3.4, 3.5.

---

## File structure additions

```
fulcra_media/
  importers/
    trakt.py            # live API; device-flow auth at ~/.config/fulcra-media/trakt.json
    apple_podcasts.py   # macOS MTLibrary.sqlite reader
    spotify.py          # GDPR Extended Streaming History zip reader
    apple_takeout.py    # Apple Data Privacy export Playback Activity.csv reader
  wizards/
    trakt.py
    apple_podcasts.py
    spotify.py
    apple_takeout.py
tests/
  fixtures/
    trakt_history_sample.json           # 6-row synthetic (1 movie + 4 episodes + 1 cluster sample)
    apple_podcasts_mtlibrary.sqlite     # synthetic SQLite with ZMTEPISODE + ZMTPODCAST
    spotify_extended_sample.zip         # zip of 1 Streaming_History_Audio_*.json with 5 entries
    apple_takeout_playback_sample.csv   # 6-row synthetic (movies + episodes + trailer)
  test_trakt_importer.py
  test_apple_podcasts_importer.py
  test_spotify_importer.py
  test_apple_takeout_importer.py
  test_trakt_wizard.py
  test_content_fingerprint.py           # shared fingerprint helper tests
```

`fulcra_media/importers/base.py` gains a `content_fingerprint(category, kind, **fields) -> str` helper.

---

## Conventions / shared rules

- **TDD strictly.** Each task is RED → GREEN → commit.
- **Run tests:** `.venv/bin/pytest -q`.
- **Content fingerprint format:** `f"{category}:{kind}:{slug}"` — see Task 1.
- **Cross-source dedup is NOT automatic.** Per spec §5: each importer writes its own annotations. The `content_fingerprint` lives in `external_ids` for consumer-side grouping.
- **Trakt creds** already at `~/.config/fulcra-media/trakt.json` (client_id, client_secret, access_token, refresh_token). Importer reads from there; never commits creds.

---

## Task 1: `content_fingerprint` helper

**Files:**
- Modify: `fulcra_media/importers/base.py`
- Create: `tests/test_content_fingerprint.py`

A pure function that produces a stable identifier for a media item, independent of time/source. Lets consumers group cross-source duplicates.

Forms:
- TV episode: `"tv:<show-slug>:s<season:02d>e<episode:02d>"`
- Movie: `"movie:<title-slug>[:y<year>]"`
- Music track: `"music:<artist-slug>:<track-slug>"`
- Podcast episode: `"podcast:<show-slug>:<episode-slug-or-guid>"`

The slugifier lowercases, strips non-alphanumerics-or-spaces, collapses spaces to hyphens.

- [ ] **Step 1: Write the failing tests**

In `tests/test_content_fingerprint.py`:

```python
import pytest

from fulcra_media.importers.base import content_fingerprint, _slugify


def test_slugify_basic():
    assert _slugify("Stranger Things") == "stranger-things"
    assert _slugify("Dune: Part Two") == "dune-part-two"
    assert _slugify("  Multiple    Spaces  ") == "multiple-spaces"


def test_slugify_strips_special_chars():
    assert _slugify("Should I Marry A Murderer?!") == "should-i-marry-a-murderer"
    assert _slugify("Sci-Fi & Fantasy") == "sci-fi-fantasy"


def test_fingerprint_tv_episode():
    fp = content_fingerprint("tv", show="Severance", season=2, episode=1)
    assert fp == "tv:severance:s02e01"


def test_fingerprint_movie_with_year():
    fp = content_fingerprint("movie", title="Dune: Part Two", year=2024)
    assert fp == "movie:dune-part-two:y2024"


def test_fingerprint_movie_no_year():
    fp = content_fingerprint("movie", title="Dune: Part Two")
    assert fp == "movie:dune-part-two"


def test_fingerprint_music_track():
    fp = content_fingerprint("music", artist="Daft Punk", track="Get Lucky")
    assert fp == "music:daft-punk:get-lucky"


def test_fingerprint_podcast_episode_by_guid():
    fp = content_fingerprint("podcast", show="Reply All", guid="abc-123")
    assert fp == "podcast:reply-all:abc-123"


def test_fingerprint_podcast_episode_by_title():
    fp = content_fingerprint("podcast", show="Reply All", title="The Crime Machine, Part I")
    assert fp == "podcast:reply-all:the-crime-machine-part-i"


def test_fingerprint_unknown_kind_raises():
    with pytest.raises(ValueError):
        content_fingerprint("nope", title="x")
```

- [ ] **Step 2: Run to verify RED**

`.venv/bin/pytest -v tests/test_content_fingerprint.py` → ImportError.

- [ ] **Step 3: Implement**

Append to `fulcra_media/importers/base.py`:

```python
import re


_SLUG_RE = re.compile(r"[^a-z0-9 ]+")


def _slugify(value: str) -> str:
    """Lowercase, strip non-alphanumeric (except spaces), collapse spaces to hyphens."""
    s = _SLUG_RE.sub("", (value or "").lower())
    return "-".join(s.split())


def content_fingerprint(kind: str, **fields) -> str:
    """Build a stable cross-source content identifier.

    kind="tv":      requires show, season:int, episode:int
    kind="movie":   requires title; optional year
    kind="music":   requires artist, track
    kind="podcast": requires show; one of (guid, title)
    """
    if kind == "tv":
        return f"tv:{_slugify(fields['show'])}:s{fields['season']:02d}e{fields['episode']:02d}"
    if kind == "movie":
        base = f"movie:{_slugify(fields['title'])}"
        year = fields.get("year")
        return f"{base}:y{year}" if year else base
    if kind == "music":
        return f"music:{_slugify(fields['artist'])}:{_slugify(fields['track'])}"
    if kind == "podcast":
        ep = fields.get("guid") or fields.get("title")
        if ep is None:
            raise ValueError("podcast fingerprint needs guid or title")
        return f"podcast:{_slugify(fields['show'])}:{_slugify(str(ep))}"
    raise ValueError(f"unknown fingerprint kind: {kind!r}")
```

- [ ] **Step 4: Run GREEN**

`.venv/bin/pytest -v tests/test_content_fingerprint.py` → 9 passed.

- [ ] **Step 5: Commit**

```bash
git add fulcra_media/importers/base.py tests/test_content_fingerprint.py
git commit -m "$(cat <<'EOF'
feat(importers): content_fingerprint helper for cross-source dedup

A stable identifier for a media item independent of timestamp or source.
Consumers (dashboards, query layers) can group events by fingerprint
to surface cross-source duplicates without losing them — the spec
forbids automatic cross-source merging, but exposes the data for
opt-in client-side dedup.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Retrofit Netflix importers with content_fingerprint

**Files:**
- Modify: `fulcra_media/importers/netflix.py`
- Modify: `tests/test_netflix_importer.py`

Both `parse_slim` and `parse_rich` should populate `external_ids.content_fingerprint`. Slim has limited info (title only, no year/season/episode parsing) so fingerprints are best-effort.

- [ ] **Step 1: Add tests**

Append to `tests/test_netflix_importer.py`:

```python
def test_parse_slim_includes_content_fingerprint():
    events = list(parse_slim(FIXTURE))
    # Movie -- "Movie One" with no colon
    movie = next(e for e in events if e.title == "Movie One")
    assert movie.external_ids["content_fingerprint"] == "movie:movie-one"
    # Episode -- "Show A: Season 1: Episode 1"
    ep = next(e for e in events if e.note == "Show A: Season 1: Episode 1")
    assert ep.external_ids["content_fingerprint"].startswith("tv:show-a:")


def test_parse_rich_includes_content_fingerprint():
    events = list(parse_rich(RICH_FIXTURE))
    # Movie with colon subtitle
    movie = next(e for e in events if e.title == "Dune: Part Two")
    assert movie.external_ids["content_fingerprint"] == "movie:dune-part-two"
    # Episode "Severance: Season 2: The We We Are"
    ep = next(e for e in events if "We We Are" in e.note)
    assert ep.external_ids["content_fingerprint"] == "tv:severance:s02e01"
```

- [ ] **Step 2: RED**

`.venv/bin/pytest -v tests/test_netflix_importer.py -k fingerprint` → 2 fail.

- [ ] **Step 3: Implement**

In `parse_slim` (inside the for-row loop, before the `yield`), compute fingerprint:

```python
            # Best-effort fingerprint from slim's limited info
            from .base import content_fingerprint
            if ":" in raw_title:
                # episode-shape: first colon-separated part is show
                show = note.split(":", 1)[0].strip()
                # Look for "Season N: Episode M" anywhere
                import re as _re
                m_season = _re.search(r"Season\s+(\d+)", raw_title)
                m_episode = _re.search(r"Episode\s+(\d+)", raw_title)
                if m_season and m_episode:
                    fp = content_fingerprint("tv", show=show, season=int(m_season.group(1)), episode=int(m_episode.group(1)))
                else:
                    # Can't parse a clean season/episode — fall back to movie-style
                    fp = content_fingerprint("movie", title=note)
            else:
                fp = content_fingerprint("movie", title=raw_title)
            external_ids = {
                "time_estimated": True,
                "point_in_time": True,
                "occurrence_index": idx,
                "raw_date": date_str,
                "content_fingerprint": fp,
            }
```

Replace the existing `external_ids=...` dict with the assignment above and use `external_ids=external_ids` in the yield.

In `parse_rich`, similarly add fingerprint to its external_ids. Use `_extract_title_rich` output to detect episode vs movie:

```python
            from .base import content_fingerprint
            note, title = _extract_title_rich(raw_title)
            # Detect episode shape
            import re as _re
            m_season = _re.search(r"Season\s+(\d+)", raw_title)
            m_episode = _re.search(r"Episode\s+(\d+)", raw_title)
            if any(marker in raw_title for marker in _EPISODE_MARKERS) and m_season and m_episode:
                fp = content_fingerprint("tv", show=title, season=int(m_season.group(1)), episode=int(m_episode.group(1)))
            else:
                fp = content_fingerprint("movie", title=title)
            external_ids = {
                "profile": profile,
                "device_type": (row.get("Device Type") or "").strip(),
                "country": (row.get("Country") or "").strip(),
                "bookmark": (row.get("Bookmark") or "").strip(),
                "content_fingerprint": fp,
            }
```

- [ ] **Step 4: GREEN + full suite**

`.venv/bin/pytest -q` → all green.

- [ ] **Step 5: Commit**

```bash
git add fulcra_media/importers/netflix.py tests/test_netflix_importer.py
git commit -m "$(cat <<'EOF'
feat(netflix): emit content_fingerprint in external_ids

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Trakt importer — auth helpers + history fetch

**Files:**
- Create: `fulcra_media/importers/trakt.py`
- Create: `tests/test_trakt_auth.py`
- Create: `tests/fixtures/trakt_history_sample.json`

Reads creds from `~/.config/fulcra-media/trakt.json`. Refreshes access token if expired. Fetches `/sync/history?extended=full&limit=1000` paginating until empty.

- [ ] **Step 1: Write the sample fixture**

`tests/fixtures/trakt_history_sample.json` — six items: 1 movie, 4 episodes (2 in a synthetic cluster at 2026-05-15T19:41:00.000Z, 2 clean), 1 rewatch:

```json
[
  {"id": 100001, "watched_at": "2026-05-12T20:30:00.000Z", "action": "scrobble", "type": "episode",
    "episode": {"season": 2, "number": 1, "title": "The We We Are",
      "ids": {"trakt": 73482, "imdb": "tt5790298", "tmdb": 1130149, "tvdb": null}, "runtime": 51},
    "show": {"title": "Severance", "year": 2022,
      "ids": {"trakt": 2243, "slug": "severance", "imdb": "tt11280740", "tmdb": 95396}}
  },
  {"id": 100002, "watched_at": "2026-05-10T21:00:00.000Z", "action": "watch", "type": "movie",
    "movie": {"title": "Dune: Part Two", "year": 2024, "runtime": 165,
      "ids": {"trakt": 71938, "slug": "dune-part-two-2024", "imdb": "tt15239678", "tmdb": 693134}}
  },
  {"id": 100003, "watched_at": "2026-05-15T19:41:00.000Z", "action": "watch", "type": "episode",
    "episode": {"season": 1, "number": 2, "title": "Episode Two",
      "ids": {"trakt": 999001, "imdb": "tt0000001", "tmdb": 1}, "runtime": 45},
    "show": {"title": "Star Wars Resistance", "year": 2018,
      "ids": {"trakt": 100, "slug": "star-wars-resistance", "imdb": null, "tmdb": null}}
  },
  {"id": 100004, "watched_at": "2026-05-15T19:41:00.000Z", "action": "watch", "type": "episode",
    "episode": {"season": 1, "number": 3, "title": "Episode Three",
      "ids": {"trakt": 999002, "imdb": "tt0000002", "tmdb": 2}, "runtime": 45},
    "show": {"title": "Star Wars Resistance", "year": 2018,
      "ids": {"trakt": 100, "slug": "star-wars-resistance", "imdb": null, "tmdb": null}}
  },
  {"id": 100005, "watched_at": "2026-05-15T19:41:00.000Z", "action": "watch", "type": "episode",
    "episode": {"season": 1, "number": 4, "title": "Episode Four",
      "ids": {"trakt": 999003, "imdb": "tt0000003", "tmdb": 3}, "runtime": 45},
    "show": {"title": "Star Wars Resistance", "year": 2018,
      "ids": {"trakt": 100, "slug": "star-wars-resistance", "imdb": null, "tmdb": null}}
  },
  {"id": 100006, "watched_at": "2026-04-01T20:00:00.000Z", "action": "checkin", "type": "movie",
    "movie": {"title": "Dune: Part Two", "year": 2024, "runtime": 165,
      "ids": {"trakt": 71938, "slug": "dune-part-two-2024", "imdb": "tt15239678", "tmdb": 693134}}
  }
]
```

Note: 3 items in the synthetic cluster at 2026-05-15T19:41 (below the ≥5 threshold) so for tests we need a smaller threshold OR use a wider cluster — choose to test cluster detection by lowering the threshold via a parameter. Plan: make cluster threshold a parameter (default 5) so tests can use 3.

- [ ] **Step 2: Write the failing tests**

In `tests/test_trakt_auth.py`:

```python
import json
from pathlib import Path

import httpx
import pytest

from fulcra_media.importers.trakt import (
    TraktAuth, load_creds, save_creds,
)


def test_load_creds_returns_dict(tmp_path: Path, mocker):
    p = tmp_path / "trakt.json"
    p.write_text(json.dumps({"client_id": "cid", "client_secret": "csec", "access_token": "at", "refresh_token": "rt", "expires_in": 86400, "created_at": 9999}))
    mocker.patch("fulcra_media.importers.trakt.CREDS_PATH", p)
    c = load_creds()
    assert c["client_id"] == "cid"
    assert c["access_token"] == "at"


def test_save_creds_round_trip(tmp_path: Path, mocker):
    p = tmp_path / "trakt.json"
    mocker.patch("fulcra_media.importers.trakt.CREDS_PATH", p)
    save_creds({"client_id": "cid", "client_secret": "csec", "access_token": "x", "refresh_token": "y", "expires_in": 86400, "created_at": 1})
    assert json.loads(p.read_text())["access_token"] == "x"


def test_auth_headers(tmp_path: Path, mocker):
    p = tmp_path / "trakt.json"
    p.write_text(json.dumps({"client_id": "cid", "client_secret": "csec", "access_token": "tok", "refresh_token": "rt", "expires_in": 86400, "created_at": 9999999999}))
    mocker.patch("fulcra_media.importers.trakt.CREDS_PATH", p)
    a = TraktAuth()
    h = a.headers()
    assert h["Authorization"] == "Bearer tok"
    assert h["trakt-api-version"] == "2"
    assert h["trakt-api-key"] == "cid"


def test_refresh_when_expired(tmp_path: Path, mocker):
    """If created_at + expires_in < now, perform a refresh and update creds."""
    import time
    p = tmp_path / "trakt.json"
    # token expired 100s ago
    p.write_text(json.dumps({"client_id": "cid", "client_secret": "csec", "access_token": "old", "refresh_token": "rt", "expires_in": 100, "created_at": int(time.time()) - 200}))
    mocker.patch("fulcra_media.importers.trakt.CREDS_PATH", p)

    def fake_post(url, json=None, timeout=None):
        assert "/oauth/token" in url
        assert json["refresh_token"] == "rt"
        return httpx.Response(200, content=httpx.Response(200, json={"access_token":"new","refresh_token":"rt2","expires_in":86400,"created_at":int(time.time())}).content)
    mocker.patch("httpx.post", side_effect=fake_post)

    a = TraktAuth()
    a.headers()  # triggers refresh internally
    c = json.loads(p.read_text())
    assert c["access_token"] == "new"
    assert c["refresh_token"] == "rt2"
```

- [ ] **Step 3: RED** — `pytest -v tests/test_trakt_auth.py`.

- [ ] **Step 4: Implement `fulcra_media/importers/trakt.py` auth surface**

```python
"""Trakt history importer.

Auth: device flow handled out-of-band (see `fulcra-media auth trakt` once we
add it). This module reads creds from ~/.config/fulcra-media/trakt.json,
refreshes the access token when expired, and exposes a fetch_history()
iterator that paginates /sync/history?extended=full&limit=1000.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Iterator
from datetime import datetime, timezone, timedelta
from pathlib import Path

import httpx

from .base import NormalizedEvent, content_fingerprint

CREDS_PATH = Path(os.path.expanduser("~/.config/fulcra-media/trakt.json"))
TRAKT_BASE = "https://api.trakt.tv"


def load_creds() -> dict:
    return json.loads(CREDS_PATH.read_text())


def save_creds(creds: dict) -> None:
    CREDS_PATH.parent.mkdir(parents=True, exist_ok=True)
    CREDS_PATH.write_text(json.dumps(creds, indent=2, sort_keys=True))
    os.chmod(CREDS_PATH, 0o600)


class TraktAuth:
    def __init__(self) -> None:
        self._creds = load_creds()

    def _is_expired(self, slack_seconds: int = 60) -> bool:
        c = self._creds
        return (c["created_at"] + c["expires_in"] - slack_seconds) < int(time.time())

    def _refresh(self) -> None:
        c = self._creds
        r = httpx.post(
            f"{TRAKT_BASE}/oauth/token",
            json={
                "refresh_token": c["refresh_token"],
                "client_id": c["client_id"],
                "client_secret": c["client_secret"],
                "redirect_uri": "urn:ietf:wg:oauth:2.0:oob",
                "grant_type": "refresh_token",
            },
            timeout=30,
        )
        r.raise_for_status()
        tok = r.json()
        c["access_token"] = tok["access_token"]
        c["refresh_token"] = tok["refresh_token"]
        c["expires_in"] = tok["expires_in"]
        c["created_at"] = tok["created_at"]
        save_creds(c)

    def headers(self) -> dict[str, str]:
        if self._is_expired():
            self._refresh()
        c = self._creds
        return {
            "Authorization": f"Bearer {c['access_token']}",
            "trakt-api-version": "2",
            "trakt-api-key": c["client_id"],
            "Content-Type": "application/json",
        }
```

- [ ] **Step 5: GREEN**

`.venv/bin/pytest -v tests/test_trakt_auth.py` → 4 passed.

- [ ] **Step 6: Commit**

```bash
git add fulcra_media/importers/trakt.py tests/test_trakt_auth.py tests/fixtures/trakt_history_sample.json
git commit -m "$(cat <<'EOF'
feat(trakt): auth surface — load/save creds, expiry detection, refresh

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Trakt — `parse_history` with cluster detection

**Files:**
- Modify: `fulcra_media/importers/trakt.py`
- Create: `tests/test_trakt_importer.py`

Convert raw Trakt history items to NormalizedEvents. Detect clusters (≥N items at exact watched_at), mark them `timestamp_confidence: low`, default confidence by action.

- [ ] **Step 1: Tests**

In `tests/test_trakt_importer.py`:

```python
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from fulcra_media.importers.trakt import normalize_history, detect_clusters
from fulcra_media.importers.base import NormalizedEvent

FIXTURE = Path(__file__).parent / "fixtures" / "trakt_history_sample.json"


def _items():
    return json.loads(FIXTURE.read_text())


def test_detect_clusters_threshold_3():
    """3 items share watched_at=2026-05-15T19:41 — detect with threshold 3."""
    clusters = detect_clusters(_items(), threshold=3)
    assert "2026-05-15T19:41:00.000Z" in clusters
    assert clusters["2026-05-15T19:41:00.000Z"] == 3


def test_normalize_history_emits_one_event_per_item():
    events = list(normalize_history(_items(), cluster_threshold=3))
    assert len(events) == 6


def test_normalize_history_episode_event_shape():
    events = list(normalize_history(_items(), cluster_threshold=3))
    e = next(e for e in events if e.external_ids.get("trakt_history_id") == 100001)
    assert e.importer == "trakt"
    assert e.service == "trakt"
    assert e.category == "watched"
    assert e.note == "Severance S02E01 – The We We Are"
    assert e.title == "Severance"
    assert e.start_time == datetime(2026, 5, 12, 20, 30, 0, tzinfo=timezone.utc)
    # runtime 51 -> end = start + 51min
    assert (e.end_time - e.start_time).total_seconds() == 51 * 60
    assert e.timestamp_confidence == "high"   # action=scrobble
    assert e.external_ids["trakt_action"] == "scrobble"
    assert e.external_ids["content_fingerprint"] == "tv:severance:s02e01"
    assert e.external_ids["imdb"] == "tt5790298"


def test_normalize_history_movie_event_shape():
    events = list(normalize_history(_items(), cluster_threshold=3))
    e = next(e for e in events if e.external_ids.get("trakt_history_id") == 100002)
    assert e.note == "Dune: Part Two (2024)"
    assert e.title == "Dune: Part Two"
    assert e.external_ids["content_fingerprint"] == "movie:dune-part-two:y2024"
    # action=watch -> medium
    assert e.timestamp_confidence == "medium"


def test_normalize_history_cluster_items_flagged_low():
    events = list(normalize_history(_items(), cluster_threshold=3))
    cluster_evs = [e for e in events if e.external_ids.get("trakt_history_id") in (100003, 100004, 100005)]
    assert len(cluster_evs) == 3
    for e in cluster_evs:
        assert e.timestamp_confidence == "low"
        assert e.external_ids["timestamp_cluster_size"] == 3


def test_normalize_history_checkin_is_high_confidence():
    events = list(normalize_history(_items(), cluster_threshold=3))
    e = next(e for e in events if e.external_ids.get("trakt_history_id") == 100006)
    assert e.timestamp_confidence == "high"  # action=checkin


def test_normalize_history_deterministic_id_uses_history_id():
    events = list(normalize_history(_items(), cluster_threshold=3))
    e = events[0]
    assert e.deterministic_id.startswith("com.fulcra.media.trakt.history.")
    history_id_str = e.deterministic_id.rsplit(".", 1)[-1]
    assert history_id_str.isdigit()
```

- [ ] **Step 2: RED** — pytest.

- [ ] **Step 3: Implement**

Append to `fulcra_media/importers/trakt.py`:

```python
from collections import Counter


CONFIDENCE_BY_ACTION = {
    "scrobble": "high",
    "checkin": "high",
    "watch": "medium",   # often retroactive / imported
}


def detect_clusters(items: list[dict], threshold: int = 5) -> dict[str, int]:
    """Return {watched_at: count} for timestamps that have >= threshold items."""
    c = Counter(it["watched_at"] for it in items)
    return {ts: n for ts, n in c.items() if n >= threshold}


def _episode_note(show: dict, ep: dict) -> str:
    s, n, t = ep.get("season"), ep.get("number"), ep.get("title")
    base = f"{show['title']} S{s:02d}E{n:02d}"
    return f"{base} – {t}" if t else base


def _movie_note(m: dict) -> str:
    y = m.get("year")
    return f"{m['title']} ({y})" if y else m["title"]


def normalize_history(items: list[dict], cluster_threshold: int = 5) -> Iterator[NormalizedEvent]:
    """Convert raw Trakt history rows to NormalizedEvents."""
    clusters = detect_clusters(items, threshold=cluster_threshold)
    for it in items:
        action = it.get("action", "watch")
        watched_at = it["watched_at"]
        confidence = CONFIDENCE_BY_ACTION.get(action, "medium")
        ext: dict = {"trakt_history_id": it["id"], "trakt_action": action}
        if watched_at in clusters:
            confidence = "low"
            ext["timestamp_cluster_size"] = clusters[watched_at]

        start = datetime.fromisoformat(watched_at.replace("Z", "+00:00"))

        if it["type"] == "episode":
            ep = it["episode"]
            show = it["show"]
            runtime_min = ep.get("runtime") or 30
            end = start + timedelta(minutes=runtime_min)
            note = _episode_note(show, ep)
            title = show["title"]
            ext["content_fingerprint"] = content_fingerprint(
                "tv", show=show["title"], season=ep["season"], episode=ep["number"]
            )
            ext["show_ids"] = show.get("ids", {})
            ext["imdb"] = ep.get("ids", {}).get("imdb") or show.get("ids", {}).get("imdb")
        elif it["type"] == "movie":
            mv = it["movie"]
            runtime_min = mv.get("runtime") or 100
            end = start + timedelta(minutes=runtime_min)
            note = _movie_note(mv)
            title = mv["title"]
            ext["content_fingerprint"] = content_fingerprint(
                "movie", title=mv["title"], year=mv.get("year")
            )
            ext["imdb"] = mv.get("ids", {}).get("imdb")
        else:
            continue   # skip unknown types

        yield NormalizedEvent(
            importer="trakt",
            service="trakt",
            category="watched",
            note=note,
            title=title,
            start_time=start,
            end_time=end,
            deterministic_id=f"com.fulcra.media.trakt.v1.history.{it['id']}",
            timestamp_confidence=confidence,
            external_ids=ext,
        )
```

- [ ] **Step 4: GREEN + full suite**

- [ ] **Step 5: Commit**

```bash
git add fulcra_media/importers/trakt.py tests/test_trakt_importer.py
git commit -m "$(cat <<'EOF'
feat(trakt): normalize_history with cluster detection and per-action confidence

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: Trakt — fetch_history + CLI subcommand

**Files:**
- Modify: `fulcra_media/importers/trakt.py`
- Modify: `fulcra_media/cli.py`
- Modify: `tests/test_trakt_importer.py`
- Modify: `tests/test_cli.py`

Add `fetch_history()` that paginates `/sync/history` and `fulcra-media import trakt` wired to `parse + fetch + run_import`.

- [ ] **Step 1: Add tests**

Add to `tests/test_trakt_importer.py`:

```python
import httpx


def test_fetch_history_paginates(mocker, tmp_path):
    p = tmp_path / "trakt.json"
    import json as _j, os as _os, time as _t
    p.write_text(_j.dumps({"client_id":"cid","client_secret":"csec","access_token":"tok","refresh_token":"rt","expires_in":86400,"created_at": _t.time() + 100000}))
    mocker.patch("fulcra_media.importers.trakt.CREDS_PATH", p)

    call_log = []
    page1 = [{"id": 1}, {"id": 2}]
    page2 = [{"id": 3}]

    def transport_handler(request: httpx.Request) -> httpx.Response:
        call_log.append(request.url.params.get("page"))
        page = request.url.params.get("page") or "1"
        if page == "1":
            return httpx.Response(200, json=page1, headers={"X-Pagination-Page-Count": "2"})
        else:
            return httpx.Response(200, json=page2, headers={"X-Pagination-Page-Count": "2"})
    transport = httpx.MockTransport(transport_handler)
    mocker.patch("httpx.Client", lambda *a, **kw: httpx.Client(transport=transport))

    from fulcra_media.importers.trakt import fetch_history
    items = list(fetch_history(per_page=2))
    assert len(items) == 3
    assert items[0]["id"] == 1
```

(Add the CLI test in test_cli.py — stub fetch_history + run_import; assert exit_code 0 and `trakt:` in output.)

```python
def test_import_trakt_runs_pipeline(tmp_path: Path, mocker):
    state_path = tmp_path / "state.json"
    save(State(watched_definition_id="w", listened_definition_id="l", tag_ids={"trakt": "t"}), state_path)
    mocker.patch("fulcra_media.cli.STATE_PATH", state_path)

    from fulcra_media.importers.base import NormalizedEvent
    from datetime import datetime, timezone
    fake_events = [
        NormalizedEvent(importer="trakt", service="trakt", category="watched",
                        note="X", title="X",
                        start_time=datetime(2026,1,1,tzinfo=timezone.utc),
                        end_time=datetime(2026,1,1,1,tzinfo=timezone.utc),
                        deterministic_id="com.fulcra.media.trakt.v1.history.123",
                        timestamp_confidence="high"),
    ]
    mocker.patch("fulcra_media.importers.trakt.fetch_history", return_value=iter([]))
    mocker.patch("fulcra_media.importers.trakt.normalize_history", return_value=iter(fake_events))

    from fulcra_media.fulcra import ImportResult
    mocker.patch("fulcra_media.fulcra.FulcraClient.run_import",
                 return_value=ImportResult(1, 0, 1, 1))
    mocker.patch("fulcra_media.fulcra.FulcraClient.ensure_tag", return_value="t")

    result = CliRunner().invoke(cli, ["import", "trakt"])
    assert result.exit_code == 0, result.output
    assert "trakt:" in result.output
```

- [ ] **Step 2: RED**

- [ ] **Step 3: Implement `fetch_history` and CLI wire**

Append to `fulcra_media/importers/trakt.py`:

```python
def fetch_history(per_page: int = 1000) -> Iterator[dict]:
    """Iterate all history items, paginating most-recent-first."""
    auth = TraktAuth()
    page = 1
    with httpx.Client(timeout=60) as client:
        while True:
            r = client.get(
                f"{TRAKT_BASE}/sync/history",
                params={"extended": "full", "limit": per_page, "page": page},
                headers=auth.headers(),
            )
            r.raise_for_status()
            items = r.json()
            if not items:
                return
            for it in items:
                yield it
            page_count = int(r.headers.get("X-Pagination-Page-Count", "1"))
            if page >= page_count:
                return
            page += 1
```

In `fulcra_media/cli.py`, add:

```python
@import_group.command("trakt")
@click.option("--cluster-threshold", default=5, type=int,
              help="Mark ≥N items sharing watched_at as timestamp_confidence: low")
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
```

- [ ] **Step 4: GREEN + full suite**

- [ ] **Step 5: Commit**

```bash
git add fulcra_media/importers/trakt.py fulcra_media/cli.py tests/test_trakt_importer.py tests/test_cli.py
git commit -m "$(cat <<'EOF'
feat(trakt): fetch_history + CLI import subcommand

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: Apple Podcasts importer

**Files:**
- Create: `fulcra_media/importers/apple_podcasts.py`
- Create: `tests/test_apple_podcasts_importer.py`
- Create: `tests/fixtures/apple_podcasts_mtlibrary.sqlite`

Reads macOS Podcasts SQLite DB. Per spec §3.3: completed episodes only, idempotency key includes ZLASTDATEPLAYED so replays across importer runs are captured.

- [ ] **Step 1: Create the synthetic SQLite fixture**

Write a small Python script (or run inline) to populate `tests/fixtures/apple_podcasts_mtlibrary.sqlite` with 4 episodes across 2 podcasts: 2 completed, 1 in-progress, 1 manually marked played.

```python
import sqlite3
conn = sqlite3.connect("tests/fixtures/apple_podcasts_mtlibrary.sqlite")
conn.executescript("""
CREATE TABLE ZMTPODCAST (Z_PK INTEGER PRIMARY KEY, ZTITLE TEXT);
CREATE TABLE ZMTEPISODE (
    Z_PK INTEGER PRIMARY KEY,
    ZTITLE TEXT, ZCLEANEDTITLE TEXT,
    ZPODCAST INTEGER REFERENCES ZMTPODCAST(Z_PK),
    ZUUID TEXT, ZGUID TEXT, ZENCLOSUREURL TEXT,
    ZDURATION REAL, ZPLAYHEAD REAL,
    ZPLAYSTATE INTEGER, ZHASBEENPLAYED INTEGER,
    ZPLAYCOUNT INTEGER, ZMARKASPLAYED INTEGER,
    ZPLAYSTATEMANUALLYSET INTEGER, ZPLAYSTATESOURCE INTEGER,
    ZLASTDATEPLAYED REAL, ZLASTUSERMARKEDASPLAYEDDATE REAL,
    ZPLAYSTATELASTMODIFIEDDATE REAL,
    ZPUBDATE REAL, ZIMPORTDATE REAL, ZDOWNLOADDATE REAL
);
INSERT INTO ZMTPODCAST VALUES (1, 'Reply All');
INSERT INTO ZMTPODCAST VALUES (2, 'Hard Fork');
-- completed naturally (PLAYSTATE=3, manuallySet=0, playhead near duration)
INSERT INTO ZMTEPISODE (Z_PK, ZTITLE, ZCLEANEDTITLE, ZPODCAST, ZUUID, ZDURATION, ZPLAYHEAD, ZPLAYSTATE, ZHASBEENPLAYED, ZPLAYSTATEMANUALLYSET, ZLASTDATEPLAYED)
    VALUES (10, 'The Crime Machine, Part I', 'The Crime Machine, Part I', 1, 'ep-uuid-10', 2700.0, 2650.0, 3, 1, 0, 769876200.0);
-- completed naturally (the 50% threshold should be met)
INSERT INTO ZMTEPISODE (Z_PK, ZTITLE, ZCLEANEDTITLE, ZPODCAST, ZUUID, ZDURATION, ZPLAYHEAD, ZPLAYSTATE, ZHASBEENPLAYED, ZPLAYSTATEMANUALLYSET, ZLASTDATEPLAYED)
    VALUES (11, 'Episode About AI', 'Episode About AI', 2, 'ep-uuid-11', 3600.0, 3500.0, 3, 1, 0, 769962600.0);
-- in-progress (playhead at 50%) - exclude
INSERT INTO ZMTEPISODE (Z_PK, ZTITLE, ZCLEANEDTITLE, ZPODCAST, ZUUID, ZDURATION, ZPLAYHEAD, ZPLAYSTATE, ZHASBEENPLAYED, ZPLAYSTATEMANUALLYSET, ZLASTDATEPLAYED)
    VALUES (12, 'In-Progress Ep', 'In-Progress Ep', 1, 'ep-uuid-12', 3000.0, 1500.0, 2, 0, 0, 769876200.0);
-- manually marked played - exclude (manuallySet=1)
INSERT INTO ZMTEPISODE (Z_PK, ZTITLE, ZCLEANEDTITLE, ZPODCAST, ZUUID, ZDURATION, ZPLAYHEAD, ZPLAYSTATE, ZHASBEENPLAYED, ZPLAYSTATEMANUALLYSET, ZLASTDATEPLAYED)
    VALUES (13, 'Marked Played', 'Marked Played', 2, 'ep-uuid-13', 1800.0, 0.0, 3, 1, 1, 769876200.0);
""")
conn.commit()
conn.close()
```

ZLASTDATEPLAYED values are Mac absolute time (seconds since 2001-01-01 UTC). `769876200` = 2025-05-08, `769962600` = 2025-05-09 — close enough for fixture purposes.

Add this script as a build-helper at `tests/fixtures/_build_apple_podcasts_fixture.py` and run it once to produce the SQLite. Or just commit the resulting SQLite directly.

- [ ] **Step 2: Write the failing tests**

`tests/test_apple_podcasts_importer.py`:

```python
from pathlib import Path
from datetime import datetime, timezone

import pytest

from fulcra_media.importers.apple_podcasts import parse_db
from fulcra_media.importers.base import NormalizedEvent

FIXTURE = Path(__file__).parent / "fixtures" / "apple_podcasts_mtlibrary.sqlite"


def test_parse_db_returns_only_completed_unmanual_high_playhead():
    events = list(parse_db(FIXTURE))
    # ep 10 (Reply All) + ep 11 (Hard Fork) — 2 of 4 rows
    assert len(events) == 2
    uuids = sorted(e.external_ids["zuuid"] for e in events)
    assert uuids == ["ep-uuid-10", "ep-uuid-11"]


def test_parse_db_episode_shape():
    events = list(parse_db(FIXTURE))
    e = next(e for e in events if e.external_ids["zuuid"] == "ep-uuid-10")
    assert e.importer == "apple-podcasts"
    assert e.service == "apple-podcasts"
    assert e.category == "listened"
    assert e.note == "Reply All – The Crime Machine, Part I"
    assert e.title == "Reply All"
    # Mac absolute 769876200 -> Unix 1748183400 -> 2025-05-25 …
    # We expect end_time = the last-played instant; start = end - duration
    assert (e.end_time - e.start_time).total_seconds() == 2700
    assert e.timestamp_confidence == "medium"
    assert e.external_ids["content_fingerprint"].startswith("podcast:reply-all:")


def test_parse_db_deterministic_id_per_play_snapshot():
    """sha256(ZUUID|ZLASTDATEPLAYED) so a new last-played stamp = new event."""
    events = list(parse_db(FIXTURE))
    ids = [e.deterministic_id for e in events]
    assert len(ids) == len(set(ids))
    assert all(i.startswith("com.fulcra.media.apple-podcasts.v1.") for i in ids)
```

- [ ] **Step 3: RED**

- [ ] **Step 4: Implement**

`fulcra_media/importers/apple_podcasts.py`:

```python
"""Apple Podcasts importer — reads macOS MTLibrary.sqlite."""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import tempfile
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .base import NormalizedEvent, content_fingerprint

DEFAULT_DB_PATH = Path(os.path.expanduser(
    "~/Library/Group Containers/243LU875E5.groups.com.apple.podcasts/Documents/MTLibrary.sqlite"
))
MAC_EPOCH_OFFSET = 978307200  # seconds between Unix epoch and Core Data epoch (2001-01-01 UTC)


def _det_id(zuuid: str, zlastdateplayed: float) -> str:
    h = hashlib.sha256(f"{zuuid}|{zlastdateplayed}".encode()).hexdigest()
    return f"com.fulcra.media.apple-podcasts.v1.{h[:16]}"


def parse_db(db_path: Path) -> Iterator[NormalizedEvent]:
    """Yield one NormalizedEvent per completed episode in the DB snapshot.

    Filters: ZPLAYSTATE=3 (played) AND ZHASBEENPLAYED=1 AND
    ZPLAYSTATEMANUALLYSET=0 AND (ZPLAYHEAD / ZDURATION) > 0.9 AND
    ZLASTDATEPLAYED IS NOT NULL.
    """
    # Snapshot db + sidecars to a tempdir so we never touch the live DB
    src = Path(db_path)
    snap_dir = Path(tempfile.mkdtemp(prefix="apple-podcasts-snap-"))
    snap_db = snap_dir / "MTLibrary.sqlite"
    for ext in ("", "-wal", "-shm"):
        candidate = Path(str(src) + ext)
        if candidate.exists():
            shutil.copy2(candidate, snap_dir / candidate.name)
    try:
        conn = sqlite3.connect(snap_db)
        cur = conn.cursor()
        cur.execute("""
            SELECT
              e.ZUUID,
              e.ZLASTDATEPLAYED,
              p.ZTITLE,
              COALESCE(e.ZCLEANEDTITLE, e.ZTITLE),
              e.ZDURATION
            FROM ZMTEPISODE e
            JOIN ZMTPODCAST p ON p.Z_PK = e.ZPODCAST
            WHERE e.ZPLAYSTATE = 3
              AND e.ZHASBEENPLAYED = 1
              AND COALESCE(e.ZPLAYSTATEMANUALLYSET, 0) = 0
              AND (e.ZPLAYHEAD * 1.0 / NULLIF(e.ZDURATION, 0)) > 0.9
              AND e.ZLASTDATEPLAYED IS NOT NULL
        """)
        for zuuid, mac_last, show_title, ep_title, duration_s in cur.fetchall():
            unix_last = mac_last + MAC_EPOCH_OFFSET
            end = datetime.fromtimestamp(unix_last, tz=timezone.utc)
            start = end - timedelta(seconds=duration_s or 1)
            fp = content_fingerprint("podcast", show=show_title or "", title=ep_title or "")
            yield NormalizedEvent(
                importer="apple-podcasts",
                service="apple-podcasts",
                category="listened",
                note=f"{show_title} – {ep_title}",
                title=show_title,
                start_time=start,
                end_time=end,
                deterministic_id=_det_id(zuuid, mac_last),
                timestamp_confidence="medium",   # last-played snapshot, not per-play event log
                external_ids={
                    "zuuid": zuuid,
                    "show": show_title,
                    "episode_title": ep_title,
                    "duration_seconds": duration_s,
                    "content_fingerprint": fp,
                    "raw_mac_last_played": mac_last,
                },
            )
    finally:
        conn.close()
        shutil.rmtree(snap_dir, ignore_errors=True)
```

- [ ] **Step 5: Add CLI subcommand `fulcra-media import apple-podcasts`**

In `cli.py`:

```python
@import_group.command("apple-podcasts")
@click.option("--db", "db_path",
              default=str(Path.home() / "Library/Group Containers/243LU875E5.groups.com.apple.podcasts/Documents/MTLibrary.sqlite"),
              help="Path to MTLibrary.sqlite (default: macOS standard location)")
def import_apple_podcasts(db_path: str) -> None:
    """Import Apple Podcasts listening history from the on-device SQLite DB."""
    from .importers import apple_podcasts as ap
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
```

Plus a CLI test stubbing parse_db + run_import.

- [ ] **Step 6: GREEN + full suite**

- [ ] **Step 7: Commit**

```bash
git add fulcra_media/importers/apple_podcasts.py fulcra_media/cli.py tests/test_apple_podcasts_importer.py tests/test_cli.py tests/fixtures/apple_podcasts_mtlibrary.sqlite
git commit -m "$(cat <<'EOF'
feat(apple-podcasts): import completed episodes from macOS MTLibrary.sqlite

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: Spotify Extended importer

**Files:**
- Create: `fulcra_media/importers/spotify.py`
- Create: `tests/test_spotify_importer.py`
- Create: `tests/fixtures/spotify_extended_sample.zip`

Reads a zip containing `Streaming_History_Audio_*.json` files. Filter `ms_played >= 30000 AND skipped != true`. Music + podcasts.

- [ ] **Step 1: Build the fixture**

A Python script that creates `spotify_extended_sample.zip` containing one JSON file with 5 entries: 2 music (1 kept + 1 skipped), 2 podcast (1 kept + 1 < 30s), 1 invalid (no track or episode URI).

```python
import json, zipfile
entries = [
  {"ts":"2026-05-10T20:30:00Z","platform":"OS X 14","ms_played":210000,"conn_country":"US","ip_addr":"x",
    "master_metadata_track_name":"Get Lucky","master_metadata_album_artist_name":"Daft Punk",
    "master_metadata_album_album_name":"Random Access Memories",
    "spotify_track_uri":"spotify:track:69kOkLUCkxIZYexIgSG8rq",
    "episode_name":None,"episode_show_name":None,"spotify_episode_uri":None,
    "reason_start":"clickrow","reason_end":"trackdone","shuffle":False,"skipped":False,
    "offline":False,"offline_timestamp":None,"incognito_mode":False},
  {"ts":"2026-05-10T20:33:30Z","platform":"OS X 14","ms_played":5000,"conn_country":"US","ip_addr":"x",
    "master_metadata_track_name":"Around the World","master_metadata_album_artist_name":"Daft Punk",
    "master_metadata_album_album_name":"Homework",
    "spotify_track_uri":"spotify:track:1pKYYY0dkg23sQQXi0Q5zN",
    "episode_name":None,"episode_show_name":None,"spotify_episode_uri":None,
    "reason_start":"fwdbtn","reason_end":"fwdbtn","shuffle":False,"skipped":True,
    "offline":False,"offline_timestamp":None,"incognito_mode":False},
  {"ts":"2026-05-09T18:15:00Z","platform":"iOS","ms_played":2400000,"conn_country":"US","ip_addr":"y",
    "master_metadata_track_name":None,"master_metadata_album_artist_name":None,"master_metadata_album_album_name":None,
    "spotify_track_uri":None,
    "episode_name":"The Crime Machine, Part I","episode_show_name":"Reply All",
    "spotify_episode_uri":"spotify:episode:abc",
    "reason_start":"clickrow","reason_end":"trackdone","shuffle":False,"skipped":False,
    "offline":False,"offline_timestamp":None,"incognito_mode":False},
  {"ts":"2026-05-09T20:00:00Z","platform":"iOS","ms_played":15000,"conn_country":"US","ip_addr":"y",
    "master_metadata_track_name":None,"master_metadata_album_artist_name":None,"master_metadata_album_album_name":None,
    "spotify_track_uri":None,
    "episode_name":"Skipped Episode","episode_show_name":"Reply All",
    "spotify_episode_uri":"spotify:episode:def",
    "reason_start":"fwdbtn","reason_end":"fwdbtn","shuffle":False,"skipped":False,
    "offline":False,"offline_timestamp":None,"incognito_mode":False},
  {"ts":"2026-05-09T20:30:00Z","platform":"iOS","ms_played":40000,"conn_country":"US","ip_addr":"y",
    "master_metadata_track_name":None,"master_metadata_album_artist_name":None,"master_metadata_album_album_name":None,
    "spotify_track_uri":None,"episode_name":None,"episode_show_name":None,"spotify_episode_uri":None,
    "reason_start":"unknown","reason_end":"unknown","shuffle":False,"skipped":False,
    "offline":False,"offline_timestamp":None,"incognito_mode":False},
]
with zipfile.ZipFile("tests/fixtures/spotify_extended_sample.zip", "w") as zf:
    zf.writestr("Streaming_History_Audio_2026_1.json", json.dumps(entries))
```

Commit the resulting zip.

- [ ] **Step 2: Tests**

`tests/test_spotify_importer.py`:

```python
from pathlib import Path
from datetime import datetime, timezone

from fulcra_media.importers.spotify import parse_extended_zip

FIXTURE = Path(__file__).parent / "fixtures" / "spotify_extended_sample.zip"


def test_parse_extended_filters_skipped_and_short():
    events = list(parse_extended_zip(FIXTURE))
    # of 5 entries: keep music #1 (210s, !skipped), keep podcast #3 (2400s ms),
    # drop music #2 (skipped), drop podcast #4 (15s), drop #5 (no uri)
    assert len(events) == 2


def test_parse_extended_music_event_shape():
    events = list(parse_extended_zip(FIXTURE))
    e = next(e for e in events if e.title == "Get Lucky")
    assert e.importer == "spotify-extended"
    assert e.service == "spotify"
    assert e.category == "listened"
    assert e.note == "Daft Punk – Get Lucky"
    assert e.timestamp_confidence == "high"
    # ts is stream-end; start = ts - ms_played
    assert e.end_time == datetime(2026, 5, 10, 20, 30, 0, tzinfo=timezone.utc)
    assert e.start_time == datetime(2026, 5, 10, 20, 26, 30, tzinfo=timezone.utc)
    assert e.external_ids["kind"] == "music"
    assert e.external_ids["content_fingerprint"] == "music:daft-punk:get-lucky"


def test_parse_extended_podcast_event_shape():
    events = list(parse_extended_zip(FIXTURE))
    e = next(e for e in events if "Crime Machine" in e.note)
    assert e.note == "Reply All – The Crime Machine, Part I"
    assert e.title == "Reply All"
    assert e.external_ids["kind"] == "podcast"
    assert e.external_ids["content_fingerprint"].startswith("podcast:reply-all:")


def test_parse_extended_deterministic_id_per_stream():
    events = list(parse_extended_zip(FIXTURE))
    ids = [e.deterministic_id for e in events]
    assert len(ids) == len(set(ids))
    assert all(i.startswith("com.fulcra.media.spotify-extended.v1.") for i in ids)
```

- [ ] **Step 3: Implement `fulcra_media/importers/spotify.py`**

```python
"""Spotify Extended Streaming History importer."""

from __future__ import annotations

import hashlib
import json
import zipfile
from collections.abc import Iterator
from datetime import datetime, timezone, timedelta
from pathlib import Path

from .base import NormalizedEvent, content_fingerprint

MIN_MS_PLAYED = 30000


def _det_id(ts: str, uri: str | None) -> str:
    h = hashlib.sha256(f"{ts}|{uri}".encode()).hexdigest()
    return f"com.fulcra.media.spotify-extended.v1.{h[:16]}"


def parse_extended_zip(zip_path: Path) -> Iterator[NormalizedEvent]:
    """Yield NormalizedEvents from a Spotify Extended Streaming History zip."""
    with zipfile.ZipFile(zip_path) as zf:
        for name in zf.namelist():
            if not name.endswith(".json"):
                continue
            entries = json.loads(zf.read(name))
            for entry in entries:
                yield from _process(entry)


def _process(entry: dict) -> Iterator[NormalizedEvent]:
    ms_played = entry.get("ms_played", 0)
    if ms_played < MIN_MS_PLAYED:
        return
    if entry.get("skipped"):
        return

    ts = entry["ts"]
    end = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    start = end - timedelta(milliseconds=ms_played)

    track_uri = entry.get("spotify_track_uri")
    episode_uri = entry.get("spotify_episode_uri")

    if track_uri:
        artist = entry.get("master_metadata_album_artist_name") or ""
        track = entry.get("master_metadata_track_name") or ""
        if not artist or not track:
            return
        note = f"{artist} – {track}"
        title = track
        fp = content_fingerprint("music", artist=artist, track=track)
        det = _det_id(ts, track_uri)
        kind = "music"
    elif episode_uri:
        show = entry.get("episode_show_name") or ""
        ep_name = entry.get("episode_name") or ""
        if not show or not ep_name:
            return
        note = f"{show} – {ep_name}"
        title = show
        fp = content_fingerprint("podcast", show=show, title=ep_name)
        det = _det_id(ts, episode_uri)
        kind = "podcast"
    else:
        return

    yield NormalizedEvent(
        importer="spotify-extended",
        service="spotify",
        category="listened",
        note=note,
        title=title,
        start_time=start,
        end_time=end,
        deterministic_id=det,
        timestamp_confidence="high",
        external_ids={
            "kind": kind,
            "ms_played": ms_played,
            "platform": entry.get("platform"),
            "track_uri": track_uri,
            "episode_uri": episode_uri,
            "content_fingerprint": fp,
        },
    )
```

- [ ] **Step 4: Add CLI subcommand**

`fulcra-media import spotify-extended <path-to-zip>`. Uses `library.resolve` for `fulcra:/` URI support.

- [ ] **Step 5: GREEN + full suite + commit**

---

## Task 8: Apple Data & Privacy takeout importer

**Files:**
- Create: `fulcra_media/importers/apple_takeout.py`
- Create: `tests/test_apple_takeout_importer.py`
- Create: `tests/fixtures/apple_takeout_playback_sample.csv`

Reads `Apple Media Services information/Apple TV/Playback Activity.csv` from an Apple Privacy export. Filter `Event Type == "PLAY"`.

Schema (confirmed from synthetic but verified against real export structure):

```
Event Type, Content Type, Title, Episode Title, Season Number,
Episode Number, Start Time, End Time, Play Duration (Seconds),
Device Type, Device Model, Country
```

- [ ] **Step 1: Fixture**

`tests/fixtures/apple_takeout_playback_sample.csv`:

```csv
Event Type,Content Type,Title,Episode Title,Season Number,Episode Number,Start Time,End Time,Play Duration (Seconds),Device Type,Device Model,Country
PLAY,Movie,Dune: Part Two,,,,2025-01-15 20:30:00,2025-01-15 23:16:00,9960,Apple TV,Apple TV 4K (3rd generation),US
PLAY,TV Episode,Severance,The We We Are,2,1,2025-01-14 21:00:00,2025-01-14 21:58:00,3480,Apple TV,Apple TV 4K (3rd generation),US
PAUSE,Movie,The Holdovers,,,,2025-01-12 20:15:00,2025-01-12 20:15:00,0,iPhone,iPhone 15 Pro,US
PLAY,TV Episode,Ted Lasso,Pilot,1,1,2025-01-10 20:00:00,2025-01-10 20:32:00,1920,iPad,iPad Pro 12.9-inch (6th generation),US
PLAY,Movie,Killers of the Flower Moon,,,,2025-01-05 18:00:00,2025-01-05 21:26:00,12360,Apple TV,Apple TV 4K (3rd generation),US
```

- [ ] **Step 2: Tests + Implementation**

Pattern matches the Netflix rich importer closely:
- Parse Start Time / End Time as UTC (with `--apple-tz` flag for local override)
- Filter `Event Type != "PLAY"`
- Note: Movie → `"{Title}"`; Episode → `"{Title} S{Season:02d}E{Episode:02d} – {Episode Title}"`
- Idempotency: `sha256(Start Time | Title | Episode Title | Device Model)` → `com.fulcra.media.apple-takeout.v1.<sha16>`
- `external_ids`: device_type, device_model, country, content_fingerprint
- `timestamp_confidence`: `high`

Service tag: `apple-tv`.

(Spec out the full implementation following the netflix-rich pattern in Tasks 3/4 of the prior plan. Tests cover: PLAY filter, movie shape, episode shape, fingerprint, deterministic id.)

- [ ] **Step 3: CLI subcommand `fulcra-media import apple-takeout <path>`**

Accepts `<zip>`, `<dir>`, or direct `<csv>` path. Auto-detects which.

- [ ] **Step 4: Commit**

---

## Task 9: Wizards for the new 4 services

**Files:**
- Create: `fulcra_media/wizards/trakt.py`
- Create: `fulcra_media/wizards/apple_podcasts.py`
- Create: `fulcra_media/wizards/spotify.py`
- Create: `fulcra_media/wizards/apple_takeout.py`
- Modify: `fulcra_media/cli.py` (register under `wizard` group)
- Create: tests per wizard

Each wizard prints the canonical setup steps for its service. Trakt walks through OAuth device flow; others walk through requesting and locating the data file.

(Bulk-spec text scoped to fit; each wizard's content mirrors the Netflix wizard pattern with the source-specific steps.)

---

## Done criteria

- [ ] `pytest -q` passes (~95+ tests)
- [ ] `fulcra-media import trakt` posts events with cluster-flagged confidence
- [ ] `fulcra-media import apple-podcasts` reads MTLibrary.sqlite and posts completed-episode events
- [ ] `fulcra-media import spotify-extended <zip>` filters and posts music/podcast events
- [ ] `fulcra-media import apple-takeout <path>` posts events from the Playback Activity CSV
- [ ] Every emitted event has `external_ids.content_fingerprint` for cross-source dedup at query time
- [ ] Wizards exist for all 4 new services
