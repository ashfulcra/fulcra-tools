# Netflix Rich Parser + Slim Honesty Fix — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox syntax.

**Goal:** Stop the slim importer from fabricating durations, and add the rich (GDPR) variant parser so users with the real export get real watch times. Import command auto-detects which variant by CSV header.

**Architecture:** `fulcra_media/importers/netflix.py` gets a new `parse_rich()` function alongside `parse_slim()`, plus a `parse_auto()` dispatcher that branches on header. `parse_slim` is changed to use a zero-duration noon-UTC point per event (no fake 30/45/100min blocks).

**Tech stack:** Same as previous plan — Python 3.11+, Click, httpx, pytest.

**Spec reference:** `docs/superpowers/specs/2026-05-16-fulcra-media-helpers-design.md` § 3.2a (rich) and § 3.2b (slim).

---

## Task 1: Slim honesty fix — zero-duration noon UTC

**Files:**
- Modify: `fulcra_media/importers/netflix.py`
- Modify: `tests/test_netflix_importer.py`

The 21:00 UTC + 30/45/100min synthetic durations look like real watch times in any visualization. Replace with a single noon-UTC point (`start_time = end_time = date 12:00 UTC`) so the events render as date-only ticks. Keep `timestamp_confidence: low`, drop `duration_estimated: true` (no duration is being estimated anymore). Add `point_in_time: true` external_id.

- [ ] **Step 1: Update the failing tests**

Modify `tests/test_netflix_importer.py` — find the existing `test_parse_slim_first_event_is_movie` and replace its time assertions:

```python
def test_parse_slim_first_event_is_movie():
    events = list(parse_slim(FIXTURE))
    e = events[0]
    assert isinstance(e, NormalizedEvent)
    assert e.importer == "netflix-slim"
    assert e.service == "netflix"
    assert e.category == "watched"
    assert e.note == "Movie One"
    assert e.title == "Movie One"
    # Honest point-in-time at noon UTC — no fake duration
    assert e.start_time == datetime(2026, 5, 12, 12, 0, tzinfo=timezone.utc)
    assert e.end_time == e.start_time
    assert e.timestamp_confidence == "low"
    assert e.external_ids["time_estimated"] is True
    assert e.external_ids["point_in_time"] is True
    assert "duration_estimated" not in e.external_ids
```

Also DELETE `test_parse_slim_episode_yields_30min_duration` (no longer applicable — all events are points).

Adjust `test_estimate_duration_*` tests: since `estimate_duration` is being removed, delete those three tests entirely.

- [ ] **Step 2: Run to verify failures**

Run `.venv/bin/pytest -v tests/test_netflix_importer.py`. Some existing tests will fail with the new expected values.

- [ ] **Step 3: Update `parse_slim`**

In `fulcra_media/importers/netflix.py`:

1. Delete the `estimate_duration` function entirely.
2. Change the `parse_slim` body:

```python
def parse_slim(csv_path: Path) -> Iterator[NormalizedEvent]:
    """Parse a Netflix slim CSV (Title, Date) into NormalizedEvents.

    The slim variant has no time or duration data. We emit one point-in-time
    event per row at 12:00 UTC on the date — start_time == end_time. The
    timestamp_confidence is 'low' and external_ids carries both
    `time_estimated: true` and `point_in_time: true`. Idempotency key
    incorporates an occurrence index so same-day rewatches produce distinct
    events.
    """
    occurrence_counter: Counter[tuple[str, str]] = Counter()

    with csv_path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames != ["Title", "Date"]:
            raise ValueError(
                f"unexpected Netflix CSV header {reader.fieldnames!r}; "
                "parse_slim handles the 2-column variant only — use parse_rich for the GDPR export"
            )
        for row in reader:
            raw_title = row["Title"]
            date_str = row["Date"]
            d = parse_netflix_date(date_str)
            key = (date_str, raw_title)
            idx = occurrence_counter[key]
            occurrence_counter[key] += 1

            note, title = make_note_and_title(raw_title)
            instant = datetime.combine(d, time(12, 0, 0), tzinfo=timezone.utc)

            yield NormalizedEvent(
                importer="netflix-slim",
                service="netflix",
                category="watched",
                note=note,
                title=title,
                start_time=instant,
                end_time=instant,
                deterministic_id=_det_id(date_str, raw_title, idx),
                timestamp_confidence="low",
                external_ids={
                    "time_estimated": True,
                    "point_in_time": True,
                    "occurrence_index": idx,
                    "raw_date": date_str,
                },
            )
```

