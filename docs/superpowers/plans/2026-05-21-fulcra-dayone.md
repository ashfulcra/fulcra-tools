# fulcra-dayone Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A new `fulcra-tools` monorepo package that imports selected Day One journal entries into Fulcra as InstantAnnotations under a "Journal" definition.

**Architecture:** A Day One reader (JSON export `.zip`/folder, or the local Core Data SQLite) produces `DayOneEntry` records; a filter selects a subset; a converter maps each to a `fulcra_csv.GenericEvent`; ingest reuses `fulcra_csv.FulcraClient.run_import` (dedup-readback + chunked POST). One small additive change to `fulcra-csv-importer` (`GenericEvent.extra_tags`) lets a Day One entry's multiple tags become multiple Fulcra tags.

**Tech Stack:** Python 3.11+, `click` (CLI), `httpx` (via the inherited Fulcra client), stdlib `sqlite3`/`zipfile`/`json`, `pytest`, the `uv` workspace.

**Spec:** `docs/superpowers/specs/2026-05-21-fulcra-dayone-design.md`

---

## File Structure

New package `packages/dayone/`:

| File | Responsibility |
|---|---|
| `pyproject.toml` | Package metadata; workspace deps `fulcra-common`, `fulcra-csv-importer`. |
| `README.md` | Usage docs. |
| `fulcra_dayone/__init__.py` | Package marker / docstring. |
| `fulcra_dayone/entry.py` | `DayOneEntry` — the reader-agnostic entry model. |
| `fulcra_dayone/convert.py` | `to_event` — `DayOneEntry` → `fulcra_csv.GenericEvent`. |
| `fulcra_dayone/filter.py` | `select` — apply the four selection filters. |
| `fulcra_dayone/readers/__init__.py` | `read` — dispatch to the right reader. |
| `fulcra_dayone/readers/json_export.py` | `read_json_export` — `.zip`/folder JSON export. |
| `fulcra_dayone/readers/local_db.py` | `read_local_db`, `find_database` — local SQLite. |
| `fulcra_dayone/client.py` | `DayOneFulcraClient` — adds `ensure_journal_definition`. |
| `fulcra_dayone/cli.py` | The `fulcra-dayone` Click CLI. |
| `tests/conftest.py` | `recording_transport` fixture + DB-fixture builder. |
| `tests/test_*.py` | One test module per source module. |

Modified in `packages/csv-importer/`:

| File | Change |
|---|---|
| `fulcra_csv/events.py` | `GenericEvent` gains `extra_tags: tuple[str, ...] = ()`. |
| `fulcra_csv/fulcra.py` | `_build_record` resolves `extra_tags` into the tags array. |
| `tests/test_extra_tags.py` | New tests for the above. |

All commands run from the monorepo root `/Users/Scanning/Developer/fulcra-tools`.

---

### Task 1: Add `extra_tags` to fulcra-csv-importer's GenericEvent

**Files:**
- Modify: `packages/csv-importer/fulcra_csv/events.py`
- Modify: `packages/csv-importer/fulcra_csv/fulcra.py`
- Test: `packages/csv-importer/tests/test_extra_tags.py`

- [ ] **Step 1: Write the failing test**

Create `packages/csv-importer/tests/test_extra_tags.py`:

```python
"""GenericEvent.extra_tags -> multiple tag ids in the built record."""
from __future__ import annotations

from datetime import datetime, timezone

from fulcra_csv.events import INSTANT, GenericEvent
from fulcra_csv.fulcra import FulcraClient


def _event(**kw) -> GenericEvent:
    base = dict(
        start_time=datetime(2026, 5, 21, tzinfo=timezone.utc),
        note="hi", title="hi", source_id="s1", annotation_type=INSTANT,
    )
    base.update(kw)
    return GenericEvent(**base)


def test_build_record_includes_tag_and_extra_tags_in_order():
    rec = FulcraClient()._build_record(
        _event(tag="primary", extra_tags=("alpha", "beta")),
        definition_id="def-1",
        tag_id_for={"primary": "t-p", "alpha": "t-a", "beta": "t-b"},
        data_type=None,
    )
    assert rec["metadata"]["tags"] == ["t-p", "t-a", "t-b"]


def test_build_record_dedupes_repeated_tag_ids():
    rec = FulcraClient()._build_record(
        _event(tag="x", extra_tags=("x", "y")),
        definition_id="def-1",
        tag_id_for={"x": "t-x", "y": "t-y"},
        data_type=None,
    )
    assert rec["metadata"]["tags"] == ["t-x", "t-y"]


def test_build_record_extra_tags_default_empty():
    rec = FulcraClient()._build_record(
        _event(),
        definition_id=None, tag_id_for={}, data_type=None,
    )
    assert rec["metadata"]["tags"] == []
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run --package fulcra-csv-importer pytest packages/csv-importer/tests/test_extra_tags.py -v`
Expected: FAIL — `TypeError: GenericEvent.__init__() got an unexpected keyword argument 'extra_tags'`.

- [ ] **Step 3: Add the `extra_tags` field to `GenericEvent`**

In `packages/csv-importer/fulcra_csv/events.py`, in the `GenericEvent` dataclass, add `extra_tags` immediately after the `tag` field:

```python
    tag: str | None = None
    extra_tags: tuple[str, ...] = ()
    value: Any = None
```

- [ ] **Step 4: Resolve `extra_tags` in `_build_record`**

In `packages/csv-importer/fulcra_csv/fulcra.py`, in `FulcraClient._build_record`, replace these two lines:

```python
        tag_id = tag_id_for.get(ev.tag or "") if ev.tag else None
        tags = [tag_id] if tag_id else []
```

with:

```python
        # Resolve the single `tag` plus any `extra_tags` to tag ids, in
        # order, de-duplicated.
        tag_ids: list[str] = []
        for name in ([ev.tag] if ev.tag else []) + list(ev.extra_tags):
            tid = tag_id_for.get(name)
            if tid and tid not in tag_ids:
                tag_ids.append(tid)
```

Then in the `return {...}` of `_build_record`, change `"tags": tags,` to `"tags": tag_ids,`.

- [ ] **Step 5: Run the test to verify it passes**

Run: `uv run --package fulcra-csv-importer pytest packages/csv-importer/tests/test_extra_tags.py -v`
Expected: PASS — 3 tests.

- [ ] **Step 6: Run the full csv-importer suite (no regressions)**

Run: `uv run --package fulcra-csv-importer pytest packages/csv-importer -q`
Expected: PASS — 70 prior tests + 3 new = 73 passed.

- [ ] **Step 7: Commit**

```bash
git add packages/csv-importer/fulcra_csv/events.py packages/csv-importer/fulcra_csv/fulcra.py packages/csv-importer/tests/test_extra_tags.py
git commit -m "feat(csv-importer): GenericEvent.extra_tags for multi-tag annotations"
```

---

### Task 2: Scaffold the fulcra-dayone package

**Files:**
- Create: `packages/dayone/pyproject.toml`
- Create: `packages/dayone/fulcra_dayone/__init__.py`
- Create: `packages/dayone/tests/conftest.py`

- [ ] **Step 1: Create `packages/dayone/pyproject.toml`**

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "fulcra-dayone"
version = "0.1.0"
description = "Import selected Day One journal entries into Fulcra as annotations."
requires-python = ">=3.11"
dependencies = [
    "click>=8.1",
    "httpx>=0.27",
    "fulcra-common",
    "fulcra-csv-importer",
]

[project.scripts]
fulcra-dayone = "fulcra_dayone.cli:cli"

[tool.uv.sources]
fulcra-common = { workspace = true }
fulcra-csv-importer = { workspace = true }

