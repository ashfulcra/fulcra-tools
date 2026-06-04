# fulcra-common

Shared core for every package in the fulcra-tools monorepo: the base
Fulcra REST API client, the wire-format module, the
annotation-definition resolver, and the unified ingest pipeline.

If you're adding a new importer or extending an existing one, this is
the package whose surface you build against — never re-invent any of
these primitives locally. The whole point of `fulcra-common` is that a
Fulcra wire-format change is a one-place change here.

## Package layout

```
fulcra_common/
├─ client.py        BaseFulcraClient — auth, httpx, tag lookup,
│                    soft-delete, event readback. Subclass it.
├─ definitions.py   resolve_definition_id — adopt-or-create flow
│                    for annotation definitions with schema mismatch
│                    detection (DefinitionSchemaMismatch).
├─ ingest.py        IngestableEvent + IngestPipeline — the SINGLE
│                    ingest path that owns wire.build_record,
│                    data-payload construction, and POST to
│                    /ingest/v1/record/batch. Refactor #69.
└─ wire.py          The wire format — record envelope, recorded_at
                     union, source array, JSONL batching, definition
                     payloads. Used by IngestPipeline; no production
                     callsite outside ingest.py uses it directly.
```

## Ingest pipeline (refactor #69)

### Why it exists

Before refactor #69 there were four+ ad-hoc callsites that each built
the Fulcra wire format inline — media-helpers, attention,
csv-importer, the collect daemon's quick-record, and the tombstone
path. Each one had to know about:

- The wire envelope (`specversion` / `data` / `metadata`)
- The `recorded_at` union (scalar string for moment, object with
  start/end for duration)
- The source-array assembly (per-event id + extras + annotation-def
  source)