Imports stay the same (still need `csv`, `hashlib`, `Counter`, `Iterator`, `datetime`, `time`, `timezone`, `Path`, `NormalizedEvent`). Drop the `timedelta` import if it's not used elsewhere.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest -v tests/test_netflix_importer.py`
Expected: all green (some tests deleted, others updated).

- [ ] **Step 5: Full suite check**

Run: `.venv/bin/pytest -q`
Expected: 56 → ~52 (some tests removed; no failures).

- [ ] **Step 6: Update the e2e test**

`tests/test_e2e_netflix.py` checks `md["recorded_at"]` has start_time and end_time. That still holds (just equal). It does not assert on specific times. Verify it still passes — if it does, no change needed. If it asserts duration > 0, fix.

- [ ] **Step 7: Update the wizard text**

In `fulcra_media/wizards/netflix.py`, update `SLIM_STEPS` — replace the line about "synthetic 21:00 UTC start time and a duration estimated by title shape" with:

```
  Note: The slim CSV is date-only (M/D/YY format) with no time, duration,
  device, or profile fields. Each row becomes one Watched annotation as a
  point-in-time event at 12:00 UTC on the date. timestamp_confidence: low,
  point_in_time: true. Real watch times require the GDPR export below.
```

Update `tests/test_netflix_wizard.py` — the `test_walkthrough_slim_route` asserts `"M/D/YY" in result.output` — that still holds. No test change needed.

- [ ] **Step 8: Commit**

```bash
git add fulcra_media/importers/netflix.py tests/test_netflix_importer.py fulcra_media/wizards/netflix.py
git commit -m "$(cat <<'EOF'
fix(netflix-slim): emit point-in-time events at noon UTC instead of fake durations

The slim CSV has no time data. Previously we synthesized 21:00 UTC start
times with 30/45/100min duration buckets, which renders as misleading
fake watch blocks in visualizations. Real complaint from ash:
"the watches don't look like real watch times — all 30 or 45 minutes etc."

Now: each event is a point in time at 12:00 UTC on the date (start_time
== end_time), with external_ids.point_in_time = true. The
timestamp_confidence stays 'low'. Consumers can render these as date
ticks rather than synthetic blocks. Real watch times require the GDPR
export — see the rich parser landing next.

Existing slim records remain in Fulcra (no event delete endpoint).
Users can soft-delete the Watched definition for a clean reset, or live
with the noise — the rich variant will overlay accurate data.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Rich variant fixture

**Files:**
- Create: `tests/fixtures/netflix_rich_small.csv`

A 6-row synthetic example of the 10-column GDPR `ViewingActivity.csv` to test `parse_rich` against. Includes a movie, a TV episode, a trailer (filtered out), and a malformed device row.

- [ ] **Step 1: Write the fixture**

Create `tests/fixtures/netflix_rich_small.csv`:

```csv
Profile Name,Start Time,Duration,Attributes,Title,Supplemental Video Type,Device Type,Bookmark,Latest Bookmark,Country
Ash,2026-05-12 20:32:15,01:42:30,,Dune: Part Two,,Apple TV 4K (3rd generation),01:42:30,01:42:30,US (United States)
Ash,2026-05-10 21:00:00,00:48:12,,Severance: Season 2: The We We Are,,Apple TV 4K (3rd generation),00:48:12,00:48:12,US (United States)
Ash,2026-05-10 22:00:00,00:25:00,,Severance: Season 2: Goodbye Mrs. Selvig,,iPhone 15 Pro,00:25:00,00:48:00,US (United States)
Ash,2026-05-09 19:45:00,00:01:30,,Stranger Things: Season 4: Trailer,TRAILER,Apple TV 4K (3rd generation),00:01:30,00:01:30,US (United States)
Ash,2026-05-08 18:00:00,02:38:45,,Killers of the Flower Moon,,Apple TV 4K (3rd generation),02:38:45,02:38:45,US (United States)
Ash,2026-04-21 06:15:00,00:18:20,,Big Mistakes: Limited Series: Episode 1,,Web Browser (Chrome),00:18:20,00:18:20,US (United States)
```

Note: this is synthetic test data, not real personal viewing.

- [ ] **Step 2: Verify the file**

`wc -l tests/fixtures/netflix_rich_small.csv` → 7 (header + 6 rows).

- [ ] **Step 3: Commit**

```bash
git add tests/fixtures/netflix_rich_small.csv
git commit -m "$(cat <<'EOF'
test(netflix): synthetic 10-column GDPR fixture for rich parser tests

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Rich parser implementation