[project.optional-dependencies]
dev = [
    "pytest>=7.4,<8",
    "ruff>=0.5",
]

[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-ra --strict-markers"

[tool.hatch.build.targets.wheel]
packages = ["fulcra_dayone"]
```

- [ ] **Step 2: Create `packages/dayone/fulcra_dayone/__init__.py`**

```python
"""Import selected Day One journal entries into Fulcra as annotations."""
```

- [ ] **Step 3: Create `packages/dayone/tests/conftest.py`**

```python
"""Shared test fixtures for fulcra-dayone."""
from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest


class RecordingTransport(httpx.MockTransport):
    """MockTransport that records every request it sees."""

    def __init__(self, handler: Callable[[httpx.Request], httpx.Response]) -> None:
        self.requests: list[httpx.Request] = []

        def wrapper(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return handler(request)

        super().__init__(wrapper)


@pytest.fixture
def recording_transport():
    def make(handler: Callable[[httpx.Request], httpx.Response]) -> RecordingTransport:
        return RecordingTransport(handler)
    return make
```

- [ ] **Step 4: Sync the workspace**

Run: `uv sync --all-extras`
Expected: success; output includes `fulcra-dayone==0.1.0` among the installed/edited packages. The root `pyproject.toml` already declares `members = ["packages/*"]`, so no root change is needed.

- [ ] **Step 5: Verify the package imports**

Run: `uv run --package fulcra-dayone python -c "import fulcra_dayone; print('ok')"`
Expected: prints `ok`.

- [ ] **Step 6: Commit**

```bash
git add packages/dayone/pyproject.toml packages/dayone/fulcra_dayone/__init__.py packages/dayone/tests/conftest.py uv.lock
git commit -m "chore(dayone): scaffold the fulcra-dayone package"
```

---

### Task 3: The DayOneEntry model

**Files:**
- Create: `packages/dayone/fulcra_dayone/entry.py`
- Test: `packages/dayone/tests/test_entry.py`

- [ ] **Step 1: Write the failing test**

Create `packages/dayone/tests/test_entry.py`:

```python
"""DayOneEntry is the reader-agnostic entry model."""
from __future__ import annotations

from datetime import datetime, timezone

from fulcra_dayone.entry import DayOneEntry


def test_dayone_entry_holds_all_fields():
    e = DayOneEntry(
        uuid="ABC123",
        creation_date=datetime(2026, 5, 21, 9, 0, tzinfo=timezone.utc),
        text="Today I learned.",
        tags=("learning",),
        starred=True,
        journal="Personal",
        location="Seattle",
        photo_count=2,
        word_count=3,
    )
    assert e.uuid == "ABC123"
    assert e.tags == ("learning",)
    assert e.starred is True
    assert e.location == "Seattle"


def test_dayone_entry_is_frozen():
    import dataclasses
    e = DayOneEntry(
        uuid="X", creation_date=datetime(2026, 5, 21, tzinfo=timezone.utc),
        text="t", tags=(), starred=False, journal="J",
        location=None, photo_count=0, word_count=1,
    )
    try:
        e.uuid = "Y"  # type: ignore[misc]
        raised = False
    except dataclasses.FrozenInstanceError:
        raised = True
    assert raised
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run --package fulcra-dayone pytest packages/dayone/tests/test_entry.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'fulcra_dayone.entry'`.

- [ ] **Step 3: Create `packages/dayone/fulcra_dayone/entry.py`**

```python
"""The reader-agnostic Day One entry model."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class DayOneEntry:
    """One Day One journal entry, normalized across the readers.

    `creation_date` is timezone-aware (UTC). `tags` is a tuple so the
    record stays hashable/frozen. `location` is a composed place string
    or None. `photo_count` and `word_count` are lightweight metadata.
    """
    uuid: str
    creation_date: datetime
    text: str
    tags: tuple[str, ...]
    starred: bool
    journal: str
    location: str | None
    photo_count: int
    word_count: int
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run --package fulcra-dayone pytest packages/dayone/tests/test_entry.py -v`
Expected: PASS — 2 tests.

- [ ] **Step 5: Commit**

```bash
git add packages/dayone/fulcra_dayone/entry.py packages/dayone/tests/test_entry.py
git commit -m "feat(dayone): DayOneEntry model"
```

---

### Task 4: Convert a DayOneEntry to a GenericEvent

**Files:**
- Create: `packages/dayone/fulcra_dayone/convert.py`
- Test: `packages/dayone/tests/test_convert.py`

- [ ] **Step 1: Write the failing test**

Create `packages/dayone/tests/test_convert.py`:

```python
"""DayOneEntry -> fulcra_csv GenericEvent conversion."""
from __future__ import annotations

from datetime import datetime, timezone

from fulcra_csv.events import INSTANT

from fulcra_dayone.convert import to_event
from fulcra_dayone.entry import DayOneEntry


def _entry(**kw) -> DayOneEntry:
    base = dict(
        uuid="ABC123",
        creation_date=datetime(2026, 5, 21, 9, 0, tzinfo=timezone.utc),
        text="My title line\n\nbody text here",
        tags=("alpha", "beta"),
        starred=False,
        journal="Personal",
        location="Seattle",
        photo_count=1,
        word_count=6,
    )
    base.update(kw)
    return DayOneEntry(**base)


def test_event_is_instant_at_creation_date():
    ev = to_event(_entry())
    assert ev.annotation_type == INSTANT
    assert ev.end_time is None
    assert ev.start_time == datetime(2026, 5, 21, 9, 0, tzinfo=timezone.utc)


def test_title_is_first_non_empty_line_without_markdown_hashes():
    ev = to_event(_entry(text="## My title line\n\nbody"))
    assert ev.title == "My title line"


def test_title_caps_at_120_chars():
    ev = to_event(_entry(text="x" * 200))
    assert ev.title is not None and len(ev.title) == 120


def test_note_replaces_dayone_media_placeholders():
    ev = to_event(_entry(text="before ![](dayone-moment://ABC) after"))
    assert ev.note == "before [photo] after"


def test_tags_become_extra_tags():
    ev = to_event(_entry(tags=("alpha", "beta")))
    assert ev.tag is None
    assert ev.extra_tags == ("alpha", "beta")


def test_source_id_is_stable_and_uuid_derived():
    a = to_event(_entry()).source_id
    b = to_event(_entry()).source_id
    assert a == b
    assert a.startswith("com.fulcra.dayone.")


def test_external_ids_carry_metadata():
    ev = to_event(_entry())
    assert ev.external_ids["dayone_uuid"] == "ABC123"
    assert ev.external_ids["journal"] == "Personal"
    assert ev.external_ids["word_count"] == 6
    assert ev.external_ids["photo_count"] == 1
    assert ev.external_ids["location"] == "Seattle"


def test_location_omitted_from_external_ids_when_absent():
    ev = to_event(_entry(location=None))
    assert "location" not in ev.external_ids
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run --package fulcra-dayone pytest packages/dayone/tests/test_convert.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'fulcra_dayone.convert'`.

- [ ] **Step 3: Create `packages/dayone/fulcra_dayone/convert.py`**

```python
"""Convert a DayOneEntry into a fulcra_csv GenericEvent."""
from __future__ import annotations

import re

from fulcra_csv.events import INSTANT, GenericEvent, derive_source_id

from .entry import DayOneEntry

SOURCE_PREFIX = "com.fulcra.dayone"

# Day One embeds photos/videos as Markdown image links pointing at a
# dayone-moment:// (or dayone2://) URI. Replace each with a [photo] marker.
_MOMENT_RE = re.compile(r"!\[[^\]]*\]\(dayone[^)]*\)")


def _clean_text(text: str) -> str:
    return _MOMENT_RE.sub("[photo]", text)


def _title_from(text: str) -> str | None:
    for line in text.splitlines():
        stripped = line.lstrip("#").strip()
        if stripped:
            return stripped[:120]
    return None


def to_event(entry: DayOneEntry) -> GenericEvent:
    """Map a DayOneEntry to an instant GenericEvent ready for run_import."""
    external_ids: dict = {
        "dayone_uuid": entry.uuid,
        "journal": entry.journal,
        "starred": entry.starred,
        "word_count": entry.word_count,
        "photo_count": entry.photo_count,
    }
    if entry.location:
        external_ids["location"] = entry.location
    return GenericEvent(
        start_time=entry.creation_date,
        note=_clean_text(entry.text),
        title=_title_from(entry.text),
        source_id=derive_source_id(SOURCE_PREFIX, entry.uuid),
        end_time=None,
        extra_tags=tuple(entry.tags),
        annotation_type=INSTANT,
        external_ids=external_ids,
    )
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run --package fulcra-dayone pytest packages/dayone/tests/test_convert.py -v`
Expected: PASS — 8 tests.

- [ ] **Step 5: Commit**

```bash
git add packages/dayone/fulcra_dayone/convert.py packages/dayone/tests/test_convert.py
git commit -m "feat(dayone): convert DayOneEntry to a GenericEvent"
```

---

### Task 5: The selection filter

**Files:**
- Create: `packages/dayone/fulcra_dayone/filter.py`
- Test: `packages/dayone/tests/test_filter.py`

- [ ] **Step 1: Write the failing test**

Create `packages/dayone/tests/test_filter.py`:

```python
"""Entry selection filters."""
from __future__ import annotations

from datetime import datetime, timezone

from fulcra_dayone.entry import DayOneEntry
from fulcra_dayone.filter import select


def _entry(uuid, *, tags=(), journal="Personal", starred=False, day=15) -> DayOneEntry:
    return DayOneEntry(
        uuid=uuid,
        creation_date=datetime(2026, 5, day, tzinfo=timezone.utc),
        text="t", tags=tuple(tags), starred=starred, journal=journal,
        location=None, photo_count=0, word_count=1,
    )


ENTRIES = [
    _entry("a", tags=("work",), journal="Personal", starred=True, day=10),
    _entry("b", tags=("travel",), journal="Travel", starred=False, day=20),
    _entry("c", tags=("work", "travel"), journal="Travel", starred=True, day=15),
]


def test_no_filters_returns_everything():
    assert {e.uuid for e in select(ENTRIES)} == {"a", "b", "c"}


def test_tag_filter_matches_any_given_tag():
    got = select(ENTRIES, tags=frozenset({"work"}))
    assert {e.uuid for e in got} == {"a", "c"}


def test_journal_filter():
    got = select(ENTRIES, journals=frozenset({"Travel"}))
    assert {e.uuid for e in got} == {"b", "c"}


def test_starred_filter():
    got = select(ENTRIES, starred_only=True)
    assert {e.uuid for e in got} == {"a", "c"}


def test_date_range_is_inclusive():
    got = select(
        ENTRIES,
        since=datetime(2026, 5, 15, tzinfo=timezone.utc),
        until=datetime(2026, 5, 20, 23, 59, 59, tzinfo=timezone.utc),
    )
    assert {e.uuid for e in got} == {"b", "c"}


def test_filters_are_anded_together():
    got = select(
        ENTRIES,
        tags=frozenset({"travel"}),
        journals=frozenset({"Travel"}),
        starred_only=True,
    )
    assert {e.uuid for e in got} == {"c"}
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run --package fulcra-dayone pytest packages/dayone/tests/test_filter.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'fulcra_dayone.filter'`.

- [ ] **Step 3: Create `packages/dayone/fulcra_dayone/filter.py`**

```python
"""Select which Day One entries to import."""
from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from .entry import DayOneEntry


def select(
    entries: Iterable[DayOneEntry],
    *,
    tags: frozenset[str] = frozenset(),
    journals: frozenset[str] = frozenset(),
    since: datetime | None = None,
    until: datetime | None = None,
    starred_only: bool = False,
) -> list[DayOneEntry]:
    """Return the entries matching ALL active filters. A filter is
    inactive (matches everything) when its set is empty / its value is
    None / the flag is False."""
    out: list[DayOneEntry] = []
    for e in entries:
        if tags and not (set(e.tags) & tags):
            continue
        if journals and e.journal not in journals:
            continue
        if since is not None and e.creation_date < since:
            continue
        if until is not None and e.creation_date > until:
            continue
        if starred_only and not e.starred:
            continue
        out.append(e)
    return out
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run --package fulcra-dayone pytest packages/dayone/tests/test_filter.py -v`
Expected: PASS — 6 tests.

- [ ] **Step 5: Commit**

```bash
git add packages/dayone/fulcra_dayone/filter.py packages/dayone/tests/test_filter.py
git commit -m "feat(dayone): entry selection filter"
```

---

### Task 6: The JSON-export reader

**Files:**
- Create: `packages/dayone/fulcra_dayone/readers/__init__.py` (empty package marker for now)
- Create: `packages/dayone/fulcra_dayone/readers/json_export.py`
- Test: `packages/dayone/tests/test_json_export.py`

- [ ] **Step 1: Create the empty readers package marker**

Create `packages/dayone/fulcra_dayone/readers/__init__.py` with a single line:

```python
"""Day One readers."""
```

(The `read` dispatch is added to this file in Task 8.)

- [ ] **Step 2: Write the failing test**

Create `packages/dayone/tests/test_json_export.py`:

```python
"""JSON-export reader: .zip and folder."""
from __future__ import annotations

import json
import zipfile
from datetime import timezone
from pathlib import Path

import pytest

from fulcra_dayone.readers.json_export import read_json_export

SAMPLE = {
    "metadata": {"version": "1.0"},
    "entries": [
        {
            "uuid": "AAA111",
            "creationDate": "2024-01-15T09:30:00Z",
            "text": "First entry body",
            "tags": ["work", "travel"],
            "starred": True,
            "location": {"placeName": "Cafe", "country": "USA"},
            "photos": [{"identifier": "p1"}],
        },
        {
            "uuid": "BBB222",
            "creationDate": "2024-02-20T14:00:00Z",
            "text": "Second entry, no tags",
        },
    ],
}


def _write_export_folder(folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "Personal.json").write_text(json.dumps(SAMPLE), encoding="utf-8")
    return folder


def test_reads_a_folder_export(tmp_path: Path):
    folder = _write_export_folder(tmp_path / "export")
    entries = read_json_export(folder)
    assert {e.uuid for e in entries} == {"AAA111", "BBB222"}
    first = next(e for e in entries if e.uuid == "AAA111")
    assert first.journal == "Personal"
    assert first.tags == ("work", "travel")
    assert first.starred is True
    assert first.location == "Cafe"
    assert first.photo_count == 1
    assert first.creation_date.tzinfo == timezone.utc
    assert first.creation_date.hour == 9


def test_entry_without_optional_fields_is_tolerated():
    pass  # covered by BBB222 below


def test_second_entry_has_empty_optionals(tmp_path: Path):
    folder = _write_export_folder(tmp_path / "export")
    entries = read_json_export(folder)
    second = next(e for e in entries if e.uuid == "BBB222")
    assert second.tags == ()
    assert second.starred is False
    assert second.location is None
    assert second.photo_count == 0


def test_reads_a_zip_export(tmp_path: Path):
    folder = _write_export_folder(tmp_path / "export")
    zip_path = tmp_path / "export.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.write(folder / "Personal.json", "Personal.json")
    entries = read_json_export(zip_path)
    assert {e.uuid for e in entries} == {"AAA111", "BBB222"}


def test_journal_name_comes_from_the_json_filename(tmp_path: Path):
    folder = tmp_path / "export"
    folder.mkdir()
    (folder / "Travel Journal.json").write_text(json.dumps(SAMPLE), encoding="utf-8")
    entries = read_json_export(folder)
    assert all(e.journal == "Travel Journal" for e in entries)


def test_rejects_a_path_that_is_neither_zip_nor_folder(tmp_path: Path):
    bogus = tmp_path / "notes.txt"
    bogus.write_text("hi")
    with pytest.raises(ValueError, match="not a Day One JSON export"):
        read_json_export(bogus)
```

- [ ] **Step 3: Run the test to verify it fails**

Run: `uv run --package fulcra-dayone pytest packages/dayone/tests/test_json_export.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'fulcra_dayone.readers.json_export'`.

- [ ] **Step 4: Create `packages/dayone/fulcra_dayone/readers/json_export.py`**

```python
"""Read a Day One JSON export (.zip or unzipped folder) into DayOneEntry[]."""
from __future__ import annotations

import json
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from ..entry import DayOneEntry


def _parse_date(raw: str) -> datetime:
    # Day One JSON dates look like "2024-01-15T09:30:00Z".
    dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    return dt.astimezone(timezone.utc)


def _compose_location(loc: dict) -> str | None:
    for key in ("placeName", "localityName", "administrativeArea", "country"):
        val = loc.get(key)
        if val:
            return str(val)
    return None


def _entries_from_json(path: Path) -> tuple[list[DayOneEntry], int]:
    journal = path.stem
    doc = json.loads(path.read_text(encoding="utf-8"))
    out: list[DayOneEntry] = []
    skipped = 0
    for raw in doc.get("entries", []):
        uuid = raw.get("uuid")
        created = raw.get("creationDate")
        if not uuid or not created:
            skipped += 1
            continue
        text = raw.get("text", "") or ""
        loc = raw.get("location") or {}
        out.append(DayOneEntry(
            uuid=uuid,
            creation_date=_parse_date(created),
            text=text,
            tags=tuple(raw.get("tags", []) or []),
            starred=bool(raw.get("starred", False)),
            journal=journal,
            location=_compose_location(loc) if loc else None,
            photo_count=len(raw.get("photos", []) or []),
            word_count=len(text.split()),
        ))
    return out, skipped


def _read_folder(folder: Path) -> list[DayOneEntry]:
    json_files = sorted(folder.rglob("*.json"))
    if not json_files:
        raise ValueError(f"no .json files found in Day One export: {folder}")
    out: list[DayOneEntry] = []
    skipped = 0
    for jf in json_files:
        entries, n = _entries_from_json(jf)
        out.extend(entries)
        skipped += n
    if skipped:
        print(f"json_export: skipped {skipped} entries missing uuid/creationDate",
              file=sys.stderr)
    return out


def read_json_export(source: Path) -> list[DayOneEntry]:
    """Read a Day One JSON export. `source` is a .zip or an unzipped folder."""
    if source.is_file() and source.suffix.lower() == ".zip":
        with tempfile.TemporaryDirectory() as tmp:
            with zipfile.ZipFile(source) as zf:
                zf.extractall(tmp)
            return _read_folder(Path(tmp))
    if source.is_dir():
        return _read_folder(source)
    raise ValueError(f"not a Day One JSON export (.zip or folder): {source}")
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `uv run --package fulcra-dayone pytest packages/dayone/tests/test_json_export.py -v`
Expected: PASS — 6 tests (the placeholder `test_entry_without_optional_fields_is_tolerated` passes trivially).

- [ ] **Step 6: Commit**

```bash
git add packages/dayone/fulcra_dayone/readers/__init__.py packages/dayone/fulcra_dayone/readers/json_export.py packages/dayone/tests/test_json_export.py
git commit -m "feat(dayone): JSON-export reader (zip + folder)"
```

---

### Task 7: The local-database reader

**Files:**
- Create: `packages/dayone/fulcra_dayone/readers/local_db.py`
- Test: `packages/dayone/tests/test_local_db.py`

Schema reference (verified against a real Day One install — see the spec's
"local_db reader" section): Core Data SQLite, `Z`-prefixed tables, dates
are float seconds since 2001-01-01 UTC. `ZENTRY(Z_PK, ZUUID, ZCREATIONDATE,
ZMARKDOWNTEXT, ZSTARRED, ZJOURNAL, ZLOCATION)`, `ZJOURNAL(Z_PK, ZNAME)`,
`ZTAG(Z_PK, ZNAME)`, `ZLOCATION(Z_PK, ZPLACENAME, ZLOCALITYNAME,
ZADMINISTRATIVEAREA, ZCOUNTRY)`, `ZATTACHMENT(ZENTRY, ZTYPE)`. The
entry↔tag join is `Z_<EntryEnt>TAGS`; entity numbers come from
`Z_PRIMARYKEY(Z_ENT, Z_NAME)`.

- [ ] **Step 1: Add a Core Data fixture builder to `conftest.py`**

Append this to `packages/dayone/tests/conftest.py`:

```python
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

# Core Data epoch — seconds since 2001-01-01 UTC.
_CD_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)


def _cd_seconds(dt: datetime) -> float:
    return (dt - _CD_EPOCH).total_seconds()


def build_dayone_db(path: Path) -> None:
    """Build a minimal Day One Core Data SQLite database for tests.

    Entity numbers: Entry=17, Tag=66 (mirroring a real Day One store);
    the entry↔tag join is therefore Z_17TAGS.
    """
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE Z_PRIMARYKEY (Z_ENT INTEGER, Z_NAME TEXT, Z_MAX INTEGER);
        CREATE TABLE ZJOURNAL (Z_PK INTEGER PRIMARY KEY, ZNAME TEXT);
        CREATE TABLE ZTAG (Z_PK INTEGER PRIMARY KEY, ZNAME TEXT);
        CREATE TABLE ZLOCATION (
            Z_PK INTEGER PRIMARY KEY, ZPLACENAME TEXT, ZLOCALITYNAME TEXT,
            ZADMINISTRATIVEAREA TEXT, ZCOUNTRY TEXT);
        CREATE TABLE ZENTRY (
            Z_PK INTEGER PRIMARY KEY, ZUUID TEXT, ZCREATIONDATE REAL,
            ZMARKDOWNTEXT TEXT, ZSTARRED INTEGER, ZJOURNAL INTEGER,
            ZLOCATION INTEGER);
        CREATE TABLE ZATTACHMENT (ZENTRY INTEGER, ZTYPE INTEGER);
        CREATE TABLE Z_17TAGS (Z_17ENTRIES INTEGER, Z_66TAGS1 INTEGER);
        """
    )
    conn.executemany(
        "INSERT INTO Z_PRIMARYKEY (Z_ENT, Z_NAME, Z_MAX) VALUES (?, ?, ?)",
        [(17, "Entry", 2), (66, "Tag", 2), (27, "Journal", 1)],
    )
    conn.executemany(
        "INSERT INTO ZJOURNAL (Z_PK, ZNAME) VALUES (?, ?)",
        [(1, "Personal"), (2, "Travel")],
    )
    conn.executemany(
        "INSERT INTO ZTAG (Z_PK, ZNAME) VALUES (?, ?)",
        [(1, "work"), (2, "travel")],
    )
    conn.execute(
        "INSERT INTO ZLOCATION (Z_PK, ZPLACENAME, ZLOCALITYNAME, "
        "ZADMINISTRATIVEAREA, ZCOUNTRY) VALUES (1, 'Cafe', 'Seattle', 'WA', 'USA')"
    )
    d1 = _cd_seconds(datetime(2024, 1, 15, 9, 30, tzinfo=timezone.utc))
    d2 = _cd_seconds(datetime(2024, 2, 20, 14, 0, tzinfo=timezone.utc))
    d3 = _cd_seconds(datetime(2024, 3, 1, 8, 0, tzinfo=timezone.utc))
    conn.executemany(
        "INSERT INTO ZENTRY (Z_PK, ZUUID, ZCREATIONDATE, ZMARKDOWNTEXT, "
        "ZSTARRED, ZJOURNAL, ZLOCATION) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (1, "AAA111", d1, "First entry body", 1, 1, 1),
            (2, "BBB222", d2, "Second entry", 0, 2, None),
            (3, "CCC333", d3, None, 0, 1, None),  # empty text -> skipped
        ],
    )
    conn.executemany(
        "INSERT INTO ZATTACHMENT (ZENTRY, ZTYPE) VALUES (?, ?)",
        [(1, 1), (1, 1)],  # entry 1 has 2 attachments
    )
    conn.executemany(
        "INSERT INTO Z_17TAGS (Z_17ENTRIES, Z_66TAGS1) VALUES (?, ?)",
        [(1, 1), (1, 2), (2, 2)],  # entry1: work+travel, entry2: travel
    )
    conn.commit()
    conn.close()
