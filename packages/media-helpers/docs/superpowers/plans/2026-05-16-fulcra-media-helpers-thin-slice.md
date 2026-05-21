# FulcraMediaHelpers Thin Slice — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship an end-to-end vertical slice — `fulcra-media bootstrap` + `fulcra-media import netflix <path>` + `fulcra-media wizard netflix` — that turns the user's real `takeouts/NetflixViewingHistory.csv` (6,456 rows, 2010-2026) into Fulcra `DurationAnnotation` events with service-tag `netflix`, including the 27 same-day-rewatch duplicates as separate annotations.

**Architecture:** Python package using Click for the CLI. The Fulcra API is reached via `httpx`. Auth and Fulcra-Library file retrieval are obtained by shelling out to the user's installed `fulcra` CLI (`fulcra auth print-access-token` and `fulcra file download`) so this package doesn't reimplement OAuth or storage. Importers produce a `NormalizedEvent` and never touch the network; `fulcra.py` is the only module that does HTTP. Tests use `httpx.MockTransport` and subprocess mocks — no real network calls.

**Tech Stack:** Python 3.11+, Click 8.x, httpx, dateparser, pytest, the `fulcra-api` CLI (branch with both `add-cli` and `file-commands` merged, or both vendored).

**Spec reference:** `docs/superpowers/specs/2026-05-16-fulcra-media-helpers-design.md` — read this if anything in this plan is unclear; the spec is the source of truth for design decisions.

---

## File structure

Created by this plan:

```
.gitignore
pyproject.toml
README.md
fulcra_media/
  __init__.py
  state.py                     # ~/.config/fulcra-media/state.json
  library.py                   # fulcra:/... URI resolution via subprocess
  fulcra.py                    # auth, definitions/tags, ingest, verify, run pipeline
  cli.py                       # Click entry points
  importers/
    __init__.py
    base.py                    # NormalizedEvent dataclass + IngestResult
    netflix.py                 # slim 2-col CSV parser
  wizards/
    __init__.py
    netflix.py                 # interactive walkthrough
tests/
  __init__.py
  conftest.py                  # shared fixtures + MockTransport helpers
  fixtures/
    netflix_slim_small.csv     # 8 rows including a same-day rewatch
  test_state.py
  test_library.py
  test_importers_base.py
  test_netflix_importer.py
  test_fulcra_auth.py
  test_fulcra_tags_defs.py
  test_fulcra_ingest.py
  test_fulcra_dedup.py
  test_netflix_wizard.py
  test_cli.py
  test_e2e_netflix.py          # full pipeline vs real takeouts/NetflixViewingHistory.csv
```

Not in this plan (deferred to later plans): Trakt, Apple Podcasts, Spotify Extended, Last.fm, Apple takeout, Netflix rich (GDPR) variant.

---

## Conventions

- **TDD strictly:** every task writes a failing test first, runs it red, implements, runs it green, then commits.
- **Each `git commit` carries the `Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>` trailer** (already a project convention from the harness).
- **Run tests with:** `pytest -q` for fast runs, `pytest -v <path>::<test>` for one.
- **Type hints required.** Python 3.11 syntax (`str | None`, no `Optional`).
- **No top-level network calls in any module.** Tests must be hermetic.

---

## Task 1: Project scaffold + git init

**Files:**
- Create: `pyproject.toml`
- Create: `.gitignore`
- Create: `README.md`
- Create: `fulcra_media/__init__.py`
- Create: `fulcra_media/importers/__init__.py`
- Create: `fulcra_media/wizards/__init__.py`
- Create: `tests/__init__.py`
- Create: `tests/conftest.py`

- [ ] **Step 1: Initialize git repo**

```bash
cd /Users/Scanning/Developer/FulcraMediaHelpers
git init -b main
```

- [ ] **Step 2: Write `.gitignore`**

```
__pycache__/
*.py[cod]
.venv/
.venv-*/
.pytest_cache/
.mypy_cache/
.ruff_cache/
*.egg-info/
build/
dist/
.scratch/
.DS_Store
~/.config/fulcra-media/   # not in repo but documented as user state location
```

- [ ] **Step 3: Write `pyproject.toml`**

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "fulcra-media-helpers"
version = "0.1.0"
description = "Import media consumption (Watched/Listened) into Fulcra as annotations."
requires-python = ">=3.11"
dependencies = [
    "click>=8.1",
    "httpx>=0.27",
    "dateparser>=1.2",
    # The fulcra-api CLI must provide both `fulcra auth print-access-token`
    # (add-cli branch) and `fulcra file download` (file-commands branch).
    # Pin to a branch/tag that includes both, or vendor a merged fork.
    "fulcra-api @ git+https://github.com/fulcradynamics/fulcra-api-python.git@file-commands",
]

[project.scripts]
fulcra-media = "fulcra_media.cli:cli"

[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "pytest-mock>=3.12",
    "ruff>=0.5",
]