**Files:**
- Modify: `fulcra_media/importers/netflix.py`
- Modify: `tests/test_netflix_importer.py`

Parses the 10-column GDPR variant. Real UTC start times, real durations (H:MM:SS), filters `Supplemental Video Type != ""`, separate idempotency scheme using `(profile, start_time, title)` — start_time alone is granular enough that occurrence_index isn't needed.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_netflix_importer.py`:

```python
from fulcra_media.importers.netflix import parse_rich

RICH_FIXTURE = Path(__file__).parent / "fixtures" / "netflix_rich_small.csv"


def test_parse_rich_filters_trailers():
    """Rows with Supplemental Video Type set (TRAILER, HOOK, etc.) are dropped."""
    events = list(parse_rich(RICH_FIXTURE))
    # 6 rows in, 1 trailer filtered, 5 events out
    assert len(events) == 5
    assert all("Trailer" not in e.title for e in events)


def test_parse_rich_movie_first_event():
    events = list(parse_rich(RICH_FIXTURE))
    # Sorted by start_time ascending? No — preserve CSV order; first row is Dune
    e = next(e for e in events if e.title == "Dune: Part Two")
    assert e.importer == "netflix-rich"
    assert e.service == "netflix"
    assert e.category == "watched"
    assert e.note == "Dune: Part Two"
    assert e.title == "Dune: Part Two"
    # UTC, real time, real duration
    assert e.start_time == datetime(2026, 5, 12, 20, 32, 15, tzinfo=timezone.utc)
    assert e.end_time == datetime(2026, 5, 12, 22, 14, 45, tzinfo=timezone.utc)
    assert e.timestamp_confidence == "high"
    assert e.external_ids["profile"] == "Ash"
    assert "Apple TV" in e.external_ids["device_type"]
    assert e.external_ids["country"].startswith("US")
    # No estimates flags
    assert "time_estimated" not in e.external_ids
    assert "duration_estimated" not in e.external_ids
    assert "point_in_time" not in e.external_ids


def test_parse_rich_episode_note_format():
    events = list(parse_rich(RICH_FIXTURE))
    e = next(e for e in events if "Severance" in e.title and "We We Are" in e.note)
    # Same colon-split note format as slim
    assert "Severance" in e.note
    assert "Season 2" in e.note


def test_parse_rich_idempotency_key_per_session():
    """Each row produces a unique deterministic_id based on profile+start_time+title."""
    events = list(parse_rich(RICH_FIXTURE))
    ids = [e.deterministic_id for e in events]
    assert len(ids) == len(set(ids))
    assert all(i.startswith("com.fulcra.media.netflix-rich.") for i in ids)


def test_parse_rich_duration_parsed_to_seconds():
    events = list(parse_rich(RICH_FIXTURE))
    e = next(e for e in events if e.title == "Killers of the Flower Moon")
    # 2:38:45 = 2*3600 + 38*60 + 45 = 9525 seconds
    assert (e.end_time - e.start_time).total_seconds() == 9525


def test_parse_rich_rejects_slim_header(tmp_path):
    csv = tmp_path / "slim.csv"
    csv.write_text('Title,Date\n"Movie","5/12/26"\n')
    import pytest
    with pytest.raises(ValueError, match="parse_rich handles the 10-column"):
        list(parse_rich(csv))
```