```

- [ ] **Step 2: Write the failing test**

Create `packages/dayone/tests/test_local_db.py`:

```python
"""Local Day One SQLite reader."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from fulcra_dayone.readers.local_db import read_local_db
from tests.conftest import build_dayone_db


def test_reads_entries_from_a_core_data_db(tmp_path: Path):
    db = tmp_path / "DayOne.sqlite"
    build_dayone_db(db)
    entries = read_local_db(db)
    # Entry CCC333 has empty text and is skipped.
    assert {e.uuid for e in entries} == {"AAA111", "BBB222"}


def test_maps_journal_tags_location_and_photo_count(tmp_path: Path):
    db = tmp_path / "DayOne.sqlite"
    build_dayone_db(db)
    entries = read_local_db(db)
    first = next(e for e in entries if e.uuid == "AAA111")
    assert first.journal == "Personal"
    assert first.tags == ("travel", "work")  # sorted
    assert first.location == "Cafe"
    assert first.photo_count == 2
    assert first.starred is True
    assert first.creation_date.year == 2024 and first.creation_date.hour == 9


def test_entry_with_no_location_or_tags(tmp_path: Path):
    db = tmp_path / "DayOne.sqlite"
    build_dayone_db(db)
    entries = read_local_db(db)
    second = next(e for e in entries if e.uuid == "BBB222")
    assert second.location is None
    assert second.tags == ("travel",)
    assert second.photo_count == 0


def test_missing_z_primarykey_raises_a_schema_error(tmp_path: Path):
    db = tmp_path / "DayOne.sqlite"
    build_dayone_db(db)
    conn = sqlite3.connect(db)
    conn.execute("DROP TABLE Z_PRIMARYKEY")
    conn.commit()
    conn.close()
    with pytest.raises(ValueError, match="schema not recognized"):
        read_local_db(db)


def test_missing_database_file_raises(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        read_local_db(tmp_path / "nope.sqlite")
```

- [ ] **Step 3: Run the test to verify it fails**

Run: `uv run --package fulcra-dayone pytest packages/dayone/tests/test_local_db.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'fulcra_dayone.readers.local_db'`.

- [ ] **Step 4: Create `packages/dayone/fulcra_dayone/readers/local_db.py`**

```python
"""Read Day One's local Core Data SQLite database into DayOneEntry[]."""
from __future__ import annotations

