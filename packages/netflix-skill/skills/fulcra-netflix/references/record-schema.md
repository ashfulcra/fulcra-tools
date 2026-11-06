# Native Netflix Event fields and identities

The actual importer on this development branch uses owned Event datatypes with
`api_version=v1`. The original wrapped DurationAnnotation writer is retired.
[Package guide](../../../README.md) owns installation, recovery, progress and
sharing boundaries; this reference owns field/identity details. Historical raw
reader shape and conversion remain unverified, with original annotations kept.

## Destination and authority

Use the shared watched `media_spec`: key `collect.media.watched.v1`, base Event,
initial name Watched, declared media fields. Optional `--data-type Event/UUID`
selects an owned compatible target; default resolution uses the complete shared
resolver/journal and exact-name uniqueness when unbound. Name alone, the old
annotation description marker, a cached UUID or a matching fingerprint is never
ownership/schema authority. Catalog metadata does not prove a custom creation key
survived; journal binding and fresh owned/schema proof have distinct roles.

The shared profile/account spec journal preserves a Watched binding through a
rename and constrains explicit replacements, including a binding made by media
imports. Native downstream consumers need actual target/schema/ownership evidence.
Legacy namespace `com.fulcradynamics.annotation.media.watched` identifies only the
old annotation contract; it is not native Event discovery or proof of native share
propagation. Incoming native sharing/pool discovery is UNKNOWN in this skill.

## Flat record

Every current record supplies canonical destination-qualified UUID `id`, one
primary source in `sources`, empty `tags`, explicit UTC `start_time`/`end_time`,
and declared enrichment:

| Field | Preserved content |
|---|---|
| note, title | Original parser output, without a media low-confidence note suffix. |
| duration_seconds | Original integer interval duration. Slim remains one synthetic second. |
| external_ids | Original variant metadata plus injected content_fingerprint and timestamp_confidence, retaining the existing merge precedence. |
| service | netflix |
| importer | netflix-slim or netflix-rich |
| category | watched |
| timestamp_confidence | low for slim, high for rich, in addition to its retained nested copy. |

Slim provenance includes original parsed date/occurrence and time-estimated/
point-in-time indicators. Rich keeps profile/device details. A fingerprint remains
nested provenance; it is not added as an alternate source or used to authorize a
skip. No definition-marker source, specversion/data/metadata wrapper or implicit
ingestion-time timestamp remains. Full finalized served-schema proof is mandatory;
local field compatibility alone does not prove a complete record is accepted.

## Example native record

Wholly synthetic input `Title,Date` with one row `Synthetic Movie,3/1/24`,
using illustrative target `Event/00000000-0000-4000-8000-000000000001`. The full example is
checked against the actual parser and native encoder by the guide contract test.

```json
{
  "id": "23729d1e-d51b-5199-b35c-a0098797a619",
  "sources": [
    "com.fulcra.media.netflix.v2.d2dcc271b1762c6c"
  ],
  "tags": [],
  "start_time": "2024-03-01T12:00:00Z",
  "end_time": "2024-03-01T12:00:01Z",
  "note": "Synthetic Movie",
  "title": "Synthetic Movie",
  "duration_seconds": 1,
  "external_ids": {
    "time_estimated": true,
    "point_in_time": true,
    "occurrence_index": 0,
    "raw_date": "3/1/24",
    "content_fingerprint": "movie:synthetic-movie",
    "timestamp_confidence": "low"
  },
  "service": "netflix",
  "importer": "netflix-slim",
  "category": "watched",
  "timestamp_confidence": "low"
}
```

## Source hashes and UUIDs

Keep existing parser normalization and exact helper argument bytes. Source schemes:

```text
slim: com.fulcra.media.netflix.v2. + sha256(date_str + '|' + raw_title + '|' + occurrence)[:16]
rich: com.fulcra.media.netflix-rich. + sha256(profile + '|' + start_time_str + '|' + raw_title)[:16]
```

Slim occurrence is the file-order index among identical date/title tuples. This
keeps repeats distinct within the file; it does not supply profile identity or
prove arbitrary partial-overlap rewatch correspondence. Rich includes profile and
real timestamp arguments. Existing parser trimming, title/fingerprint ambiguity,
UTC interpretation, supplemental filtering and source prefixes are unchanged.

The native primary UUID uses UUIDv5 with the standard URL namespace and name:

```text
fulcra-tools:v1:<full Event/UUID>:<original deterministic source ID>
```

`INGEST_VERSION=2` is the existing slim source-ID scheme, independent of native
transport `api_version=v1`. Do not bump prefixes to hide a conflict or silently
change identity during datatype migration. The source helper formulas agree with
the established media importer vectors; equal source identity does not imply equal
notes/tags/sources/enrichment across tools.

## Existing records, fingerprints and variants

All current primary IDs are read before staging any current chunk. Every incoming
submitted field must match an existing row; unrelated additional top-level fields
may remain. Nested external_ids additions, source/tag/time/provenance changes
are conflicts, holding the whole new input before record mutation. No alias or
fingerprint can substitute for the primary ID. Existing original-payload recovery
uses the unchanged kernel; it is not a global edit transaction.

Fingerprints use the existing movie/TV slug conventions. They identify content,
not an observation: two rewatches may share one. Slim and rich intentionally use
different source namespaces and coexist. Rich does not remove low-confidence slim
rows. Exact already-visible incoming payloads can require zero POST while still
counting as verified current delivery; those counts do not mean newly created
records or sample visibility. See the package guide for uncertainty, chunks and
recovery/current evidence.