- [ ] **Step 2: Run to verify failures**

Run: `.venv/bin/pytest -v tests/test_netflix_importer.py`
Expected: 6 new test failures on `ImportError` for `parse_rich`.

- [ ] **Step 3: Implement `parse_rich`**

Append to `fulcra_media/importers/netflix.py`:

```python
_RICH_EXPECTED_COLS = [
    "Profile Name", "Start Time", "Duration", "Attributes", "Title",
    "Supplemental Video Type", "Device Type", "Bookmark", "Latest Bookmark", "Country",
]


def _det_id_rich(profile: str, start_time_str: str, raw_title: str) -> str:
    h = hashlib.sha256(f"{profile}|{start_time_str}|{raw_title}".encode()).hexdigest()
    return f"com.fulcra.media.netflix-rich.{h[:16]}"


def _parse_hmmss(value: str) -> "timedelta":
    """Parse H:MM:SS into a timedelta."""
    from datetime import timedelta as _td
    parts = value.split(":")
    if len(parts) != 3:
        raise ValueError(f"not a H:MM:SS duration: {value!r}")
    h, m, s = (int(p) for p in parts)
    return _td(hours=h, minutes=m, seconds=s)


def parse_rich(csv_path: Path) -> Iterator[NormalizedEvent]:
    """Parse a Netflix rich (GDPR) CSV into NormalizedEvents.

    The rich variant has 10 columns including UTC Start Time, Duration in
    H:MM:SS, Profile Name, Device Type, and Country. Rows with non-empty
    Supplemental Video Type (TRAILER, HOOK, PROMOTIONAL, etc.) are dropped.
    """
    with csv_path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames != _RICH_EXPECTED_COLS:
            raise ValueError(
                f"unexpected Netflix CSV header {reader.fieldnames!r}; "
                f"parse_rich handles the 10-column GDPR variant only — use parse_slim "
                f"for the in-app 2-column download"
            )
        for row in reader:
            if (row.get("Supplemental Video Type") or "").strip():
                continue
            raw_title = row["Title"]
            start_str = row["Start Time"]
            # Netflix's GDPR export uses "YYYY-MM-DD HH:MM:SS" in UTC
            start = datetime.strptime(start_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            duration = _parse_hmmss(row["Duration"])
            end = start + duration

            note, title = make_note_and_title(raw_title)
            profile = (row.get("Profile Name") or "").strip()

            yield NormalizedEvent(
                importer="netflix-rich",
                service="netflix",
                category="watched",
                note=note,
                title=title,
                start_time=start,
                end_time=end,
                deterministic_id=_det_id_rich(profile, start_str, raw_title),
                timestamp_confidence="high",
                external_ids={
                    "profile": profile,
                    "device_type": (row.get("Device Type") or "").strip(),
                    "country": (row.get("Country") or "").strip(),
                    "bookmark": (row.get("Bookmark") or "").strip(),
                },
            )
```

Note: `make_note_and_title` and `_det_id` are already in the file from prior tasks. `_det_id_rich` is new and uses a separate prefix (`netflix-rich.` vs `netflix.`) so rich and slim events don't collide.

- [ ] **Step 4: Run to verify they pass**

Run: `.venv/bin/pytest -v tests/test_netflix_importer.py`
Expected: all green.

- [ ] **Step 5: Full suite check**

Run: `.venv/bin/pytest -q`
Expected: all pass, ~58 tests (post-slim-cleanup baseline + 6 new rich tests).

- [ ] **Step 6: Commit**