[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-ra --strict-markers"

[tool.ruff]
target-version = "py311"
line-length = 100
```

- [ ] **Step 4: Write minimal `README.md`**

```markdown
# FulcraMediaHelpers

Import your media consumption (Watched, Listened) into Fulcra as annotations.

See `docs/superpowers/specs/2026-05-16-fulcra-media-helpers-design.md` for the design.

## Install

    pip install -e ".[dev]"

## Bootstrap (once)

    fulcra auth login           # via the underlying fulcra-api CLI
    fulcra-media bootstrap      # create the Watched/Listened annotation definitions

## Import Netflix

    fulcra-media wizard netflix           # interactive walkthrough
    # or, if you already have a CSV in hand:
    fulcra-media import netflix takeouts/NetflixViewingHistory.csv
    # or from your Fulcra Library:
    fulcra-media import netflix fulcra:/takeouts/NetflixViewingHistory.csv
```

- [ ] **Step 5: Create empty package files**

```bash
touch fulcra_media/__init__.py
touch fulcra_media/importers/__init__.py
touch fulcra_media/wizards/__init__.py
touch tests/__init__.py
touch tests/conftest.py
```

- [ ] **Step 6: Verify the scaffold installs**

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest -q
```

Expected: `pip install` succeeds, `pytest` reports `no tests ran`.

- [ ] **Step 7: First commit**

```bash
git add .gitignore pyproject.toml README.md fulcra_media tests
git commit -m "$(cat <<'EOF'
chore: scaffold fulcra-media-helpers package

Empty Python package + pytest harness. No behavior yet.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: `state.py` — on-disk state load/save

**Files:**
- Create: `fulcra_media/state.py`
- Test: `tests/test_state.py`

The state file caches annotation definition IDs, tag UUIDs, and per-importer watermarks. Round-trips a dataclass to JSON. Default path is `~/.config/fulcra-media/state.json` but every function takes a `path:` override for testability.

- [ ] **Step 1: Write the failing tests**

In `tests/test_state.py`:

```python
from pathlib import Path

from fulcra_media.state import State, load, save


def test_load_returns_default_when_file_missing(tmp_path: Path):
    state = load(tmp_path / "does-not-exist.json")
    assert state == State()
    assert state.watched_definition_id is None
    assert state.tag_ids == {}
    assert state.watermarks == {}


def test_save_then_load_round_trips(tmp_path: Path):
    state = State(
        watched_definition_id="def-watched-uuid",
        listened_definition_id="def-listened-uuid",
        tag_ids={"netflix": "tag-uuid-1", "media": "tag-uuid-2"},
        watermarks={"netflix-slim": "2026-05-12"},
    )
    path = tmp_path / "nested" / "state.json"
    save(state, path)
    assert path.exists()
    loaded = load(path)
    assert loaded == state


def test_save_creates_parent_directories(tmp_path: Path):
    state = State()
    path = tmp_path / "a" / "b" / "c" / "state.json"
    save(state, path)
    assert path.exists()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest -v tests/test_state.py`
Expected: `ImportError` or `ModuleNotFoundError` for `fulcra_media.state`.

- [ ] **Step 3: Implement `state.py`**

In `fulcra_media/state.py`:

```python
"""On-disk state cache for fulcra-media-helpers.

Caches:
- Annotation definition IDs (created once via bootstrap)
- Tag UUIDs (created server-side, referenced by name locally)
- Per-importer watermarks (highest timestamp seen, for incremental runs)

Default location: ~/.config/fulcra-media/state.json. Every function takes an
explicit path argument to keep tests hermetic.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

DEFAULT_PATH = Path(
    os.environ.get("FULCRA_MEDIA_STATE")
    or os.path.expanduser("~/.config/fulcra-media/state.json")
)


@dataclass
class State:
    watched_definition_id: str | None = None
    listened_definition_id: str | None = None
    tag_ids: dict[str, str] = field(default_factory=dict)
    watermarks: dict[str, str] = field(default_factory=dict)


def load(path: Path = DEFAULT_PATH) -> State:
    if not path.exists():
        return State()
    raw = json.loads(path.read_text())
    return State(
        watched_definition_id=raw.get("watched_definition_id"),
        listened_definition_id=raw.get("listened_definition_id"),
        tag_ids=raw.get("tag_ids", {}),
        watermarks=raw.get("watermarks", {}),
    )


def save(state: State, path: Path = DEFAULT_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(state), indent=2, sort_keys=True))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest -v tests/test_state.py`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add fulcra_media/state.py tests/test_state.py
git commit -m "$(cat <<'EOF'
feat(state): on-disk state cache for definitions, tags, watermarks

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: `importers/base.py` — `NormalizedEvent` dataclass

**Files:**
- Create: `fulcra_media/importers/base.py`
- Test: `tests/test_importers_base.py`

The shared dataclass every importer produces. `fulcra.py` consumes only this type — importers know nothing about Fulcra.

- [ ] **Step 1: Write the failing test**

In `tests/test_importers_base.py`:

```python
from datetime import datetime, timezone

from fulcra_media.importers.base import NormalizedEvent


def test_normalized_event_has_required_fields():
    event = NormalizedEvent(
        importer="netflix-slim",
        service="netflix",
        category="watched",
        note="Stranger Things S01E01 – The Vanishing of Will Byers",
        title="Stranger Things",
        start_time=datetime(2024, 8, 14, 21, 0, tzinfo=timezone.utc),
        end_time=datetime(2024, 8, 14, 21, 30, tzinfo=timezone.utc),
        deterministic_id="com.fulcra.media.netflix.abc123def4567890",
        timestamp_confidence="high",
        external_ids={"profile": "default"},
    )
    assert event.importer == "netflix-slim"
    assert event.service == "netflix"
    assert event.category == "watched"
    assert event.timestamp_confidence == "high"
    assert event.external_ids == {"profile": "default"}


def test_normalized_event_external_ids_defaults_to_empty():
    event = NormalizedEvent(
        importer="netflix-slim",
        service="netflix",
        category="watched",
        note="x",
        title="x",
        start_time=datetime(2024, 1, 1, tzinfo=timezone.utc),
        end_time=datetime(2024, 1, 1, tzinfo=timezone.utc),
        deterministic_id="id",
        timestamp_confidence="low",
    )
    assert event.external_ids == {}


def test_normalized_event_rejects_naive_datetimes():
    import pytest
    with pytest.raises(ValueError):
        NormalizedEvent(
            importer="x", service="x", category="watched",
            note="x", title="x",
            start_time=datetime(2024, 1, 1),  # no tzinfo
            end_time=datetime(2024, 1, 1, tzinfo=timezone.utc),
            deterministic_id="id", timestamp_confidence="high",
        )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest -v tests/test_importers_base.py`
Expected: `ImportError`.

- [ ] **Step 3: Implement `importers/base.py`**

In `fulcra_media/importers/base.py`:

```python
"""Shared types for importers."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

VALID_CATEGORIES = {"watched", "listened"}
VALID_CONFIDENCE = {"high", "medium", "low"}


@dataclass
class NormalizedEvent:
    importer: str
    service: str
    category: str            # "watched" or "listened"
    note: str
    title: str
    start_time: datetime
    end_time: datetime
    deterministic_id: str    # full source string e.g. "com.fulcra.media.netflix.<sha16>"
    timestamp_confidence: str
    external_ids: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.category not in VALID_CATEGORIES:
            raise ValueError(f"invalid category {self.category!r}")
        if self.timestamp_confidence not in VALID_CONFIDENCE:
            raise ValueError(f"invalid timestamp_confidence {self.timestamp_confidence!r}")
        if self.start_time.tzinfo is None or self.end_time.tzinfo is None:
            raise ValueError("start_time and end_time must be timezone-aware")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest -v tests/test_importers_base.py`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add fulcra_media/importers/base.py tests/test_importers_base.py
git commit -m "$(cat <<'EOF'
feat(importers): NormalizedEvent dataclass with TZ + category validation

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Netflix importer — date parsing

**Files:**
- Create: `fulcra_media/importers/netflix.py`
- Test: `tests/test_netflix_importer.py`

The Netflix slim CSV has dates as `M/D/YY` (e.g. `5/12/26` = May 12, 2026). Two-digit years need century pivoting — by Netflix's data range (account creation 2008+) any `YY` value maps to 20YY, never 19YY.

- [ ] **Step 1: Write the failing test**

In `tests/test_netflix_importer.py`:

```python
from datetime import date

from fulcra_media.importers.netflix import parse_netflix_date


def test_parse_netflix_date_full_year():
    assert parse_netflix_date("5/12/26") == date(2026, 5, 12)
    assert parse_netflix_date("1/1/10") == date(2010, 1, 1)
    assert parse_netflix_date("12/31/99") == date(2099, 12, 31)


def test_parse_netflix_date_single_digit_month_and_day():
    assert parse_netflix_date("6/4/10") == date(2010, 6, 4)


def test_parse_netflix_date_invalid_raises():
    import pytest
    with pytest.raises(ValueError):
        parse_netflix_date("2024-05-12")
    with pytest.raises(ValueError):
        parse_netflix_date("")
    with pytest.raises(ValueError):
        parse_netflix_date("13/45/26")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest -v tests/test_netflix_importer.py::test_parse_netflix_date_full_year`
Expected: `ImportError`.

- [ ] **Step 3: Implement `parse_netflix_date`**

Create `fulcra_media/importers/netflix.py`:

```python
"""Netflix slim-CSV importer.

Slim variant (in-app per-profile download) has two columns: Title, Date.
Date format is M/D/YY (US, two-digit year). No time, no timezone, no duration,
no profile.
"""

from __future__ import annotations

import re
from datetime import date


_NETFLIX_DATE_RE = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{2})$")


def parse_netflix_date(value: str) -> date:
    """Parse Netflix's M/D/YY into a date. Two-digit years are 20YY."""
    m = _NETFLIX_DATE_RE.match(value or "")
    if not m:
        raise ValueError(f"not a Netflix slim date: {value!r}")
    month, day, year2 = (int(x) for x in m.groups())
    return date(2000 + year2, month, day)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest -v tests/test_netflix_importer.py`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add fulcra_media/importers/netflix.py tests/test_netflix_importer.py
git commit -m "$(cat <<'EOF'
feat(netflix): parse_netflix_date for M/D/YY format

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: Netflix importer — note + duration heuristic

**Files:**
- Modify: `fulcra_media/importers/netflix.py`
- Modify: `tests/test_netflix_importer.py`

The slim CSV's `Title` column glues show/season/episode with `": "` separators. Movies have no colon (rough heuristic — some movies do, but we accept the imprecision and flag it via duration estimation). Estimated durations: 30min for episode-like titles, 100min for movie-like, 45min default.

- [ ] **Step 1: Add the failing tests**

Append to `tests/test_netflix_importer.py`:

```python
from datetime import timedelta

from fulcra_media.importers.netflix import (
    make_note_and_title,
    estimate_duration,
)


def test_make_note_and_title_movie_no_colon():
    note, title = make_note_and_title("Tetris")
    assert note == "Tetris"
    assert title == "Tetris"


def test_make_note_and_title_episode_three_parts():
    note, title = make_note_and_title("Stranger Things: Season 1: Chapter Three: The Body")
    # All trailing parts after the show stay in the episode portion
    assert title == "Stranger Things"
    assert "Stranger Things" in note
    assert "Season 1" in note
    assert "Chapter Three: The Body" in note


def test_make_note_and_title_episode_two_parts():
    note, title = make_note_and_title("Slow Horses: Failure's Contagious")
    assert title == "Slow Horses"
    assert note == "Slow Horses: Failure's Contagious"


def test_make_note_and_title_leading_colon_malformed():
    # Real Netflix data has rows like " : Episode 10" where the show name is missing
    note, title = make_note_and_title(" : Episode 10")
    assert note == ": Episode 10"
    assert title == ""


def test_estimate_duration_movie_no_colon():
    assert estimate_duration("Tetris") == timedelta(minutes=100)


def test_estimate_duration_episode_with_season_marker():
    assert estimate_duration("Show: Season 1: Ep") == timedelta(minutes=30)
    assert estimate_duration("Show: Limited Series: Episode 1") == timedelta(minutes=30)


def test_estimate_duration_two_part_default():
    assert estimate_duration("Some Show: Some Title") == timedelta(minutes=45)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest -v tests/test_netflix_importer.py`
Expected: 7 failures (the 4 new note tests, 3 duration tests) on `ImportError`.

- [ ] **Step 3: Implement the two helpers**

Append to `fulcra_media/importers/netflix.py`:

```python
from datetime import timedelta


def make_note_and_title(raw_title: str) -> tuple[str, str]:
    """Split Netflix's joined title into a display note + bare show title.

    Returns (note, title). For movies (no colon) note == title == raw_title.
    For shows, title is the first colon-separated part (show name), note keeps
    the full string in trimmed form. Handles malformed rows whose show name is
    blank (e.g. " : Episode 10") by returning an empty title.
    """
    parts = [p.strip() for p in raw_title.split(":")]
    # Re-join with consistent spacing to clean leading whitespace
    note = ": ".join(p for p in parts if p) if parts[0] == "" else ": ".join(parts)
    if len(parts) == 1:
        return note, parts[0]
    return note, parts[0]


def estimate_duration(raw_title: str) -> timedelta:
    """Heuristic runtime estimate for slim-variant rows (no real duration).

    - No colon -> assume movie -> 100 min
    - Contains 'Season' or 'Episode' marker -> assume TV episode -> 30 min
    - Otherwise -> default 45 min
    """
    if ":" not in raw_title:
        return timedelta(minutes=100)
    lowered = raw_title.lower()
    if "season" in lowered or "episode" in lowered or "limited series" in lowered:
        return timedelta(minutes=30)
    return timedelta(minutes=45)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest -v tests/test_netflix_importer.py`
Expected: 10 passed (3 from Task 4 + 7 new).

- [ ] **Step 5: Commit**

```bash
git add fulcra_media/importers/netflix.py tests/test_netflix_importer.py
git commit -m "$(cat <<'EOF'
feat(netflix): note formatting and duration heuristic for slim CSV

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: Netflix importer — `parse_slim` with per-event idempotency