- The `duration_seconds` defensive field — a renderer quirk (#30)
  that required surfacing the duration on the `data` payload, not
  just the recorded_at envelope. Three of the four sites duplicated
  this with subtle drift (one used `max(0, …)`, one didn't, one
  emitted float, one int).

The pipeline collapses all of that into one place. Importers now
build a typed `IngestableEvent` (declarative — just the fields) and
hand it to the pipeline. Future wire-format changes touch one file.

### The IngestableEvent contract

`IngestableEvent` is the base dataclass; `MomentEvent` adds `ts`,
`DurationEvent` adds `start` + `end`. Every event carries:

| Field | Purpose |
|---|---|
| `definition_id` | The annotation definition this event belongs to. `None` for the tombstone path (no def attached). |
| `source_id` | Deterministic per-event id. The importer computes this. |
| `extra_source_ids` | Cross-source fingerprints (e.g. `com.fulcra.content.watched.v1.<hash>`) appended to the source array for cross-importer dedup. |
| `tags` | Tag UUIDs to attach. Importers resolve names → UUIDs before constructing the event. |
| `external_ids` | Free-form per-importer enrichment map. Lands at `data.external_ids` on the wire. |
| `note` / `title` / `service` / `timestamp_confidence` / `comment` | Common optional top-level data fields. Only emitted when not None. |

`DurationEvent` adds `start` / `end` and exposes `.duration_seconds`
(clamped to zero on misordered range) which the pipeline injects into
the wire payload as the `duration_seconds` defensive field.

### Importer-specific top-level data fields

The pre-refactor wire shape carried a handful of importer-specific
top-level data keys. To preserve byte parity (refactor #69 decision —
no silent wire-shape changes), those fields are dedicated optional
properties on `IngestableEvent` rather than being routed through
`external_ids`:

- **Attention** (`category`, `url`, `og_description`, `favicon_url`,
  `parent_source_id`) — five top-level keys emitted unconditionally
  (with `None` values when not applicable). Importers opt in via
  `_emit_attention_fields=True`. Also forces `note` + `title` to
  emit even when None, matching the category-variant wire shape.
- **Quick-record** (`comment`) — distinct from `note`. Set by the
  collect daemon's `_record_annotation` and `_delete_annotation`.
- **Tombstone** (`superseded_by`, `supersedes_source_id`) — set by
  `_delete_annotation` only.

The pipeline emits each of these only when the importer populates
them — no leakage into wire payloads from other importers.

### IngestPipeline interface

```python
from fulcra_common.ingest import IngestPipeline, DurationEvent

pipeline = IngestPipeline(client=my_fulcra_client)

# Build a wire record without I/O. Useful for tests + the csv-importer
# which does post-build mutations.
record: dict = pipeline.build_record(event)

# POST a single event.
pipeline.ingest_one(event)

# POST a batch as JSONL to /ingest/v1/record/batch.
pipeline.ingest_batch(events)
```

`build_record` is pure (`client=None` is fine). `ingest_one` /
`ingest_batch` need a `BaseFulcraClient` for the auth + HTTP transport.

### Adding a new event kind

You shouldn't need a new IngestableEvent subclass for typical
importers — `DurationEvent` and `MomentEvent` cover every shape in
the codebase today. If you genuinely need a new kind (e.g. a third
recorded_at variant Fulcra adds in the future):

1. Add the subclass + `__post_init__` validation in `ingest.py`.
2. Add a branch in `IngestPipeline.build_record` that maps the new
   subclass to `wire.build_record`'s args.
3. Export the new symbol from `fulcra_common/__init__.py`.
4. Write a unit test in `tests/test_ingest.py` and a byte-parity test
   in `tests/test_ingest_byte_parity.py` (the byte-parity tests were
   deleted after refactor #69 landed; you'd re-introduce one if your
   change has a wire-shape concern).

### Adding an importer-specific top-level data field

If you're hitting a case where an importer wants to emit a top-level
data key the pipeline doesn't model:

- Strong preference: route the field via `external_ids` (a free-form
  map that lands at `data.external_ids.<key>`). That's the default
  choice for anything that's not load-bearing in the legacy timeline
  renderer.
- Only add a dedicated optional field on `IngestableEvent` if byte
  parity with an existing site forces it (the refactor #69 attention
  decision is the canonical example). Document why in the field's
  docstring.

## Cutover history (refactor #69)

The four primary callsites that previously held inline
`wire.build_record` + `httpx.post` blocks are now thin wrappers around
`IngestPipeline`:

- `packages/media-helpers/fulcra_media/fulcra.py:ingest_batch` →
  loops `NormalizedEvent.to_duration_event(...)` → pipeline.
- `attention/fulcra_attention/ingest.py:build_attention_event`
  returns a `DurationEvent`; the daemon's `/api/extension/attention`
  route posts via `IngestPipeline.ingest_one`.
- `packages/csv-importer/fulcra_csv/fulcra.py:_build_record` builds a
  `DurationEvent` and post-merges csv-specific top-level data keys
  (`value`, `tag` echo, `data_fields`) into the built record. Instant
  events stay on the legacy path — their `InstantAnnotation` data_type
  is semantically distinct from `MomentAnnotation`.
- `packages/collect/fulcra_collect/daemon.py:_record_annotation` and
  `_delete_annotation` both use a module-level `_QuickRecordClient`
  (BaseFulcraClient subclass with the legacy 10s timeout) +
  `IngestPipeline.ingest_one`.

## Wire format invariants the pipeline owns

- `specversion: 1`
- `data` is a sorted-key JSON string of the inner payload
- `metadata.data_type` is `"MomentAnnotation"` or `"DurationAnnotation"`
- `metadata.recorded_at` is a bare ISO string for moments, an object
  `{start_time, end_time}` for durations
- `metadata.source` is `[source_id, *extra_source_ids,
  com.fulcradynamics.annotation.<definition_id>]`, with empties and
  duplicates filtered
- `metadata.tags` is a flat list of tag UUIDs
- `duration_seconds` is injected into the `data` payload for every
  `DurationEvent` (the #30 defensive field)
- JSONL batches: one sorted-key JSON object per line, newline-joined,
  POSTed to `/ingest/v1/record/batch` with
  `Content-Type: application/x-jsonl`

A change to any of these is a change to `wire.py` + `ingest.py` only.

## Testing

```bash
# Per-package
uv run --directory packages/fulcra-common pytest -q

# Full workspace
uv run --all-packages pytest -q packages/
```

`tests/test_ingest.py` covers the dataclasses + pipeline unit-level.
The four importer cutover commits each carry their own assertions
against the post-cutover wire shape via `IngestPipeline.build_record`.