```bash
git add fulcra_media/importers/netflix.py tests/test_netflix_importer.py
git commit -m "$(cat <<'EOF'
feat(netflix): parse_rich for the 10-column GDPR ViewingActivity.csv

Real UTC start times, real H:MM:SS durations, profile/device/country
captured. Trailers (Supplemental Video Type set) filtered out.
timestamp_confidence: high, no synthetic estimates.

Distinct importer name (netflix-rich) and source-id prefix
(com.fulcra.media.netflix-rich.) so rich and slim events don't collide
in the dedup readback.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Auto-detect dispatcher in import CLI

**Files:**
- Modify: `fulcra_media/importers/netflix.py`
- Modify: `fulcra_media/cli.py`
- Modify: `tests/test_netflix_importer.py`
- Modify: `tests/test_cli.py`

A `parse_auto(csv_path)` function inspects the CSV header and dispatches to `parse_slim` or `parse_rich`. The CLI's `import_netflix` calls `parse_auto` instead of `parse_slim`.

- [ ] **Step 1: Add the failing test in the importer module**

Append to `tests/test_netflix_importer.py`:

```python
from fulcra_media.importers.netflix import parse_auto


def test_parse_auto_routes_slim():
    events = list(parse_auto(FIXTURE))
    # Same as parse_slim — emits netflix-slim importer name
    assert all(e.importer == "netflix-slim" for e in events)
    assert len(events) == 8


def test_parse_auto_routes_rich():
    events = list(parse_auto(RICH_FIXTURE))
    assert all(e.importer == "netflix-rich" for e in events)
    assert len(events) == 5


def test_parse_auto_rejects_unknown_header(tmp_path):
    csv = tmp_path / "weird.csv"
    csv.write_text("Foo,Bar\n1,2\n")
    import pytest
    with pytest.raises(ValueError, match="unrecognized Netflix CSV"):
        list(parse_auto(csv))
```

- [ ] **Step 2: Run to verify failures**

Run: `.venv/bin/pytest -v tests/test_netflix_importer.py`
Expected: ImportError for `parse_auto`.

- [ ] **Step 3: Implement `parse_auto`**

Append to `fulcra_media/importers/netflix.py`:

```python
def parse_auto(csv_path: Path) -> Iterator[NormalizedEvent]:
    """Inspect CSV header and dispatch to parse_slim or parse_rich."""
    with csv_path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        header = reader.fieldnames
    if header == ["Title", "Date"]:
        yield from parse_slim(csv_path)
    elif header == _RICH_EXPECTED_COLS:
        yield from parse_rich(csv_path)
    else:
        raise ValueError(
            f"unrecognized Netflix CSV header {header!r}; "
            "expected slim ['Title', 'Date'] or rich 10-column GDPR variant"
        )
```

- [ ] **Step 4: Run to verify tests pass**

Run: `.venv/bin/pytest -v tests/test_netflix_importer.py`
Expected: 3 new pass.

- [ ] **Step 5: Switch CLI to use `parse_auto`**

In `fulcra_media/cli.py`, change the import-netflix command:

```python
events = list(netflix_importer.parse_slim(Path(resolved)))
```

to

```python
events = list(netflix_importer.parse_auto(Path(resolved)))
```

(Single-line change.)

- [ ] **Step 6: Add a CLI test for the rich path**

Append to `tests/test_cli.py`:

```python
def test_import_netflix_rich_variant(tmp_path: Path, mocker):
    """The CLI auto-detects the 10-column rich variant and routes correctly."""
    csv = tmp_path / "rich.csv"
    csv.write_text(
        'Profile Name,Start Time,Duration,Attributes,Title,Supplemental Video Type,Device Type,Bookmark,Latest Bookmark,Country\n'
        '"Ash","2026-05-12 20:00:00","00:30:00","","Some: Show: S1","","Apple TV","00:30:00","00:30:00","US"\n'
    )
    state_path = tmp_path / "state.json"
    save(State(
        watched_definition_id="w", listened_definition_id="l",
        tag_ids={"netflix": "t"},
    ), state_path)
    mocker.patch("fulcra_media.cli.STATE_PATH", state_path)

    captured = {}
    from fulcra_media.fulcra import ImportResult
    def fake_run(self, events, state, chunk_size=500, window_pad_minutes=10):
        events = list(events)
        captured["count"] = len(events)
        captured["importer"] = events[0].importer if events else None
        return ImportResult(total=len(events), skipped_existing=0, posted=len(events), verified=len(events))
    mocker.patch("fulcra_media.fulcra.FulcraClient.run_import", fake_run)

    result = CliRunner().invoke(cli, ["import", "netflix", str(csv)])
    assert result.exit_code == 0, result.output
    assert captured["count"] == 1
    assert captured["importer"] == "netflix-rich"