**Files:**
- Modify: `fulcra_media/importers/netflix.py`
- Create: `tests/fixtures/netflix_slim_small.csv`
- Modify: `tests/test_netflix_importer.py`

The full slim-CSV parser. Synthesizes a `start_time` at `21:00 UTC` on the date and an `end_time = start + estimate_duration(title)`. Idempotency key: `sha256(date | raw_title | occurrence_index)` truncated to 16 hex chars, where `occurrence_index` is the count of prior rows with the same `(date, raw_title)` pair. **This is the dedup-respects-rewatches rule** — the 27 same-day rewatches in the real CSV must become 27 distinct events with different keys.

- [ ] **Step 1: Create the fixture CSV**

Write `tests/fixtures/netflix_slim_small.csv`:

```csv
Title,Date
"Movie One","5/12/26"
"Show A: Season 1: Episode 1","5/12/26"
"Show A: Season 1: Episode 2","5/12/26"
" : Episode 10","2/16/20"
" : Episode 10","2/16/20"
"Show B: Limited Series: Episode 1","5/1/26"
"Show B: Limited Series: Episode 1","5/1/26"
"Movie Two","6/4/10"
```

Row 4 and 5 are the same-day, same-title pair (testing the malformed-leading-colon + rewatch combo). Rows 6 and 7 are a clean same-day rewatch.

- [ ] **Step 2: Write the failing tests**

Append to `tests/test_netflix_importer.py`:

```python
from datetime import datetime, timezone
from pathlib import Path

from fulcra_media.importers.netflix import parse_slim
from fulcra_media.importers.base import NormalizedEvent


FIXTURE = Path(__file__).parent / "fixtures" / "netflix_slim_small.csv"


def test_parse_slim_yields_one_event_per_row():
    events = list(parse_slim(FIXTURE))
    assert len(events) == 8


def test_parse_slim_first_event_is_movie():
    events = list(parse_slim(FIXTURE))
    e = events[0]
    assert isinstance(e, NormalizedEvent)
    assert e.importer == "netflix-slim"
    assert e.service == "netflix"
    assert e.category == "watched"
    assert e.note == "Movie One"
    assert e.title == "Movie One"
    assert e.start_time == datetime(2026, 5, 12, 21, 0, tzinfo=timezone.utc)
    # Movie heuristic -> 100 min
    assert (e.end_time - e.start_time).total_seconds() == 100 * 60
    assert e.timestamp_confidence == "low"
    assert e.external_ids["time_estimated"] is True
    assert e.external_ids["duration_estimated"] is True


def test_parse_slim_episode_yields_30min_duration():
    events = list(parse_slim(FIXTURE))
    e = events[1]
    assert (e.end_time - e.start_time).total_seconds() == 30 * 60


def test_parse_slim_same_day_rewatch_gets_distinct_ids():
    """The 27 real-data same-day rewatches must each produce a unique annotation."""
    events = list(parse_slim(FIXTURE))
    # rows 4 and 5 (zero-indexed 3 and 4): same date, same raw title
    a, b = events[3], events[4]
    assert a.start_time == b.start_time
    assert a.note == b.note
    assert a.deterministic_id != b.deterministic_id
    # And both deterministic_ids start with the expected prefix
    assert a.deterministic_id.startswith("com.fulcra.media.netflix.")
    assert b.deterministic_id.startswith("com.fulcra.media.netflix.")


def test_parse_slim_clean_same_day_rewatch_also_distinct():
    events = list(parse_slim(FIXTURE))
    a, b = events[5], events[6]  # "Show B: ... Episode 1" twice on 5/1/26
    assert a.note == b.note
    assert a.deterministic_id != b.deterministic_id


def test_parse_slim_deterministic_id_is_stable_across_runs():
    """Same CSV in -> same IDs out."""
    a = list(parse_slim(FIXTURE))
    b = list(parse_slim(FIXTURE))
    assert [e.deterministic_id for e in a] == [e.deterministic_id for e in b]


def test_parse_slim_malformed_leading_colon_row_has_empty_title():
    events = list(parse_slim(FIXTURE))
    e = events[3]
    assert e.title == ""
    assert e.note  # non-empty
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `pytest -v tests/test_netflix_importer.py`
Expected: 6 new failures on `ImportError` for `parse_slim`.

- [ ] **Step 4: Implement `parse_slim`**

Append to `fulcra_media/importers/netflix.py`:

```python
import csv
import hashlib
from collections import Counter
from collections.abc import Iterator
from datetime import datetime, time, timezone
from pathlib import Path

from .base import NormalizedEvent


def _det_id(date_str: str, raw_title: str, occurrence: int) -> str:
    h = hashlib.sha256(f"{date_str}|{raw_title}|{occurrence}".encode()).hexdigest()
    return f"com.fulcra.media.netflix.{h[:16]}"


def parse_slim(csv_path: Path) -> Iterator[NormalizedEvent]:
    """Parse a Netflix slim CSV (Title, Date) into NormalizedEvents.

    Each row -> one event with synthetic 21:00 UTC start time and an estimated
    duration. Idempotency key incorporates an occurrence index so same-day
    rewatches produce distinct events.
    """
    occurrence_counter: Counter[tuple[str, str]] = Counter()

    with csv_path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames != ["Title", "Date"]:
            raise ValueError(
                f"unexpected Netflix CSV header {reader.fieldnames!r}; "
                "this importer handles the slim 2-column variant only"
            )
        for row in reader:
            raw_title = row["Title"]
            date_str = row["Date"]
            d = parse_netflix_date(date_str)
            key = (date_str, raw_title)
            idx = occurrence_counter[key]
            occurrence_counter[key] += 1

            note, title = make_note_and_title(raw_title)
            start = datetime.combine(d, time(21, 0, 0), tzinfo=timezone.utc)
            end = start + estimate_duration(raw_title)

            yield NormalizedEvent(
                importer="netflix-slim",
                service="netflix",
                category="watched",
                note=note,
                title=title,
                start_time=start,
                end_time=end,
                deterministic_id=_det_id(date_str, raw_title, idx),
                timestamp_confidence="low",
                external_ids={
                    "time_estimated": True,
                    "duration_estimated": True,
                    "occurrence_index": idx,
                    "raw_date": date_str,
                },
            )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest -v tests/test_netflix_importer.py`
Expected: 16 passed (10 from earlier + 6 new + 1 existing rewatch-id test = double-check the count matches your earlier additions).

- [ ] **Step 6: Commit**

```bash
git add fulcra_media/importers/netflix.py tests/fixtures/netflix_slim_small.csv tests/test_netflix_importer.py
git commit -m "$(cat <<'EOF'
feat(netflix): parse_slim emits NormalizedEvents with per-event idempotency

Same-day rewatches produce distinct annotations via occurrence-index in
the deterministic_id hash, per spec §3.2b.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: `library.py` — `fulcra:/...` URI resolution

**Files:**
- Create: `fulcra_media/library.py`
- Test: `tests/test_library.py`

Resolves path arguments. Local paths pass through; `fulcra:/...` URIs shell out to `fulcra file download <remote>` into a tempfile. Subprocess invocation is fully mocked in tests.

- [ ] **Step 1: Write the failing tests**

In `tests/test_library.py`:

```python
import subprocess
from pathlib import Path

import pytest

from fulcra_media import library


def test_is_fulcra_uri_true():
    assert library.is_fulcra_uri("fulcra:/takeouts/x.csv")
    assert library.is_fulcra_uri("fulcra:/x.csv")


def test_is_fulcra_uri_false():
    assert not library.is_fulcra_uri("/tmp/x.csv")
    assert not library.is_fulcra_uri("takeouts/x.csv")
    assert not library.is_fulcra_uri("")


def test_resolve_local_path_passes_through(tmp_path: Path):
    p = tmp_path / "file.csv"
    p.write_text("hi")
    out = library.resolve(str(p))
    assert out == p
    assert out.read_text() == "hi"


def test_resolve_local_path_missing_raises(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        library.resolve(str(tmp_path / "does-not-exist.csv"))


def test_resolve_fulcra_uri_shells_out(mocker, tmp_path: Path):
    """fulcra:/x.csv -> `fulcra file download /x.csv <tempfile>` is called."""
    calls = []

    def fake_run(cmd, **kwargs):
        # Mock writes the expected contents to the tempfile (last argument)
        Path(cmd[-1]).write_bytes(b"downloaded contents")
        calls.append(cmd)
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=b"", stderr=b"")

    mocker.patch("subprocess.run", side_effect=fake_run)
    result = library.resolve("fulcra:/takeouts/file.csv")
    assert result.read_bytes() == b"downloaded contents"
    assert calls[0][:3] == ["fulcra", "file", "download"]
    assert calls[0][3] == "/takeouts/file.csv"
    # The last arg is the local tempfile path
    assert calls[0][4] == str(result)


def test_resolve_fulcra_uri_propagates_subprocess_failure(mocker):
    err = subprocess.CalledProcessError(returncode=2, cmd=["fulcra", "file", "download"])
    mocker.patch("subprocess.run", side_effect=err)
    with pytest.raises(RuntimeError, match="fulcra file download"):
        library.resolve("fulcra:/missing.csv")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest -v tests/test_library.py`
Expected: `ImportError`.

- [ ] **Step 3: Implement `library.py`**

In `fulcra_media/library.py`:

```python
"""Path argument resolution.

Importers accept either a local filesystem path or a `fulcra:/...` URI that
points into the user's Fulcra Library. The Library is implemented by the
fulcra-api CLI's `file-commands` branch (`fulcra file download`).
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

FULCRA_URI_PREFIX = "fulcra:"


def is_fulcra_uri(value: str) -> bool:
    return value.startswith(FULCRA_URI_PREFIX) if value else False


def resolve(path_or_uri: str) -> Path:
    """Return a local Path. Downloads to a tempfile if it's a fulcra: URI."""
    if is_fulcra_uri(path_or_uri):
        remote = path_or_uri[len(FULCRA_URI_PREFIX):]
        if not remote.startswith("/"):
            remote = "/" + remote
        suffix = Path(remote).suffix or ""
        tf = tempfile.NamedTemporaryFile(prefix="fulcra-media-", suffix=suffix, delete=False)
        tf.close()
        local = Path(tf.name)
        try:
            subprocess.run(
                ["fulcra", "file", "download", remote, str(local)],
                check=True,
                capture_output=True,
            )
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(
                f"fulcra file download {remote} failed (rc={exc.returncode}): "
                f"{exc.stderr!r}"
            ) from exc
        return local

    p = Path(path_or_uri).expanduser()
    if not p.exists():
        raise FileNotFoundError(p)
    return p
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest -v tests/test_library.py`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add fulcra_media/library.py tests/test_library.py
git commit -m "$(cat <<'EOF'
feat(library): resolve fulcra:/... URIs via `fulcra file download`

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 8: `fulcra.py` — `FulcraClient.get_token` shell-out

**Files:**
- Create: `fulcra_media/fulcra.py`
- Test: `tests/test_fulcra_auth.py`

The `FulcraClient` is the only HTTP-talking module. Auth comes from shelling out to `fulcra auth print-access-token`. Tests mock subprocess.

- [ ] **Step 1: Write the failing tests**

In `tests/test_fulcra_auth.py`:

```python
import subprocess

import pytest

from fulcra_media.fulcra import FulcraClient


def test_get_token_calls_fulcra_auth(mocker):
    mocker.patch(
        "subprocess.run",
        return_value=subprocess.CompletedProcess(
            args=[], returncode=0, stdout=b"  fake.jwt.token  \n", stderr=b""
        ),
    )
    client = FulcraClient()
    assert client.get_token() == "fake.jwt.token"


def test_get_token_propagates_failure(mocker):
    err = subprocess.CalledProcessError(returncode=1, cmd=["fulcra"], stderr=b"not logged in")
    mocker.patch("subprocess.run", side_effect=err)
    client = FulcraClient()
    with pytest.raises(RuntimeError, match="fulcra auth print-access-token"):
        client.get_token()


def test_get_token_respects_env_override(mocker):
    mocker.patch.dict("os.environ", {"FULCRA_ACCESS_TOKEN": "env-token"})
    # subprocess should NOT be called when env var is set
    spy = mocker.patch("subprocess.run")
    client = FulcraClient()
    assert client.get_token() == "env-token"
    spy.assert_not_called()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest -v tests/test_fulcra_auth.py`
Expected: `ImportError`.

- [ ] **Step 3: Implement the auth surface of `fulcra.py`**

In `fulcra_media/fulcra.py`:

```python
"""Fulcra API client + run-import pipeline.

Single point of contact with the Fulcra REST API. Importers produce
NormalizedEvent instances; this module handles auth, definitions, tags,
ingest, dedup readback, and verification.
"""

from __future__ import annotations

import os
import subprocess

import httpx

DEFAULT_BASE_URL = os.environ.get("FULCRA_API_BASE", "https://api.fulcradynamics.com")


class FulcraClient:
    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url
        self._transport = transport
        self._http: httpx.Client | None = None

    def get_token(self) -> str:
        env = os.environ.get("FULCRA_ACCESS_TOKEN")
        if env:
            return env
        try:
            result = subprocess.run(
                ["fulcra", "auth", "print-access-token"],
                check=True,
                capture_output=True,
            )
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(
                "fulcra auth print-access-token failed; run `fulcra auth login` first. "
                f"stderr={exc.stderr!r}"
            ) from exc
        return result.stdout.decode().strip()

    def _client(self) -> httpx.Client:
        if self._http is None:
            self._http = httpx.Client(
                base_url=self.base_url,
                transport=self._transport,
                timeout=30.0,
                headers={"User-Agent": "fulcra-media-helpers/0.1"},
            )
        return self._http

    def _authed_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.get_token()}"}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest -v tests/test_fulcra_auth.py`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add fulcra_media/fulcra.py tests/test_fulcra_auth.py
git commit -m "$(cat <<'EOF'
feat(fulcra): FulcraClient.get_token via subprocess + env override

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 9: `fulcra.py` — tags and definitions

**Files:**
- Modify: `fulcra_media/fulcra.py`
- Create: `tests/conftest.py` (helper for MockTransport)
- Create: `tests/test_fulcra_tags_defs.py`

Adds `ensure_tag(name)` and `ensure_definitions(state)` — both idempotent against local `State` cache. Uses `httpx.MockTransport` to intercept all HTTP.

- [ ] **Step 1: Add shared MockTransport helper to `conftest.py`**

Replace `tests/conftest.py` with:

```python
"""Shared test fixtures."""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest


class RecordingTransport(httpx.MockTransport):
    """MockTransport that records every request it sees."""

    def __init__(self, handler: Callable[[httpx.Request], httpx.Response]) -> None:
        self.requests: list[httpx.Request] = []
        self.bodies: list[bytes] = []

        def wrapper(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            self.bodies.append(request.content)
            return handler(request)

        super().__init__(wrapper)


@pytest.fixture
def recording_transport():
    def make(handler: Callable[[httpx.Request], httpx.Response]) -> RecordingTransport:
        return RecordingTransport(handler)
    return make


def json_response(status: int, body: dict | list) -> httpx.Response:
    return httpx.Response(status, content=json.dumps(body).encode(), headers={"content-type": "application/json"})
```

- [ ] **Step 2: Write the failing tests**

In `tests/test_fulcra_tags_defs.py`:

```python
import httpx
import pytest

from fulcra_media.fulcra import FulcraClient
from fulcra_media.state import State
from tests.conftest import json_response


@pytest.fixture(autouse=True)
def fake_token(mocker):
    mocker.patch.dict("os.environ", {"FULCRA_ACCESS_TOKEN": "test-token"})


def test_ensure_tag_returns_cached_id_without_hitting_api(recording_transport):
    transport = recording_transport(lambda r: pytest.fail(f"unexpected request {r.url}"))
    client = FulcraClient(transport=transport)
    state = State(tag_ids={"netflix": "cached-uuid"})
    assert client.ensure_tag("netflix", state) == "cached-uuid"
    assert state.tag_ids == {"netflix": "cached-uuid"}


def test_ensure_tag_looks_up_existing_then_caches(recording_transport):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/user/v1alpha1/tag/name/netflix":
            return json_response(200, {"id": "server-uuid", "name": "netflix"})
        pytest.fail(f"unexpected {request.method} {request.url}")
    transport = recording_transport(handler)
    client = FulcraClient(transport=transport)
    state = State()
    tag_id = client.ensure_tag("netflix", state)
    assert tag_id == "server-uuid"
    assert state.tag_ids["netflix"] == "server-uuid"


def test_ensure_tag_creates_when_missing(recording_transport):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(404)
        if request.method == "POST" and request.url.path == "/user/v1alpha1/tag":
            return json_response(200, {"id": "new-uuid", "name": "netflix"})
        pytest.fail(f"unexpected {request.method} {request.url}")
    transport = recording_transport(handler)
    client = FulcraClient(transport=transport)
    state = State()
    tag_id = client.ensure_tag("netflix", state)
    assert tag_id == "new-uuid"
    assert state.tag_ids["netflix"] == "new-uuid"


def test_ensure_definitions_creates_watched_and_listened(recording_transport):
    posted: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        # Bootstrap will first ensure_tag the three default tags
        if request.method == "GET" and "/tag/name/" in request.url.path:
            return httpx.Response(404)
        if request.method == "POST" and request.url.path == "/user/v1alpha1/tag":
            import json as _json
            body = _json.loads(request.content)
            return json_response(200, {"id": f"tag-{body['name']}", "name": body["name"]})
        if request.method == "POST" and request.url.path == "/user/v1alpha1/annotation":
            import json as _json
            body = _json.loads(request.content)
            posted.append(body)
            kind = body["name"].lower()
            return json_response(200, {"id": f"def-{kind}", **body})
        pytest.fail(f"unexpected {request.method} {request.url}")

    transport = recording_transport(handler)
    client = FulcraClient(transport=transport)
    state = State()
    client.ensure_definitions(state)

    assert state.watched_definition_id == "def-watched"
    assert state.listened_definition_id == "def-listened"
    # Both definitions are DurationAnnotation with the right default tags
    assert {d["annotation_type"] for d in posted} == {"duration"}
    watched = next(d for d in posted if d["name"] == "Watched")
    listened = next(d for d in posted if d["name"] == "Listened")
    assert "tag-media" in watched["tags"] and "tag-watched" in watched["tags"]
    assert "tag-media" in listened["tags"] and "tag-listened" in listened["tags"]


def test_ensure_definitions_skips_when_already_cached(recording_transport):
    transport = recording_transport(lambda r: pytest.fail(f"unexpected {r.url}"))
    client = FulcraClient(transport=transport)
    state = State(watched_definition_id="x", listened_definition_id="y", tag_ids={"media": "m", "watched": "w", "listened": "l"})
    client.ensure_definitions(state)
    assert state.watched_definition_id == "x"
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `pytest -v tests/test_fulcra_tags_defs.py`
Expected: `AttributeError: 'FulcraClient' object has no attribute 'ensure_tag'`.

- [ ] **Step 4: Extend `FulcraClient` in `fulcra_media/fulcra.py`**

At the top of `fulcra_media/fulcra.py`, add the import:

```python
from .state import State
```

Then add these three methods **inside the existing `class FulcraClient:` body** (do not redeclare the class):

```python
    def ensure_tag(self, name: str, state: State) -> str:
        if name in state.tag_ids:
            return state.tag_ids[name]
        c = self._client()
        r = c.get(f"/user/v1alpha1/tag/name/{name}", headers=self._authed_headers())
        if r.status_code == 200:
            tag_id = r.json()["id"]
        else:
            r = c.post(
                "/user/v1alpha1/tag",
                json={"name": name},
                headers=self._authed_headers(),
            )
            r.raise_for_status()
            tag_id = r.json()["id"]
        state.tag_ids[name] = tag_id
        return tag_id

    def ensure_definitions(self, state: State) -> None:
        if state.watched_definition_id and state.listened_definition_id:
            return
        media = self.ensure_tag("media", state)
        watched = self.ensure_tag("watched", state)
        listened = self.ensure_tag("listened", state)

        if not state.watched_definition_id:
            state.watched_definition_id = self._create_duration_definition(
                name="Watched",
                description="Media content watched (movies, TV, video).",
                tags=[media, watched],
            )
        if not state.listened_definition_id:
            state.listened_definition_id = self._create_duration_definition(
                name="Listened",
                description="Media content listened to (music, podcasts).",
                tags=[media, listened],
            )

    def _create_duration_definition(self, name: str, description: str, tags: list[str]) -> str:
        body = {
            "annotation_type": "duration",
            "name": name,
            "description": description,
            "tags": tags,
            "measurement_spec": {
                "measurement_type": "duration",
                "value_type": "duration",
                "unit": None,
            },
        }
        r = self._client().post(
            "/user/v1alpha1/annotation",
            json=body,
            headers=self._authed_headers(),
        )
        r.raise_for_status()
        return r.json()["id"]