import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..entry import DayOneEntry

# Core Data stores timestamps as float seconds since 2001-01-01 UTC.
_CORE_DATA_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)
_DB_GLOB = "Library/Group Containers/*.dayoneapp2/Data/Documents/DayOne.sqlite"
_SCHEMA_ERROR = "Day One database schema not recognized — use the JSON export instead"


def find_database() -> Path:
    """Locate the Day One SQLite database under the user's home directory."""
    matches = sorted(Path.home().glob(_DB_GLOB))
    if not matches:
        raise FileNotFoundError(
            f"no Day One database found at ~/{_DB_GLOB}; "
            "pass --db-path or use the JSON export"
        )
    return matches[0]


def _snapshot(db: Path) -> Path:
    """Copy the DB to a temp file (APFS clone when possible) so the live
    database is never opened directly."""
    dest = Path(tempfile.mkdtemp()) / "dayone-snapshot.sqlite"
    try:
        subprocess.run(
            ["cp", "-c", str(db), str(dest)], check=True, capture_output=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        shutil.copy2(db, dest)
    return dest


def _entity_number(conn: sqlite3.Connection, name: str) -> int:
    row = conn.execute(
        "SELECT Z_ENT FROM Z_PRIMARYKEY WHERE Z_NAME = ?", (name,),
    ).fetchone()
    if row is None:
        raise ValueError(f"{_SCHEMA_ERROR} (no '{name}' entity)")
    return int(row[0])


def _find_tag_join(conn: sqlite3.Connection, entry_ent: int) -> tuple[str, str, str]:
    """Return (join_table, entry_column, tag_column) for the entry↔tag
    many-to-many relation, discovered from the schema."""
    entry_col = f"Z_{entry_ent}ENTRIES"
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name LIKE 'Z\\_%TAGS' ESCAPE '\\'"
    ).fetchall()
    for (table,) in rows:
        cols = [c[1] for c in conn.execute(f"PRAGMA table_info('{table}')")]
        if entry_col in cols and len(cols) == 2:
            tag_col = cols[0] if cols[1] == entry_col else cols[1]
            return table, entry_col, tag_col
    raise ValueError(f"{_SCHEMA_ERROR} (no entry/tag join table)")


