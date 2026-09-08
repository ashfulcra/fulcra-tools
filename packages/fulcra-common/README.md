# fulcra-common

Shared Python plumbing for the importers and agent tools: Fulcra API access,
annotation definitions, record encoding, ingest, and media fingerprints.
This is a library, with no CLI or Collect daemon to run. Other packages use
it without needing the Collect app.

If you're adding a new importer or extending an existing one, this is
the package whose surface you build against — never re-invent any of
these primitives locally. The whole point of `fulcra-common` is that a
Fulcra wire-format change is a one-place change here.

## Package layout

| Module | What it owns |
|---|---|
| [client.py](fulcra_common/client.py) | `BaseFulcraClient`: auth, HTTP transport, record reads, tags, and definition CRUD; subclasses supply importer behavior. |
| [definitions.py](fulcra_common/definitions.py) | `resolve_definition_id`: adopt or create a definition, rejecting incompatible schemas with `DefinitionSchemaMismatch`. |
| [ingest.py](fulcra_common/ingest.py) | Event dataclasses and `IngestPipeline`, including wrapped and typed ingest paths. |
| [wire.py](fulcra_common/wire.py) | Wrapped envelopes, unwrapped typed records, source arrays, and definition payloads. |
| [cross_source_fingerprint.py](fulcra_common/cross_source_fingerprint.py) | Shared fingerprints for music, podcasts, movies, and TV episodes. |
| [schema_check.py](fulcra_common/schema_check.py) | Fetch the server's record schema and check payload fields against it. |
| [annotations.py](fulcra_common/annotations.py) | Agent lifecycle, needs-user, projection, and digest annotations. |

## Install

Python 3.11+. From the repository root:

```bash
uv sync --package fulcra-common
```

The runtime dependencies are `httpx` and `fulcra-api`; the latter supplies
library operations and the auth CLI. Pure record construction needs no account.
API calls require an authenticated Fulcra account (`uv run --package fulcra-common
fulcra auth login`).

## Ingest pipeline

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
hand it to the pipeline. Importers share the encoding logic; callers still own deduplication, retries,
and verification that accepted records became visible.

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

Except for the explicit attention opt-in, optional fields are emitted only
when populated.

### IngestPipeline interface

```python
from datetime import datetime, timezone
from fulcra_common.ingest import IngestPipeline, DurationEvent

event = DurationEvent(
    definition_id="11111111-2222-3333-4444-555555555555",
    source_id="example.duration.1",
    start=datetime(2026, 1, 1, 12, tzinfo=timezone.utc),
    end=datetime(2026, 1, 1, 13, tzinfo=timezone.utc),
    note="Synthetic example",
)
pipeline = IngestPipeline(client=None)

# Build a wire record without I/O. Useful for tests + the csv-importer
# which does post-build mutations.
record: dict = pipeline.build_record(event)

# Supply your BaseFulcraClient subclass to post.
# IngestPipeline(client=my_client).ingest_one(event)
# IngestPipeline(client=my_client).ingest_batch([event])
```

`build_record` is pure (`client=None` is fine). `ingest_one` posts to
`/ingest/v1/record`, falling back to the batch endpoint only on HTTP 404/405.
`ingest_batch` posts JSONL to `/ingest/v1/record/batch`. Both need a client;
other HTTP errors propagate to the caller.

### Typed records

`wire.build_typed_record(...)` builds the unwrapped schema used by
`IngestPipeline.ingest_typed(base_type, records)`. This is the path used by
[labs](../labs/README.md): `/ingest/v1/record/NumericAnnotation`, with one
record sent as JSON or several as JSONL. It is separate from the wrapped
`MomentEvent` / `DurationEvent` path above.

**An accepted typed upload is not a verified import.** The caller must check
existing source IDs before posting and verify visibility afterward. The code
accounts for asynchronous processing, missing server deduplication, dropped
invalid lines, and stripped unknown fields; `ingest_typed` itself only checks
the HTTP response. See [the labs storage flow](../labs/README.md#storage).

### Adding a new event kind

You shouldn't need a new IngestableEvent subclass for typical
importers — `DurationEvent` and `MomentEvent` cover the wrapped
annotation events modeled here; typed numeric records use the separate path above. If you genuinely need a new kind (e.g. a third
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

## Callers and boundaries

[Media helpers](../media-helpers/README.md) convert normalized events into
`DurationEvent`s. [CSV importer](../csv-importer/README.md) uses that path
for duration annotations, then merges CSV-specific fields; its instant and
built-in-type records use `wire.build_record` directly. Collect's quick-record
and tombstone helpers use `ingest_one`. Labs builds typed records.

The browser extensions encode their records in TypeScript, and the
[Netflix skill](../netflix-skill/README.md) deliberately vendors its importer.
A wire change therefore still needs a caller audit; this Python library does
not control every writer in the repository.

## Wrapped wire format invariants

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

These describe the wrapped event path. Typed records have a different schema;
see [`wire.build_typed_record`](fulcra_common/wire.py).

## Testing

```bash
# Per-package
uv run --package fulcra-common --extra dev pytest packages/fulcra-common/tests -q

# Full workspace
uv run --all-packages --extra dev pytest -q packages/
```

The tests cover wire records, pipeline behavior, client operations, definition
resolution, fingerprints, schema checks, and agent annotation routing with
stubbed transports. They do not establish the state of a live Fulcra deployment.