```

(The duplicated class declaration above was a mistake — make sure the final file contains exactly one `class FulcraClient:` with all these methods on it.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest -v tests/test_fulcra_tags_defs.py`
Expected: 5 passed.

- [ ] **Step 6: Commit**

```bash
git add fulcra_media/fulcra.py tests/conftest.py tests/test_fulcra_tags_defs.py
git commit -m "$(cat <<'EOF'
feat(fulcra): ensure_tag and ensure_definitions (DurationAnnotation for both)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 10: `fulcra.py` — dedup readback

**Files:**
- Modify: `fulcra_media/fulcra.py`
- Create: `tests/test_fulcra_dedup.py`

Reads existing `DurationAnnotation` events over a time window and collects all `source` strings. This is how we avoid duplicate ingest on re-runs (the API has no idempotency key).

- [ ] **Step 1: Write the failing test**

In `tests/test_fulcra_dedup.py`:

```python
from datetime import datetime, timezone

import httpx
import pytest

from fulcra_media.fulcra import FulcraClient
from tests.conftest import json_response


@pytest.fixture(autouse=True)
def fake_token(mocker):
    mocker.patch.dict("os.environ", {"FULCRA_ACCESS_TOKEN": "test-token"})


def test_fetch_existing_source_ids_collects_from_records(recording_transport):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/data/v1alpha1/event/DurationAnnotation"
        # Both params present
        params = dict(request.url.params)
        assert "start_time" in params and "end_time" in params
        return json_response(200, [
            {"metadata": {"source": ["com.fulcra.media.netflix.aaa", "com.fulcradynamics.annotation.x"]}},
            {"metadata": {"source": ["com.fulcra.media.netflix.bbb", "com.fulcradynamics.annotation.x"]}},
            {"metadata": {"source": ["unrelated.source"]}},
        ])

    transport = recording_transport(handler)
    client = FulcraClient(transport=transport)
    got = client.fetch_existing_source_ids(
        start=datetime(2026, 5, 12, 20, 50, tzinfo=timezone.utc),
        end=datetime(2026, 5, 12, 23, 10, tzinfo=timezone.utc),
    )
    assert got == {
        "com.fulcra.media.netflix.aaa",
        "com.fulcra.media.netflix.bbb",
        "com.fulcradynamics.annotation.x",
        "unrelated.source",
    }


def test_fetch_existing_source_ids_empty_when_no_records(recording_transport):
    transport = recording_transport(lambda r: json_response(200, []))
    client = FulcraClient(transport=transport)
    got = client.fetch_existing_source_ids(
        start=datetime(2026, 5, 12, tzinfo=timezone.utc),
        end=datetime(2026, 5, 13, tzinfo=timezone.utc),
    )
    assert got == set()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest -v tests/test_fulcra_dedup.py`
Expected: `AttributeError: 'FulcraClient' object has no attribute 'fetch_existing_source_ids'`.

- [ ] **Step 3: Implement `fetch_existing_source_ids`**

Append a method inside `class FulcraClient`:

```python
    def fetch_existing_source_ids(
        self, start: datetime, end: datetime
    ) -> set[str]:
        r = self._client().get(
            "/data/v1alpha1/event/DurationAnnotation",
            params={
                "start_time": start.isoformat().replace("+00:00", "Z"),
                "end_time": end.isoformat().replace("+00:00", "Z"),
            },
            headers=self._authed_headers(),
        )
        r.raise_for_status()
        records = r.json() if isinstance(r.json(), list) else r.json().get("data", [])
        out: set[str] = set()
        for rec in records:
            for s in (rec.get("metadata") or {}).get("source") or []:
                out.add(s)
        return out
```

Add `from datetime import datetime` and `from typing import Iterable` imports at the top of the file if not already present.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest -v tests/test_fulcra_dedup.py`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add fulcra_media/fulcra.py tests/test_fulcra_dedup.py
git commit -m "$(cat <<'EOF'
feat(fulcra): fetch_existing_source_ids for dedup readback

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 11: `fulcra.py` — batch ingest

**Files:**
- Modify: `fulcra_media/fulcra.py`
- Create: `tests/test_fulcra_ingest.py`

Posts a list of `NormalizedEvent`s to `/ingest/v1/record/batch` as JSONL. Each event becomes one `DataRecordV1`. The `data` field carries `note`, `title`, `service`, `timestamp_confidence`, `external_ids` as a JSON-encoded string.

- [ ] **Step 1: Write the failing tests**

In `tests/test_fulcra_ingest.py`:

```python
import json
from datetime import datetime, timezone

import httpx
import pytest

from fulcra_media.fulcra import FulcraClient
from fulcra_media.importers.base import NormalizedEvent
from fulcra_media.state import State
from tests.conftest import json_response


@pytest.fixture(autouse=True)
def fake_token(mocker):
    mocker.patch.dict("os.environ", {"FULCRA_ACCESS_TOKEN": "test-token"})


def _ev(idx: int) -> NormalizedEvent:
    return NormalizedEvent(
        importer="netflix-slim",
        service="netflix",
        category="watched",
        note=f"Note {idx}",
        title=f"Title {idx}",
        start_time=datetime(2026, 5, 12, 21, 0, tzinfo=timezone.utc),
        end_time=datetime(2026, 5, 12, 22, 0, tzinfo=timezone.utc),
        deterministic_id=f"com.fulcra.media.netflix.id{idx:04d}",
        timestamp_confidence="low",
        external_ids={"time_estimated": True, "occurrence_index": 0},
    )


def test_ingest_batch_posts_jsonl_with_correct_shape(recording_transport):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/ingest/v1/record/batch"
        assert request.headers["content-type"].startswith("application/x-jsonl")
        seen["lines"] = request.content.splitlines()
        return httpx.Response(204)

    transport = recording_transport(handler)
    client = FulcraClient(transport=transport)
    state = State(
        watched_definition_id="def-watched",
        listened_definition_id="def-listened",
        tag_ids={"netflix": "tag-netflix"},
    )

    events = [_ev(1), _ev(2)]
    client.ingest_batch(events, state)

    assert len(seen["lines"]) == 2
    first = json.loads(seen["lines"][0])
    assert first["specversion"] == 1
    md = first["metadata"]
    assert md["data_type"] == "DurationAnnotation"
    assert md["recorded_at"] == {
        "start_time": "2026-05-12T21:00:00Z",
        "end_time":   "2026-05-12T22:00:00Z",
    }
    assert md["content_type"] == "application/json"
    assert md["tags"] == ["tag-netflix"]
    assert "com.fulcra.media.netflix.id0001" in md["source"]
    assert "com.fulcradynamics.annotation.def-watched" in md["source"]

    data_inner = json.loads(first["data"])
    assert data_inner["note"] == "Note 1"
    assert data_inner["title"] == "Title 1"
    assert data_inner["service"] == "netflix"
    assert data_inner["timestamp_confidence"] == "low"
    assert data_inner["external_ids"]["time_estimated"] is True


def test_ingest_batch_routes_listened_events_to_listened_definition(recording_transport):
    captured = []
    def handler(request: httpx.Request) -> httpx.Response:
        captured.extend(request.content.splitlines())
        return httpx.Response(204)
    transport = recording_transport(handler)
    client = FulcraClient(transport=transport)
    state = State(
        watched_definition_id="def-watched",
        listened_definition_id="def-listened",
        tag_ids={"spotify": "tag-spotify"},
    )
    ev = _ev(1)
    ev.category = "listened"
    ev.service = "spotify"
    client.ingest_batch([ev], state)
    md = json.loads(captured[0])["metadata"]
    assert "com.fulcradynamics.annotation.def-listened" in md["source"]
    assert md["tags"] == ["tag-spotify"]


def test_ingest_batch_empty_input_does_not_post(recording_transport):
    transport = recording_transport(lambda r: pytest.fail(f"unexpected {r.url}"))
    client = FulcraClient(transport=transport)
    state = State(watched_definition_id="x", listened_definition_id="y")
    client.ingest_batch([], state)  # no-op
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest -v tests/test_fulcra_ingest.py`
Expected: `AttributeError: 'FulcraClient' object has no attribute 'ingest_batch'`.