def read_local_db(db_path: Path | None = None) -> list[DayOneEntry]:
    """Read entries from the local Day One database. With no `db_path`,
    locate it automatically."""
    src = db_path or find_database()
    if not src.exists():
        raise FileNotFoundError(f"Day One database not found: {src}")
    snapshot = _snapshot(src)
    conn = sqlite3.connect(snapshot)
    conn.row_factory = sqlite3.Row
    try:
        return _read(conn)
    except sqlite3.OperationalError as exc:
        raise ValueError(f"{_SCHEMA_ERROR} ({exc})") from exc
    finally:
        conn.close()


def _read(conn: sqlite3.Connection) -> list[DayOneEntry]:
    entry_ent = _entity_number(conn, "Entry")
    join_table, entry_col, tag_col = _find_tag_join(conn, entry_ent)

    journals = {
        r["Z_PK"]: r["ZNAME"]
        for r in conn.execute("SELECT Z_PK, ZNAME FROM ZJOURNAL")
    }
    tag_names = {
        r["Z_PK"]: r["ZNAME"]
        for r in conn.execute("SELECT Z_PK, ZNAME FROM ZTAG")
    }
    locations = {
        r["Z_PK"]: (
            r["ZPLACENAME"] or r["ZLOCALITYNAME"]
            or r["ZADMINISTRATIVEAREA"] or r["ZCOUNTRY"]
        )
        for r in conn.execute(
            "SELECT Z_PK, ZPLACENAME, ZLOCALITYNAME, ZADMINISTRATIVEAREA, "
            "ZCOUNTRY FROM ZLOCATION"
        )
    }
    tags_by_entry: dict[int, list[str]] = {}
    for r in conn.execute(
        f"SELECT {entry_col} AS e, {tag_col} AS t FROM {join_table}"
    ):
        name = tag_names.get(r["t"])
        if name:
            tags_by_entry.setdefault(r["e"], []).append(name)
    photos_by_entry = {
        r["ZENTRY"]: r["n"]
        for r in conn.execute(
            "SELECT ZENTRY, COUNT(*) AS n FROM ZATTACHMENT "
            "WHERE ZENTRY IS NOT NULL GROUP BY ZENTRY"
        )
    }

    out: list[DayOneEntry] = []
    skipped = 0
    for r in conn.execute(
        "SELECT Z_PK, ZUUID, ZCREATIONDATE, ZMARKDOWNTEXT, ZSTARRED, "
        "ZJOURNAL, ZLOCATION FROM ZENTRY"
    ):
        text = r["ZMARKDOWNTEXT"]
        if not text or not r["ZUUID"] or r["ZCREATIONDATE"] is None:
            skipped += 1
            continue
        created = _CORE_DATA_EPOCH + timedelta(seconds=float(r["ZCREATIONDATE"]))
        out.append(DayOneEntry(
            uuid=r["ZUUID"],
            creation_date=created,
            text=text,
            tags=tuple(sorted(tags_by_entry.get(r["Z_PK"], []))),
            starred=bool(r["ZSTARRED"]),
            journal=journals.get(r["ZJOURNAL"], "(unknown)"),
            location=locations.get(r["ZLOCATION"]),
            photo_count=photos_by_entry.get(r["Z_PK"], 0),
            word_count=len(text.split()),
        ))
    if skipped:
        print(f"local_db: skipped {skipped} entries with no readable text",
              file=sys.stderr)
    return out
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `uv run --package fulcra-dayone pytest packages/dayone/tests/test_local_db.py -v`
Expected: PASS — 5 tests.

