# Refactor 3: Unified ingest pipeline

**Task:** #69
**Priority:** medium (high pain, scoped blast radius)
**Why now:** `wire.build_record` is called from 4+ disparate locations. The `duration_seconds` defensive fix (#30) had to land in 3 sites. The cross-source fingerprint plumbing (#55) had to land in N importers. Every wire-format change pays this tax.

## Concrete pain

`wire.build_record` callsites:

- `packages/attention/fulcra_attention/ingest.py:194` — Attention extension events
- `packages/media-helpers/fulcra_media/fulcra.py:113` — Media importers (Trakt, Last.fm, Netflix, Spotify, takeouts, etc.) via `FulcraClient.ingest_batch`
- `packages/csv-importer/fulcra_csv/fulcra.py:71` — Generic CSV
- `packages/collect/fulcra_collect/daemon.py:522` — Daemon's quick-record (menubar tap)

Each has its own:
- `data_inner` dict construction
- `tags` resolution
- Source ID generation
- Error handling around the POST
- Cross-source fingerprint plumbing (some have it, some don't — youtube/netflix-slim deliberately skipped per #55)
- `duration_seconds` field (added to 3 sites for #30; csv-importer got conditional duration only when annotation_type == DURATION)

We have a `BaseFulcraClient.ingest_batch` AND we have manual `httpx.post` + `wire.build_record`. The split exists because BaseFulcraClient's contract is "give me NormalizedEvents"; the daemon's quick-record builds Moments from a definition_id, not from a NormalizedEvent.

## What an "ingest pipeline" buys us

A typed `IngestableEvent` union — `MomentEvent | DurationEvent | … future kinds` — that ALL ingest paths construct. One module owns:

- `build_record` callsite (one)
- `data_inner` shape (one — `note`, `title`, `service`, `duration_seconds` if duration, `external_ids`, etc.)
- Cross-source fingerprint computation (one decision point)
- Tag resolution
- POST with retry + structured error
- Activity-feed annotation emission

The four callsites become:

```python
# attention extension event:
event = DurationEvent(
    title=note, start=start_dt_sec, end=end_dt_sec,
    service="web", definition_id=state.attention_definition_id,
    external_ids={...},
    extra_source_ids=[],  # attention skipped from cross-source for now
    tags=[...],
)
pipeline.ingest_one(event)

# media importer:
events = [DurationEvent.from_normalized(ne) for ne in importer_output]
pipeline.ingest_batch(events)

# quick-record moment:
pipeline.ingest_one(MomentEvent(
    title=def_name, ts=now, comment=comment,
    definition_id=def_id,
))
```

## The contract

```python
@dataclass
class IngestableEvent:
    """Base class for all ingestable annotation events. Holds the
    fields every annotation needs: definition_id, source_id, tags,
    extra_source_ids (cross-source fingerprint), external_ids,
    timestamp_confidence, service tag, optional comment/title/note."""
    definition_id: str
    source_id: str               # per-source deterministic id
    extra_source_ids: tuple[str, ...] = ()
    tags: list[str] = ()
    external_ids: dict = field(default_factory=dict)
    note: str | None = None
    title: str | None = None
    service: str | None = None
    timestamp_confidence: str | None = None

@dataclass
class MomentEvent(IngestableEvent):
    ts: datetime

@dataclass
class DurationEvent(IngestableEvent):
    start: datetime
    end: datetime
    @property
    def duration_seconds(self) -> int: ...
```

Pipeline module `packages/fulcra-common/fulcra_common/ingest.py`:

```python
class IngestPipeline:
    def __init__(self, client: BaseFulcraClient): ...
    def build_record(self, event: IngestableEvent) -> dict: ...
    def ingest_one(self, event: IngestableEvent) -> None: ...
    def ingest_batch(self, events: Iterable[IngestableEvent]) -> IngestResult: ...
```

Behavior:
- `build_record` does the full data_inner construction, including `duration_seconds` for DurationEvent
- `extra_source_ids` flattened into `metadata.source` with dedup
- definition source tag (`com.fulcradynamics.annotation.{id}`) appended
- POST via the injected client; retries on 5xx/timeout (today's importers do this ad-hoc)
- Per-event activity-feed annotation emit if a callback is set

## What stays per-caller

The Attention extension event still needs Attention-specific data shape (host, chrome_identity, og_type). That goes in the `external_ids` dict — already where it lives today. Each importer builds an IngestableEvent with whatever external_ids it needs.

`NormalizedEvent` (the importer-side intermediate type) stays — it's a useful abstraction. We add a `to_duration_event(definition_id, tags)` method on it that produces the IngestableEvent.

## Migration plan

1. Build the IngestPipeline + IngestableEvent types in `fulcra-common`
2. Add a `to_ingest_event` factory on NormalizedEvent
3. Cut over `media-helpers/fulcra.py:ingest_batch` to use the pipeline. Smallest first because it's the most-tested.
4. Cut over `attention/ingest.py`. Validate the wire shape hasn't changed via test_wire.py + a synthetic round-trip.
5. Cut over `csv-importer/fulcra.py`.
6. Cut over `daemon.py:_record_annotation`.
7. Delete the orphaned per-callsite `data_inner` construction code.

Each step is independently shippable. The wire format doesn't change — payloads pre- and post-refactor are byte-identical. This is observable via test_wire and the existing importer regression tests.

## Test strategy

- New `test_ingest_pipeline.py` in fulcra-common covers IngestPipeline shape + builds for moment/duration
- Existing per-importer tests now exercise the pipeline via integration; assertions about the wire shape stay (`assert payload['metadata']['data_type'] == 'DurationAnnotation'`)
- Byte-for-byte regression test: build the same NormalizedEvent through old path + new path, diff the bytes. Catches accidental wire-format drift.

## Time estimate

- Phase 1 (types + pipeline + first cutover): 1 batch, ~3 hours
- Phase 2 (remaining 3 cutovers): 1 batch, ~2 hours
- Phase 3 (delete orphaned construction code): 30 min

Total: 1-2 sessions.

## Risks

1. **Wire format drift**: a careless refactor could change `metadata.source` ordering or `data` JSON key ordering, which Fulcra's source_id dedup would handle but we'd see weird events. Byte-for-byte regression test mandatory.
2. **Per-package definition resolution**: media-helpers does `_ensure_media_def` BEFORE building events; attention does its own `ensure_definitions`. The pipeline doesn't replace these — they're upstream of the pipeline. Keep them as-is.
3. **Cross-source fingerprint computation**: today each importer computes its own. The pipeline COULD centralize this (every DurationEvent with category=listened gets a listened_fingerprint), but importers like Apple Podcasts compute it on `end` time vs `start` time for cross-source alignment. Leave per-importer computation; pipeline accepts extra_source_ids precomputed.

## Recommendation

Land this AFTER refactor #1 (SQLite state). The pipeline refactor will accidentally touch state code (because the daemon's quick-record builds a moment + writes a state update); doing it after state is unified keeps the diff cleaner.