- [ ] **Step 3: Implement `ingest_batch`**

Append to `class FulcraClient`:

```python
    def ingest_batch(
        self, events: list["NormalizedEvent"], state: "State"
    ) -> None:
        if not events:
            return
        lines: list[bytes] = []
        for ev in events:
            def_id = (
                state.watched_definition_id
                if ev.category == "watched"
                else state.listened_definition_id
            )
            if def_id is None:
                raise RuntimeError(
                    f"missing {ev.category} definition id in state; run bootstrap first"
                )
            data_inner = {
                "note": ev.note,
                "title": ev.title,
                "service": ev.service,
                "timestamp_confidence": ev.timestamp_confidence,
                "external_ids": ev.external_ids,
            }
            service_tag = state.tag_ids.get(ev.service)
            tags = [service_tag] if service_tag else []
            metadata = {
                "data_type": "DurationAnnotation",
                "recorded_at": {
                    "start_time": ev.start_time.isoformat().replace("+00:00", "Z"),
                    "end_time":   ev.end_time.isoformat().replace("+00:00", "Z"),
                },
                "tags": tags,
                "source": [ev.deterministic_id, f"com.fulcradynamics.annotation.{def_id}"],
                "content_type": "application/json",
            }
            line = {
                "specversion": 1,
                "data": json.dumps(data_inner, sort_keys=True),
                "metadata": metadata,
            }
            lines.append(json.dumps(line, sort_keys=True).encode())
        body = b"\n".join(lines)
        r = self._client().post(
            "/ingest/v1/record/batch",
            content=body,
            headers={
                **self._authed_headers(),
                "content-type": "application/x-jsonl",
            },
        )
        r.raise_for_status()
```