- [ ] **Step 6: Commit**

```bash
git add packages/dayone/fulcra_dayone/readers/local_db.py packages/dayone/tests/test_local_db.py packages/dayone/tests/conftest.py
git commit -m "feat(dayone): local Core Data SQLite reader"
```

---

### Task 8: The reader dispatch

**Files:**
- Modify: `packages/dayone/fulcra_dayone/readers/__init__.py`
- Test: `packages/dayone/tests/test_readers_dispatch.py`

- [ ] **Step 1: Write the failing test**

Create `packages/dayone/tests/test_readers_dispatch.py`:

```python
"""readers.read dispatch."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from fulcra_dayone.readers import read
from tests.conftest import build_dayone_db

_SAMPLE = {"entries": [
    {"uuid": "AAA111", "creationDate": "2024-01-15T09:30:00Z", "text": "hi"},
]}


def test_read_uses_json_export_for_a_folder(tmp_path: Path):
    folder = tmp_path / "export"
    folder.mkdir()
    (folder / "Personal.json").write_text(json.dumps(_SAMPLE), encoding="utf-8")
    entries = read(folder, local_db=False, db_path=None)
    assert {e.uuid for e in entries} == {"AAA111"}


def test_read_uses_local_db_when_requested(tmp_path: Path):
    db = tmp_path / "DayOne.sqlite"
    build_dayone_db(db)
    entries = read(None, local_db=True, db_path=db)
    assert {e.uuid for e in entries} == {"AAA111", "BBB222"}


def test_read_without_source_or_local_db_raises():
    with pytest.raises(ValueError, match="export path"):
        read(None, local_db=False, db_path=None)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run --package fulcra-dayone pytest packages/dayone/tests/test_readers_dispatch.py -v`
Expected: FAIL — `ImportError: cannot import name 'read' from 'fulcra_dayone.readers'`.

- [ ] **Step 3: Replace `packages/dayone/fulcra_dayone/readers/__init__.py`**

```python
"""Day One readers — dispatch to the JSON export or the local database."""
from __future__ import annotations

from pathlib import Path

from ..entry import DayOneEntry
from .json_export import read_json_export
from .local_db import read_local_db


def read(
    source: Path | None, *, local_db: bool, db_path: Path | None,
) -> list[DayOneEntry]:
    """Read Day One entries. With `local_db` True, read the local
    database (`db_path` optional); otherwise read the JSON export at
    `source` (a .zip or a folder)."""
    if local_db:
        return read_local_db(db_path)
    if source is None:
        raise ValueError("provide an export path (.zip or folder), or use --local-db")
    return read_json_export(source)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run --package fulcra-dayone pytest packages/dayone/tests/test_readers_dispatch.py -v`
Expected: PASS — 3 tests.

- [ ] **Step 5: Commit**

```bash
git add packages/dayone/fulcra_dayone/readers/__init__.py packages/dayone/tests/test_readers_dispatch.py
git commit -m "feat(dayone): reader dispatch"
```

---

### Task 9: The Fulcra client (Journal definition bootstrap)

**Files:**
- Create: `packages/dayone/fulcra_dayone/client.py`
- Test: `packages/dayone/tests/test_client.py`

- [ ] **Step 1: Write the failing test**

Create `packages/dayone/tests/test_client.py`:

```python
"""DayOneFulcraClient — find-or-create the Journal definition."""
from __future__ import annotations

import json

import httpx
import pytest

from fulcra_dayone.client import DayOneFulcraClient


@pytest.fixture(autouse=True)
def _token(monkeypatch):
    monkeypatch.setenv("FULCRA_ACCESS_TOKEN", "test-token")


def test_ensure_journal_definition_adopts_existing(recording_transport):
    def responder(r: httpx.Request) -> httpx.Response:
        if r.method == "GET" and r.url.path == "/user/v1alpha1/annotation":
            return httpx.Response(200, json=[
                {"name": "Journal", "annotation_type": "instant",
                 "id": "def-journal", "created_at": "2026-01-01T00:00:00Z",
                 "deleted_at": None},
            ])
        raise AssertionError(f"unexpected {r.method} {r.url}")

    client = DayOneFulcraClient(transport=recording_transport(responder))
    assert client.ensure_journal_definition() == "def-journal"


def test_ensure_journal_definition_creates_when_absent(recording_transport):
    posted: list[dict] = []

    def responder(r: httpx.Request) -> httpx.Response:
        if r.method == "GET" and r.url.path == "/user/v1alpha1/annotation":
            return httpx.Response(200, json=[])
        if r.method == "POST" and r.url.path == "/user/v1alpha1/annotation":
            posted.append(json.loads(r.content))
            return httpx.Response(200, json={"id": "def-new"})
        raise AssertionError(f"unexpected {r.method} {r.url}")

    client = DayOneFulcraClient(transport=recording_transport(responder))
    assert client.ensure_journal_definition() == "def-new"
    assert posted[0]["name"] == "Journal"
    assert posted[0]["annotation_type"] == "instant"
    assert posted[0]["measurement_spec"]["measurement_type"] == "instant"


def test_ensure_journal_definition_picks_oldest_duplicate(recording_transport):
    def responder(r: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[
            {"name": "Journal", "annotation_type": "instant", "id": "def-new",
             "created_at": "2026-05-01T00:00:00Z", "deleted_at": None},
            {"name": "Journal", "annotation_type": "instant", "id": "def-old",
             "created_at": "2026-01-01T00:00:00Z", "deleted_at": None},
        ])

    client = DayOneFulcraClient(transport=recording_transport(responder))
    assert client.ensure_journal_definition() == "def-old"


def test_ensure_journal_definition_ignores_soft_deleted(recording_transport):
    posted: list[dict] = []

    def responder(r: httpx.Request) -> httpx.Response:
        if r.method == "GET":
            return httpx.Response(200, json=[
                {"name": "Journal", "annotation_type": "instant",
                 "id": "def-dead", "created_at": "2026-01-01T00:00:00Z",
                 "deleted_at": "2026-02-01T00:00:00Z"},
            ])
        posted.append(json.loads(r.content))
        return httpx.Response(200, json={"id": "def-fresh"})

    client = DayOneFulcraClient(transport=recording_transport(responder))
    assert client.ensure_journal_definition() == "def-fresh"
    assert len(posted) == 1
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run --package fulcra-dayone pytest packages/dayone/tests/test_client.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'fulcra_dayone.client'`.