```

- [ ] **Step 7: Full suite check**

Run: `.venv/bin/pytest -q`
Expected: all pass.

- [ ] **Step 8: Commit**

```bash
git add fulcra_media/importers/netflix.py fulcra_media/cli.py tests/test_netflix_importer.py tests/test_cli.py
git commit -m "$(cat <<'EOF'
feat(netflix): parse_auto dispatches to slim or rich by CSV header

`fulcra-media import netflix <path>` now accepts either variant
transparently. Header ["Title","Date"] -> parse_slim. 10-column GDPR
header -> parse_rich. Other -> ValueError with explanation.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: Update wizard to mention rich is now wired

**Files:**
- Modify: `fulcra_media/wizards/netflix.py`
- Modify: `tests/test_netflix_wizard.py`

The wizard's GDPR section currently says "Importing the rich variant is not yet wired up." Remove that disclaimer.

- [ ] **Step 1: Update tests**

In `tests/test_netflix_wizard.py`, add an assertion to `test_walkthrough_gdpr_route`:

```python
def test_walkthrough_gdpr_route():
    runner = CliRunner()
    result = runner.invoke(walkthrough, input="2\n")
    assert result.exit_code == 0
    assert "netflix.com/account/getmyinfo" in result.output
    assert "up to 30 days" in result.output
    assert "10 columns" in result.output or "rich" in result.output.lower()
    # New: confirm rich import is wired (no more "not yet wired up" disclaimer)
    assert "not yet wired" not in result.output
    assert "fulcra-media import netflix" in result.output
```

- [ ] **Step 2: Update GDPR_STEPS in `fulcra_media/wizards/netflix.py`**

Replace the trailing block:

```
  Importing the rich variant is not yet wired up (the slim importer is in
  place). For now, upload the ZIP to your Fulcra Library and we'll wire the
  rich importer in the next milestone.
```

with:

```
  When you have the zip, extract it and import:
    fulcra-media import netflix CONTENT_INTERACTION/ViewingActivity.csv

  The importer auto-detects the 10-column variant. Each row becomes one
  Watched annotation with the real UTC start time and real duration —
  timestamp_confidence: high, no estimates. Trailers and previews are
  filtered out automatically.
```

- [ ] **Step 3: Run tests**

Run: `.venv/bin/pytest -v tests/test_netflix_wizard.py`
Expected: all pass.

- [ ] **Step 4: Commit**

```bash
git add fulcra_media/wizards/netflix.py tests/test_netflix_wizard.py
git commit -m "$(cat <<'EOF'
docs(wizard): drop the 'rich variant not yet wired' disclaimer

Now that parse_auto routes to parse_rich on the 10-column header, the
wizard points users at the same `fulcra-media import netflix` command
for either variant.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Done criteria

- [ ] `pytest -q` passes
- [ ] `fulcra-media import netflix <slim.csv>` produces point-in-time events at noon UTC, `timestamp_confidence: low`
- [ ] `fulcra-media import netflix <rich.csv>` produces interval events with real UTC times and durations, `timestamp_confidence: high`
- [ ] `fulcra-media wizard netflix` (route 2) tells users to run `import netflix` on the rich CSV directly