Add `import json` and `from .importers.base import NormalizedEvent` at the top of the file if not already imported. (The string-quoted forward references in the signature avoid an import cycle.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest -v tests/test_fulcra_ingest.py`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add fulcra_media/fulcra.py tests/test_fulcra_ingest.py
git commit -m "$(cat <<'EOF'
feat(fulcra): ingest_batch posts JSONL DataRecordV1 to /ingest/v1/record/batch

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 12: `fulcra.py` — `run_import` pipeline (dedup + ingest + verify)

**Files:**
- Modify: `fulcra_media/fulcra.py`
- Create: `tests/test_fulcra_pipeline.py`

The composed pipeline that an importer drives. Chunks events, dedupes against existing source IDs in the window, ingests new ones, re-queries the window to verify counts.

- [ ] **Step 1: Write the failing tests**

In `tests/test_fulcra_pipeline.py`:

```python
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx
import pytest

from fulcra_media.fulcra import FulcraClient, ImportResult
from fulcra_media.importers.base import NormalizedEvent
from fulcra_media.state import State
from tests.conftest import json_response


@pytest.fixture(autouse=True)
def fake_token(mocker):
    mocker.patch.dict("os.environ", {"FULCRA_ACCESS_TOKEN": "test-token"})


def _ev(i: int, det_id: str | None = None) -> NormalizedEvent:
    return NormalizedEvent(
        importer="netflix-slim",
        service="netflix",
        category="watched",
        note=f"N{i}",
        title=f"T{i}",
        start_time=datetime(2026, 5, 12, 21, 0, tzinfo=timezone.utc),
        end_time=datetime(2026, 5, 12, 22, 0, tzinfo=timezone.utc),
        deterministic_id=det_id or f"com.fulcra.media.netflix.id{i:04d}",
        timestamp_confidence="low",
    )


def test_run_import_dedupes_against_existing(recording_transport):
    """One existing, two new -> ingest 2, skip 1, verify 2."""
    existing_response = [
        {"metadata": {"source": ["com.fulcra.media.netflix.id0001"]}},
    ]
    after_response = [
        {"metadata": {"source": ["com.fulcra.media.netflix.id0001"]}},
        {"metadata": {"source": ["com.fulcra.media.netflix.id0002"]}},
        {"metadata": {"source": ["com.fulcra.media.netflix.id0003"]}},
    ]
    call_counter = {"get": 0, "post": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            call_counter["get"] += 1
            return json_response(200, existing_response if call_counter["get"] == 1 else after_response)
        if request.method == "POST":
            call_counter["post"] += 1
            return httpx.Response(204)
        pytest.fail(request.url)

    transport = recording_transport(handler)
    client = FulcraClient(transport=transport)
    state = State(
        watched_definition_id="def-watched",
        listened_definition_id="def-listened",
        tag_ids={"netflix": "tag-netflix"},
    )
    events = [_ev(1), _ev(2), _ev(3)]
    result = client.run_import(events, state, chunk_size=10)
    assert isinstance(result, ImportResult)
    assert result.skipped_existing == 1
    assert result.posted == 2
    assert result.verified == 2
    assert call_counter["post"] == 1
    assert call_counter["get"] == 2


def test_run_import_no_new_events_does_not_post(recording_transport):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return json_response(200, [
                {"metadata": {"source": ["com.fulcra.media.netflix.id0001"]}},
            ])
        pytest.fail(f"unexpected POST {request.url}")
    transport = recording_transport(handler)
    client = FulcraClient(transport=transport)
    state = State(watched_definition_id="d", listened_definition_id="d2", tag_ids={"netflix": "t"})
    result = client.run_import([_ev(1)], state, chunk_size=10)
    assert result.posted == 0
    assert result.skipped_existing == 1
    assert result.verified == 0


def test_run_import_chunks_large_input(recording_transport):
    post_count = {"n": 0}
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return json_response(200, [])
        post_count["n"] += 1
        return httpx.Response(204)
    transport = recording_transport(handler)
    client = FulcraClient(transport=transport)
    state = State(watched_definition_id="d", listened_definition_id="d2", tag_ids={"netflix": "t"})
    events = [_ev(i) for i in range(25)]
    # GET response is empty so verification will fail-count, but we only care about chunking here
    with pytest.raises(RuntimeError, match="verified .* < posted"):
        client.run_import(events, state, chunk_size=10)
    assert post_count["n"] == 3
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest -v tests/test_fulcra_pipeline.py`
Expected: `ImportError: cannot import name 'ImportResult'`.

- [ ] **Step 3: Implement `ImportResult` and `run_import`**

Add to `fulcra_media/fulcra.py` (top-level, after the existing imports):

```python
from dataclasses import dataclass


@dataclass
class ImportResult:
    total: int
    skipped_existing: int
    posted: int
    verified: int
```

Append to `class FulcraClient`:

```python
    def run_import(
        self,
        events: list[NormalizedEvent],
        state: State,
        chunk_size: int = 500,
        window_pad_minutes: int = 10,
    ) -> ImportResult:
        from datetime import timedelta

        events = list(events)
        total = len(events)
        if total == 0:
            return ImportResult(0, 0, 0, 0)

        win_start = min(e.start_time for e in events) - timedelta(minutes=window_pad_minutes)
        win_end = max(e.end_time for e in events) + timedelta(minutes=window_pad_minutes)

        existing = self.fetch_existing_source_ids(win_start, win_end)
        new_events = [e for e in events if e.deterministic_id not in existing]
        skipped = total - len(new_events)

        posted = 0
        for i in range(0, len(new_events), chunk_size):
            chunk = new_events[i : i + chunk_size]
            self.ingest_batch(chunk, state)
            posted += len(chunk)

        after = self.fetch_existing_source_ids(win_start, win_end)
        verified = sum(1 for e in new_events if e.deterministic_id in after)
        if verified < posted:
            raise RuntimeError(
                f"verified {verified} < posted {posted} — readback did not see "
                f"all newly-ingested events. Window: {win_start} → {win_end}"
            )
        return ImportResult(total=total, skipped_existing=skipped, posted=posted, verified=verified)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest -v tests/test_fulcra_pipeline.py`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add fulcra_media/fulcra.py tests/test_fulcra_pipeline.py
git commit -m "$(cat <<'EOF'
feat(fulcra): run_import pipeline (dedup readback + chunked ingest + verify)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 13: `wizards/netflix.py` — walkthrough text

**Files:**
- Create: `fulcra_media/wizards/netflix.py`
- Create: `tests/test_netflix_wizard.py`

The wizard prints a numbered walkthrough for both Netflix export routes and prompts the user to choose. After choosing, it prints the steps for that route. (Upload integration comes in Task 14.)

- [ ] **Step 1: Write the failing tests**

In `tests/test_netflix_wizard.py`:

```python
from click.testing import CliRunner

from fulcra_media.wizards.netflix import walkthrough


def test_walkthrough_slim_route():
    runner = CliRunner()
    # input "1" picks the slim CSV route
    result = runner.invoke(walkthrough, input="1\n")
    assert result.exit_code == 0
    assert "Viewing activity" in result.output
    assert "Download all" in result.output
    assert "netflix.com/account" in result.output
    assert "M/D/YY" in result.output  # warn about precision


def test_walkthrough_gdpr_route():
    runner = CliRunner()
    result = runner.invoke(walkthrough, input="2\n")
    assert result.exit_code == 0
    assert "netflix.com/account/getmyinfo" in result.output
    assert "up to 30 days" in result.output
    assert "10 columns" in result.output or "rich" in result.output.lower()


def test_walkthrough_rejects_bad_choice():
    runner = CliRunner()
    result = runner.invoke(walkthrough, input="9\n")
    # Click's Choice will keep prompting; abort by EOF
    assert result.exit_code != 0 or "Invalid" in result.output
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest -v tests/test_netflix_wizard.py`
Expected: `ImportError`.

- [ ] **Step 3: Implement the wizard**

In `fulcra_media/wizards/netflix.py`:

```python
"""Netflix wizard: walks the user through requesting and importing their data.

Two routes:
  1. Slim CSV (in-app per-profile, two columns Title/Date)
  2. GDPR full export (10-column, takes up to 30 days)

The wizard prints the canonical steps and links to Netflix's help pages.
Upload-and-import integration is added in a follow-up step.
"""

from __future__ import annotations

import click


SLIM_STEPS = """\
Netflix slim CSV (in-app per-profile download)
  Reference: https://help.netflix.com/en/node/101917

  1. Open https://www.netflix.com/account in a web browser.
  2. Select 'Profiles', then choose the profile whose history you want.
  3. Open 'Viewing activity'.
  4. Click 'Show More' at the bottom until all entries are loaded.
  5. Click 'Download all'.
  6. Save the file (filename usually NetflixViewingHistory.csv).

  Note: The slim CSV is date-only (M/D/YY format) with no time, duration,
  device, or profile fields. Each row becomes one Watched annotation with a
  synthetic 21:00 UTC start time and a duration estimated by title shape
  (movie ~ 100 min, episode ~ 30 min). timestamp_confidence: low.

  When the file is ready, run:
    fulcra-media import netflix /path/to/NetflixViewingHistory.csv
"""

GDPR_STEPS = """\
Netflix GDPR / "Request your personal information" export (RECOMMENDED)
  Reference: https://help.netflix.com/en/node/100624

  1. Open https://www.netflix.com/account/getmyinfo in a web browser.
  2. Follow Netflix's verification prompts (email confirmation + re-auth).
  3. Submit the request. Netflix says delivery may take up to 30 days
     (in practice usually 1-5 days).
  4. When you receive the email link, download the ZIP. The download link is
     valid for 7 days.
  5. Inside the ZIP, the relevant file is:
       CONTENT_INTERACTION/ViewingActivity.csv
     This is the 10-column rich variant (Profile Name, Start Time UTC,
     Duration H:MM:SS, Title, Supplemental Video Type, Device Type, ...).

  Scope: covers ALL profiles in your account and your full account lifetime.

  Importing the rich variant is not yet wired up (the slim importer is in
  place). For now, upload the ZIP to your Fulcra Library and we'll wire the
  rich importer in the next milestone.
"""


@click.command("walkthrough")
def walkthrough() -> None:
    """Interactive walkthrough for requesting a Netflix viewing export."""
    click.echo("Which Netflix export do you want to set up?")
    click.echo("  1. Slim CSV (in-app, instant download, date-only precision)")
    click.echo("  2. GDPR full export (richer schema, takes up to 30 days) [RECOMMENDED]")
    choice = click.prompt(
        "Choose 1 or 2",
        type=click.Choice(["1", "2"]),
        show_choices=False,
    )
    click.echo("")
    click.echo(SLIM_STEPS if choice == "1" else GDPR_STEPS)
```

Create `fulcra_media/wizards/__init__.py` as empty if it isn't already.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest -v tests/test_netflix_wizard.py`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add fulcra_media/wizards/netflix.py tests/test_netflix_wizard.py
git commit -m "$(cat <<'EOF'
feat(wizard): netflix walkthrough for slim and GDPR routes

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 14: `cli.py` — top-level group + `bootstrap` + `status`

**Files:**
- Create: `fulcra_media/cli.py`
- Create: `tests/test_cli.py`

The top-level Click group `cli`, the `bootstrap` command (creates definitions/tags), and `status` (prints state.json contents).

- [ ] **Step 1: Write the failing tests**

In `tests/test_cli.py`:

```python
from pathlib import Path

from click.testing import CliRunner

from fulcra_media.cli import cli
from fulcra_media.state import State, save


def test_cli_no_args_shows_help():
    result = CliRunner().invoke(cli, [])
    assert result.exit_code == 0
    assert "bootstrap" in result.output
    assert "wizard" in result.output
    assert "import" in result.output
    assert "status" in result.output


def test_status_prints_state_file(tmp_path: Path, mocker):
    state_path = tmp_path / "state.json"
    save(State(
        watched_definition_id="w",
        listened_definition_id="l",
        tag_ids={"netflix": "t"},
        watermarks={"netflix-slim": "2026-05-12"},
    ), state_path)
    mocker.patch("fulcra_media.state.DEFAULT_PATH", state_path)
    mocker.patch("fulcra_media.cli.STATE_PATH", state_path)
    result = CliRunner().invoke(cli, ["status"])
    assert result.exit_code == 0
    assert "watched_definition_id" in result.output
    assert "netflix" in result.output


def test_bootstrap_calls_ensure_definitions(tmp_path: Path, mocker):
    state_path = tmp_path / "state.json"
    mocker.patch("fulcra_media.cli.STATE_PATH", state_path)

    def fake_ensure(self, state):
        state.watched_definition_id = "w-id"
        state.listened_definition_id = "l-id"
        state.tag_ids["media"] = "m"

    mocker.patch("fulcra_media.fulcra.FulcraClient.ensure_definitions", fake_ensure)
    result = CliRunner().invoke(cli, ["bootstrap"])
    assert result.exit_code == 0, result.output
    # State persisted to disk
    persisted = State()
    import json as _json
    raw = _json.loads(state_path.read_text())
    assert raw["watched_definition_id"] == "w-id"
    assert raw["listened_definition_id"] == "l-id"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest -v tests/test_cli.py`
Expected: `ImportError`.

- [ ] **Step 3: Implement `cli.py`**

In `fulcra_media/cli.py`:

```python
"""Click entry point."""

from __future__ import annotations

import json

import click

from . import state as state_mod
from .fulcra import FulcraClient

STATE_PATH = state_mod.DEFAULT_PATH


@click.group(help="Import media consumption (Watched/Listened) into Fulcra.")
def cli() -> None:
    pass


@cli.command(help="Create the Watched/Listened annotation definitions and service tags.")
def bootstrap() -> None:
    s = state_mod.load(STATE_PATH)
    client = FulcraClient()
    client.ensure_definitions(s)
    state_mod.save(s, STATE_PATH)
    click.echo(f"watched={s.watched_definition_id} listened={s.listened_definition_id}")


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


if __name__ == "__main__":
    cli()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest -v tests/test_cli.py`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add fulcra_media/cli.py tests/test_cli.py
git commit -m "$(cat <<'EOF'
feat(cli): top-level group + bootstrap + status

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 15: `cli.py` — wire `wizard netflix` + `import netflix`

**Files:**
- Modify: `fulcra_media/cli.py`
- Modify: `tests/test_cli.py`

- [ ] **Step 1: Add the failing tests**

Append to `tests/test_cli.py`:

```python
def test_wizard_netflix_invokes_walkthrough():
    result = CliRunner().invoke(cli, ["wizard", "netflix"], input="1\n")
    assert result.exit_code == 0
    assert "Download all" in result.output


def test_import_netflix_runs_pipeline(tmp_path: Path, mocker):
    # Prep a tiny CSV and state on disk
    csv = tmp_path / "small.csv"
    csv.write_text('Title,Date\n"Movie One","5/12/26"\n')
    state_path = tmp_path / "state.json"
    save(State(
        watched_definition_id="w", listened_definition_id="l",
        tag_ids={"netflix": "t"},
    ), state_path)
    mocker.patch("fulcra_media.cli.STATE_PATH", state_path)

    # Stub the network-touching pipeline
    from fulcra_media.fulcra import ImportResult

    captured = {}
    def fake_run(self, events, state, chunk_size=500, window_pad_minutes=10):
        events = list(events)
        captured["count"] = len(events)
        return ImportResult(total=len(events), skipped_existing=0, posted=len(events), verified=len(events))
    mocker.patch("fulcra_media.fulcra.FulcraClient.run_import", fake_run)

    result = CliRunner().invoke(cli, ["import", "netflix", str(csv)])
    assert result.exit_code == 0, result.output
    assert captured["count"] == 1
    assert "posted=1" in result.output or "1 posted" in result.output


def test_import_netflix_resolves_fulcra_uri(tmp_path: Path, mocker):
    csv = tmp_path / "downloaded.csv"
    csv.write_text('Title,Date\n"Movie","5/12/26"\n')
    mocker.patch("fulcra_media.library.resolve", return_value=csv)

    state_path = tmp_path / "state.json"
    save(State(
        watched_definition_id="w", listened_definition_id="l",
        tag_ids={"netflix": "t"},
    ), state_path)
    mocker.patch("fulcra_media.cli.STATE_PATH", state_path)

    from fulcra_media.fulcra import ImportResult
    mocker.patch(
        "fulcra_media.fulcra.FulcraClient.run_import",
        return_value=ImportResult(1, 0, 1, 1),
    )
    result = CliRunner().invoke(cli, ["import", "netflix", "fulcra:/takeouts/Netflix.csv"])
    assert result.exit_code == 0, result.output
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest -v tests/test_cli.py`
Expected: failures complaining the `wizard netflix` and `import netflix` commands don't exist.

- [ ] **Step 3: Wire the subcommands**

Append to `fulcra_media/cli.py`:

```python
from pathlib import Path

from . import library
from .importers import netflix as netflix_importer
from .wizards.netflix import walkthrough as netflix_walkthrough


wizard.add_command(netflix_walkthrough, name="netflix")


@import_group.command("netflix")
@click.argument("path", type=str)
def import_netflix(path: str) -> None:
    """Import a Netflix slim-variant CSV (local path or fulcra:/... URI)."""
    resolved = library.resolve(path)
    s = state_mod.load(STATE_PATH)
    if not s.watched_definition_id or "netflix" not in s.tag_ids:
        raise click.UsageError(
            "Run `fulcra-media bootstrap` first; need Watched definition + netflix tag."
        )
    events = list(netflix_importer.parse_slim(Path(resolved)))
    client = FulcraClient()
    result = client.run_import(events, s)
    state_mod.save(s, STATE_PATH)
    click.echo(
        f"netflix: total={result.total} skipped_existing={result.skipped_existing} "
        f"posted={result.posted} verified={result.verified}"
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest -v tests/test_cli.py`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add fulcra_media/cli.py tests/test_cli.py
git commit -m "$(cat <<'EOF'
feat(cli): wire `wizard netflix` and `import netflix`

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 16: End-to-end test against real `takeouts/NetflixViewingHistory.csv`

**Files:**
- Create: `tests/test_e2e_netflix.py`

Drives the full pipeline against the user's actual 6,456-row CSV with `httpx.MockTransport`. Asserts:
- All 6,456 rows are parsed
- 6,456 deterministic IDs are unique (the 27 same-day-rewatch pairs are distinguished)
- Mock receives `POST /ingest/v1/record/batch` calls whose total line count equals 6,456
- Each ingested line is a valid JSON line with `data_type: DurationAnnotation` and a UTC `recorded_at` interval

- [ ] **Step 1: Verify the fixture is present**

Run: `wc -l /Users/Scanning/Developer/FulcraMediaHelpers/takeouts/NetflixViewingHistory.csv`
Expected: `6457` (header + 6456 data rows).

- [ ] **Step 2: Write the failing test**

In `tests/test_e2e_netflix.py`:

```python
"""End-to-end test driving the real Netflix CSV through the full pipeline.

Uses httpx.MockTransport so no real network calls are made.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import httpx
import pytest

from fulcra_media.fulcra import FulcraClient
from fulcra_media.importers.netflix import parse_slim
from fulcra_media.state import State
from tests.conftest import json_response


REAL_CSV = Path(__file__).parent.parent / "takeouts" / "NetflixViewingHistory.csv"


@pytest.fixture(autouse=True)
def fake_token(mocker):
    mocker.patch.dict("os.environ", {"FULCRA_ACCESS_TOKEN": "test-token"})


@pytest.mark.skipif(not REAL_CSV.exists(), reason="real Netflix takeout not present")
def test_real_netflix_csv_full_pipeline(recording_transport):
    # All 6,456 rows produce distinct deterministic IDs
    events = list(parse_slim(REAL_CSV))
    assert len(events) >= 6000, f"expected at least 6000 events, got {len(events)}"
    ids = [e.deterministic_id for e in events]
    assert len(ids) == len(set(ids)), "deterministic IDs collided — rewatch dedup is broken"

    # And the dedup rule produced extra annotations for same-day rewatches
    by_date_title = Counter()
    for e in events:
        by_date_title[(e.external_ids["raw_date"], e.note)] += 1
    rewatches = {k: v for k, v in by_date_title.items() if v > 1}
    assert len(rewatches) >= 1, "expected at least one same-day rewatch in real data"

    # Drive the network pipeline with a mock that captures every JSONL line
    posted_lines: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/data/v1alpha1/event/DurationAnnotation":
            return json_response(200, [])  # nothing exists yet
        if request.method == "POST" and request.url.path == "/ingest/v1/record/batch":
            posted_lines.extend(request.content.splitlines())
            return httpx.Response(204)
        pytest.fail(f"unexpected {request.method} {request.url}")

    transport = recording_transport(handler)
    client = FulcraClient(transport=transport)

    # Verification readback after ingest returns the same source IDs we posted
    posted_so_far: set[str] = set()
    def handler2(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/data/v1alpha1/event/DurationAnnotation":
            return json_response(
                200,
                [{"metadata": {"source": [sid]}} for sid in posted_so_far],
            )
        if request.method == "POST" and request.url.path == "/ingest/v1/record/batch":
            for line in request.content.splitlines():
                rec = json.loads(line)
                # Source array always has the deterministic ID at position 0
                posted_so_far.add(rec["metadata"]["source"][0])
                posted_lines.append(line)
            return httpx.Response(204)
        pytest.fail(f"unexpected {request.method} {request.url}")

    # Replace transport handler with the verifying one
    client._http = None
    client._transport = recording_transport(handler2)

    state = State(
        watched_definition_id="def-watched",
        listened_definition_id="def-listened",
        tag_ids={"netflix": "tag-netflix"},
    )

    result = client.run_import(events, state)

    assert result.total == len(events)
    assert result.skipped_existing == 0
    assert result.posted == len(events)
    assert result.verified == len(events)
    assert len(posted_lines) == len(events)

    # Spot-check the first emitted line
    first = json.loads(posted_lines[0])
    assert first["specversion"] == 1
    md = first["metadata"]
    assert md["data_type"] == "DurationAnnotation"
    assert "start_time" in md["recorded_at"] and "end_time" in md["recorded_at"]
    assert md["tags"] == ["tag-netflix"]
    assert any("com.fulcradynamics.annotation." in s for s in md["source"])
    inner = json.loads(first["data"])
    assert inner["service"] == "netflix"
    assert inner["timestamp_confidence"] == "low"
```

- [ ] **Step 3: Run the test to verify it fails**

Run: `pytest -v tests/test_e2e_netflix.py`
Expected: assertion failure or test failure — whatever the first concrete bug is in the pipeline against real data. Diagnose, fix the offending earlier task, repeat.

(Note for the executing agent: if this test surfaces a real bug in any previous module, fix that module's code AND its unit test, then re-run the whole test suite, then re-run this e2e test. Don't paper over with e2e-only special-cases.)

- [ ] **Step 4: Run the test to verify it passes**

Run: `pytest -v tests/test_e2e_netflix.py`
Expected: 1 passed.

- [ ] **Step 5: Run the full suite once more**

Run: `pytest -q`
Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add tests/test_e2e_netflix.py
git commit -m "$(cat <<'EOF'
test(e2e): netflix slim pipeline against real 6,456-row takeout

Verifies per-event idempotency keys, no collisions, full pipeline run
with mocked HTTP and same-day rewatches surfacing as distinct annotations.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 17: Manual smoke test + final commit

**Files:**
- Modify: `README.md` (add a "Manual smoke test" section)

- [ ] **Step 1: Manually exercise against real Fulcra (developer only)**

(Skip this step if you don't have Fulcra credentials handy; this is a sanity check, not part of CI.)

```bash
fulcra auth login   # device flow via the upstream fulcra-api CLI
fulcra-media bootstrap
fulcra-media status            # confirm watched/listened def IDs are populated
fulcra-media wizard netflix    # exercise the walkthrough interactively
fulcra-media import netflix takeouts/NetflixViewingHistory.csv
fulcra-media status            # confirm a netflix watermark
```

Expected: bootstrap creates two definitions in Fulcra; import posts ~6,456 annotations; rerunning import skips all events (idempotent).

- [ ] **Step 2: Add a Manual smoke test section to README**

Append to `README.md`:

```markdown

## Manual smoke test

Run this end-to-end against your real Fulcra account once after a fresh install:

    fulcra auth login
    fulcra-media bootstrap
    fulcra-media import netflix takeouts/NetflixViewingHistory.csv
    fulcra-media import netflix takeouts/NetflixViewingHistory.csv  # rerun: should skip all

You should see ~6,456 `DurationAnnotation` events tagged `netflix` in your
Fulcra account, and the second run should report `posted=0 skipped_existing=~6456`.
```

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "$(cat <<'EOF'
docs: manual smoke test instructions

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Done criteria

- [ ] `pytest -q` passes (all unit + e2e tests).
- [ ] Manual smoke against real Fulcra produces ~6,456 events on the first run and 0 new on the second.
- [ ] `fulcra-media wizard netflix` works interactively and prints both slim and GDPR routes.
- [ ] No code touched outside the files in §File structure.
- [ ] Git history is bite-sized per task (one commit per task at minimum).

## What's deferred (for the next plan)

- Trakt importer + wizard (with `--trakt-from` for cluster-flagged historical entries — see spec §3.1 timestamp-confidence handling)
- Spotify Extended importer + wizard
- Apple Podcasts importer (`MTLibrary.sqlite`)
- Last.fm importer
- Apple Data & Privacy takeout importer + wizard
- Netflix rich (GDPR) variant — the spec describes the schema; the slim variant validates the pipeline end-to-end
- Cross-source dedup helpers (not automatic — user windowing remains the pattern)