- [ ] **Step 3: Create `packages/dayone/fulcra_dayone/client.py`**

```python
"""Fulcra client for fulcra-dayone — adds the Journal definition bootstrap.

Subclasses fulcra_csv.FulcraClient so it inherits run_import, ensure_tag,
the httpx client, and auth from fulcra-common.
"""
from __future__ import annotations

from fulcra_csv.fulcra import FulcraClient

JOURNAL_DEFINITION_NAME = "Journal"


class DayOneFulcraClient(FulcraClient):
    USER_AGENT = "fulcra-dayone/0.1"

    def ensure_journal_definition(self) -> str:
        """Return the id of the live "Journal" InstantAnnotation
        definition, creating it if none exists. If duplicates exist,
        returns the oldest by created_at — so every run converges on the
        same definition."""
        r = self._client().get(
            "/user/v1alpha1/annotation", headers=self._authed_headers(),
        )
        r.raise_for_status()
        matches = [
            d for d in r.json()
            if d.get("name") == JOURNAL_DEFINITION_NAME
            and d.get("annotation_type") == "instant"
            and not d.get("deleted_at")
        ]
        if matches:
            matches.sort(key=lambda d: d.get("created_at") or "")
            return matches[0]["id"]
        body = {
            "annotation_type": "instant",
            "name": JOURNAL_DEFINITION_NAME,
            "description": "Day One journal entries.",
            "tags": [],
            "measurement_spec": {
                "measurement_type": "instant",
                "value_type": "none",
                "unit": None,
            },
        }
        r = self._client().post(
            "/user/v1alpha1/annotation", json=body,
            headers=self._authed_headers(),
        )
        r.raise_for_status()
        return r.json()["id"]
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run --package fulcra-dayone pytest packages/dayone/tests/test_client.py -v`
Expected: PASS — 4 tests.

- [ ] **Step 5: Commit**

```bash
git add packages/dayone/fulcra_dayone/client.py packages/dayone/tests/test_client.py
git commit -m "feat(dayone): DayOneFulcraClient with find-or-create Journal definition"
```

---

### Task 10: The CLI

**Files:**
- Create: `packages/dayone/fulcra_dayone/cli.py`
- Test: `packages/dayone/tests/test_cli.py`

- [ ] **Step 1: Write the failing test**

Create `packages/dayone/tests/test_cli.py`:

```python
"""fulcra-dayone CLI."""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from click.testing import CliRunner

import fulcra_dayone.cli as cli_mod
from fulcra_dayone.client import DayOneFulcraClient
from fulcra_dayone.cli import cli

_SAMPLE = {"entries": [
    {"uuid": "AAA111", "creationDate": "2024-01-15T09:30:00Z",
     "text": "First", "tags": ["work"], "starred": True},
    {"uuid": "BBB222", "creationDate": "2024-02-20T14:00:00Z",
     "text": "Second", "starred": False},
]}


@pytest.fixture(autouse=True)
def _token(monkeypatch):
    monkeypatch.setenv("FULCRA_ACCESS_TOKEN", "test-token")


def _export(tmp_path: Path) -> Path:
    folder = tmp_path / "export"
    folder.mkdir()
    (folder / "Personal.json").write_text(json.dumps(_SAMPLE), encoding="utf-8")
    return folder


def test_no_filters_without_all_is_an_error(tmp_path: Path):
    res = CliRunner().invoke(cli, ["import", str(_export(tmp_path))])
    assert res.exit_code != 0
    assert "--all" in res.output


def test_dry_run_reports_counts_without_network(tmp_path: Path):
    res = CliRunner().invoke(cli, ["import", str(_export(tmp_path)), "--all", "--dry-run"])
    assert res.exit_code == 0, res.output
    assert "Would import 2 entries" in res.output


def test_dry_run_with_starred_filter(tmp_path: Path):
    res = CliRunner().invoke(
        cli, ["import", str(_export(tmp_path)), "--starred", "--dry-run"],
    )
    assert res.exit_code == 0, res.output
    assert "Would import 1 entries" in res.output


def test_import_posts_to_fulcra(tmp_path: Path, monkeypatch):
    def responder(r: httpx.Request) -> httpx.Response:
        path = r.url.path
        if r.method == "GET" and path == "/user/v1alpha1/annotation":
            return httpx.Response(200, json=[])
        if r.method == "POST" and path == "/user/v1alpha1/annotation":
            return httpx.Response(200, json={"id": "def-journal"})
        if r.method == "GET" and path.startswith("/user/v1alpha1/tag/name/"):
            return httpx.Response(200, json={"id": "tag-x"})
        if r.method == "GET" and path.startswith("/data/v1alpha1/event/"):
            return httpx.Response(200, json=[])
        if r.method == "POST" and path == "/ingest/v1/record/batch":
            return httpx.Response(200, json={"ok": True})
        raise AssertionError(f"unexpected {r.method} {r.url}")

    transport = httpx.MockTransport(responder)
    monkeypatch.setattr(
        cli_mod, "DayOneFulcraClient",
        lambda **kw: DayOneFulcraClient(transport=transport, **kw),
    )
    res = CliRunner().invoke(cli, ["import", str(_export(tmp_path)), "--all"])
    assert res.exit_code == 0, res.output
    assert "Imported" in res.output
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run --package fulcra-dayone pytest packages/dayone/tests/test_cli.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'fulcra_dayone.cli'`.

- [ ] **Step 3: Create `packages/dayone/fulcra_dayone/cli.py`**

```python
"""fulcra-dayone command-line interface."""
from __future__ import annotations

from datetime import datetime, time, timezone
from pathlib import Path

import click

from .client import DayOneFulcraClient
from .convert import to_event
from .filter import select
from .readers import read


def _parse_date(value: str, *, end_of_day: bool) -> datetime:
    """Parse an ISO date (YYYY-MM-DD) to a UTC datetime — start or end of day."""
    d = datetime.fromisoformat(value).date()
    t = time(23, 59, 59, 999999) if end_of_day else time(0, 0, 0)
    return datetime.combine(d, t, tzinfo=timezone.utc)


@click.group()
def cli() -> None:
    """Import Day One journal entries into Fulcra."""


@cli.command(name="import")
@click.argument("source", required=False, type=click.Path(path_type=Path))
@click.option("--local-db", is_flag=True,
              help="Read Day One's local database instead of an export.")
@click.option("--db-path", type=click.Path(path_type=Path), default=None,
              help="Override the local database path.")
@click.option("--tag", "tags", multiple=True,
              help="Only entries carrying this tag (repeatable).")
@click.option("--journal", "journals", multiple=True,
              help="Only entries in this journal (repeatable).")
@click.option("--since", default=None, help="Only entries on/after this ISO date.")
@click.option("--until", default=None, help="Only entries on/before this ISO date.")
@click.option("--starred", is_flag=True, help="Only starred entries.")
@click.option("--all", "import_all", is_flag=True,
              help="Required to import with no filters.")
@click.option("--dry-run", is_flag=True,
              help="Show what would be imported; don't contact Fulcra.")
def import_cmd(
    source: Path | None, local_db: bool, db_path: Path | None,
    tags: tuple[str, ...], journals: tuple[str, ...],
    since: str | None, until: str | None, starred: bool,
    import_all: bool, dry_run: bool,
) -> None:
    """Import Day One entries from SOURCE (a .zip or folder), or --local-db."""
    any_filter = bool(tags or journals or since or until or starred)
    if not any_filter and not import_all:
        raise click.UsageError(
            "No filters given. Pass --all to import every entry, or use "
            "--tag / --journal / --since / --until / --starred."
        )
    if local_db and source is not None:
        raise click.UsageError("Pass either a SOURCE path or --local-db, not both.")
    if not local_db and source is None:
        raise click.UsageError("Provide a SOURCE path (.zip or folder), or use --local-db.")

    entries = read(source, local_db=local_db, db_path=db_path)
    selected = select(
        entries,
        tags=frozenset(tags),
        journals=frozenset(journals),
        since=_parse_date(since, end_of_day=False) if since else None,
        until=_parse_date(until, end_of_day=True) if until else None,
        starred_only=starred,
    )
    if not selected:
        click.echo("No entries matched the filters.")
        return

    if dry_run:
        journals_seen = sorted({e.journal for e in selected})
        dates = sorted(e.creation_date for e in selected)
        click.echo(f"Would import {len(selected)} entries.")
        click.echo(f"  journals: {', '.join(journals_seen)}")
        click.echo(f"  date range: {dates[0].date()} .. {dates[-1].date()}")
        return

    events = [to_event(e) for e in selected]
    client = DayOneFulcraClient()
    definition_id = client.ensure_journal_definition()
    tag_names = sorted({t for e in selected for t in e.tags})
    tag_id_for = {name: client.ensure_tag(name) for name in tag_names}
    result = client.run_import(
        events, definition_id=definition_id, tag_id_for=tag_id_for,
    )
    click.echo(
        f"Imported {result.posted} entries "
        f"({result.skipped_existing} already present, "
        f"{result.verified} verified)."
    )
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run --package fulcra-dayone pytest packages/dayone/tests/test_cli.py -v`
Expected: PASS — 4 tests.

- [ ] **Step 5: Run the full fulcra-dayone suite**

Run: `uv run --package fulcra-dayone pytest packages/dayone -q`
Expected: PASS — all tests (entry 2, convert 8, filter 6, json_export 6, local_db 5, dispatch 3, client 4, cli 4 = 38).

- [ ] **Step 6: Commit**

```bash
git add packages/dayone/fulcra_dayone/cli.py packages/dayone/tests/test_cli.py
git commit -m "feat(dayone): import CLI"
```

---

### Task 11: README and final verification

**Files:**
- Create: `packages/dayone/README.md`

- [ ] **Step 1: Create `packages/dayone/README.md`**

```markdown
# fulcra-dayone

Import selected [Day One](https://dayoneapp.com) journal entries into your
Fulcra account as annotations. Each imported entry becomes an
InstantAnnotation under a "Journal" definition, carrying the entry text,
its Day One tags, and lightweight metadata (journal, location, word and
photo counts).

## Input modes

Day One has no read API and its CLI is write-only, so entries come from
either a JSON export or the app's local database:

- **JSON export** — in Day One, File → Export → JSON. Pass the resulting
  `.zip`, or an unzipped folder.
- **Local database** — `--local-db` reads Day One's local SQLite store
  directly (no manual export). Unofficial: it can break on a Day One
  update, and it skips entries with no readable text.

## Usage

```bash
# JSON export, filtered
fulcra-dayone import ~/Downloads/Export.zip --journal Personal --tag fulcra
fulcra-dayone import ~/Downloads/export-folder --since 2024-01-01 --starred

# Local database
fulcra-dayone import --local-db --tag fulcra

# Preview without posting
fulcra-dayone import ~/Downloads/Export.zip --all --dry-run
```

Filters (`--tag`, `--journal`, `--since`, `--until`, `--starred`) combine
with AND. With no filter, `--all` is required — a guard against an
accidental full import. Re-running is safe: entries dedup on a stable
`source_id` derived from the Day One entry uuid.

## Develop

```bash
uv sync --all-extras
uv run --package fulcra-dayone pytest packages/dayone
```
```

- [ ] **Step 2: Run the whole workspace test suite**

Run: `uv run --package fulcra-common pytest packages/fulcra-common -q && uv run --package fulcra-attention pytest packages/attention -q && uv run --package fulcra-media-helpers pytest packages/media-helpers -q && uv run --package fulcra-csv-importer pytest packages/csv-importer -q && uv run --package fulcra-dayone pytest packages/dayone -q`
Expected: every package passes — fulcra-common 13, attention 144, media-helpers 412 (+1 skipped), csv-importer 73, fulcra-dayone 38.

- [ ] **Step 3: Smoke-check the local_db reader against the real database (optional, this machine only)**

Run: `uv run --package fulcra-dayone python -c "from fulcra_dayone.readers.local_db import read_local_db; e = read_local_db(); print(len(e), 'entries'); print(e[0].journal, e[0].tags)"`
Expected: prints a non-zero entry count and a sample journal/tags — confirms the verified schema matches the live Day One database. If it raises a schema error, capture the message and reconcile `local_db.py` against the real schema before proceeding.

- [ ] **Step 4: Commit**

```bash
git add packages/dayone/README.md
git commit -m "docs(dayone): package README"
```

---

## Self-Review

**1. Spec coverage:**
- Three input modes (zip, folder, local SQLite) — Tasks 6, 7, 8. ✓
- Four AND-combined filters — Task 5. ✓
- Full-entry InstantAnnotation, note/title/tags/external_ids — Task 4. ✓
- Find-or-create "Journal" InstantAnnotation definition — Task 9. ✓
- Multi-tag via `GenericEvent.extra_tags` — Task 1. ✓
- Append-only dedup via uuid-derived `source_id` — Task 4 (`derive_source_id`), relies on csv-importer's `run_import` readback. ✓
- Reuse `fulcra_csv.run_import` — Task 10. ✓
- `--all` guard, `--dry-run` — Task 10. ✓
- local_db: APFS-clone snapshot, `Z_PRIMARYKEY` entity resolution, schema-drift error, empty-text skip — Task 7. ✓
- Error handling: malformed JSON entries skipped + counted (Task 6), schema drift (Task 7). ✓
- Testing: mock transport, JSON + SQLite fixtures built in-test — Tasks 6, 7, 9, 10. ✓
- csv-importer `extra_tags` gets its own tests — Task 1. ✓

**2. Placeholder scan:** `test_entry_without_optional_fields_is_tolerated` in Task 6 is an intentional no-op kept only so the test list reads completely; the real coverage is `test_second_entry_has_empty_optionals`. No "TBD"/"implement later"/vague steps elsewhere; every code step shows complete code.

**3. Type consistency:** `DayOneEntry` field names (`uuid`, `creation_date`, `text`, `tags`, `starred`, `journal`, `location`, `photo_count`, `word_count`) are identical across `entry.py`, both readers, `convert.py`, and the filter. `GenericEvent` usage matches its real signature (`start_time`, `note`, `title`, `source_id`, `end_time`, `tag`, `extra_tags`, `value`, `annotation_type`, `data_fields`, `external_ids`). `read(source, *, local_db, db_path)`, `read_json_export(source)`, `read_local_db(db_path=None)`, `select(entries, *, tags, journals, since, until, starred_only)`, `to_event(entry)`, `ensure_journal_definition()` signatures are consistent between their definitions and call sites.
