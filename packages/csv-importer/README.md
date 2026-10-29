# fulcra-csv-importer

Map timestamped CSV rows to owned v1 Event or Metric destinations in
[Fulcra](https://fulcradynamics.com). The pure parser also serves Collect's
[Generic media CSV plugin](../media-helpers/README.md), which keeps its own
media writer and authority.

**Development branch, 2026-10-24:** standalone import and bootstrap now use
native v1 records. This source change is separate from installed-app/provider
acceptance and historical conversion. The released installer has not switched
to this implementation.

## Install and authentication

From this repository's checkout, with Python 3.11+ and `uv`:

```bash
uv sync --all-packages --all-extras
source .venv/bin/activate
fulcra-csv --help
fulcra auth login
```

The standalone CLI uses explicit CLI/environment authentication, including
`FULCRA_ACCESS_TOKEN`, with fresh API account proof. It does not inherit Collect
sign-in. It needs the declared sibling `fulcra-common` and `fulcra-collect`
dependencies; no daemon needs to be running. Parser-only imports remain lazy
and do not import the Collect runtime.

## Choose a v1 target

Real import and dry-run require `--data-type Event/<canonical UUID>` or
`--data-type Metric/<canonical UUID>`. Bare UUIDs, legacy annotation names and
native-name aliases such as `BodyMass` are refused before authentication or
source access. `--definition-id` is retained only as an opaque historical
selection paired with a full target; it neither authorizes that target nor adds
an annotation marker to new sources.

Use Event for scalar/string/bool/null observations or spans; use Metric for
actual finite numeric values. Choose or explicitly bootstrap an owned compatible
target. Import freshly verifies account, base, schema and Metric metadata; it
never creates a missing destination.

```bash
fulcra-csv bootstrap --base-type Event --name "Imported CSV"
fulcra-csv bootstrap --base-type Metric --name "Manual weight" --unit kg
```

Save the printed full target as `CSV_EVENT` or `CSV_METRIC` for the examples.
Bootstrap accepts `--description`; its default is
`Timestamped CSV observations with preserved source fields.` Metric uses
`--aggregation discrete|cumulative` (default `discrete`) and an optional exact
`--unit`; omitted unit means unitless. Event bootstrap rejects explicitly supplied
Metric metadata. The old bootstrap annotation/value/tag options are removed.

Bootstrap's exact name identifies its durable setup intent. Retry with the
original name, description, base and Metric parameters after an uncertain
result; changing same-name metadata holds instead of creating again. Distinct
names represent explicitly distinct targets, not a recovery workaround.
Custom display names/descriptions and later target renames work with explicit
pinned import because ownership/schema proof does not depend on display names.

## Examples

### Numeric observations

```bash
fulcra-csv import weights.csv \
  --data-type "$CSV_METRIC" --annotation-type instant \
  --ts-col date --value-col kg --value-type float --unit kg \
  --tag manual-scale
```

Metric destination metadata must match `--unit` and `--aggregation` exactly.
A nonempty mapped source unit takes precedence over the constant fallback;
mixed or unequal units fail without conversion. Omitted `--unit` means unitless,
not inferred source units. Event uses the constant only inside source data.

### Event values and spans

```bash
fulcra-csv import mood.csv \
  --data-type "$CSV_EVENT" --annotation-type instant \
  --value-col score --value-type int --note-col note --dry-run

fulcra-csv import plays.csv \
  --data-type "$CSV_EVENT" \
  --ts-col ts --title-col track --subtitle-col artist \
  --source-id-col track_id --tag spotify --extra url=spotify_url
```

Preview strictly parses and checks local rows, samples the first five and
explicitly says the destination is not verified. It performs no authentication,
runtime/profile/DB entry, metadata lookup, pending inspection/recovery or writes.
It cannot tell how many rows are new.

`--annotation-type duration` is the default; supply `--end-col` or
`--duration-col`, otherwise the parser keeps its existing one-second sentinel.
`instant` emits equal start/end and rejects duration-only flags. Backwards times
fail before current effects. Existing timestamp parsing and explicit
`--tz` behavior remain.

## Column and value options

| Options | Meaning |
|---|---|
| `--ts-col`, `--end-col`, `--duration-col` | Timestamp (default `timestamp`), explicit end or duration seconds. |
| `--title-col`, `--subtitle-col`, `--note-col` | Title (default `title`), subtitle-derived note or explicit note. An explicitly requested column must exist. |
| `--value-col`, `--value-type float\|int\|str\|bool` | Existing parser coercion; empty value becomes null. Metric refuses null/string/bool/nonfinite values. |
| `--tag-col`, `--tag` | Per-row tag or default. Original labels and extra labels survive separately from resolved UUIDs. |
| `--data-field COL=KEY` | Repeatable mapped field in `csv_data_fields`. |
| `--extra COL=KEY` | Repeatable mapped field in `external_ids`. |
| `--source-id-col`, `--source-id-prefix` | Hashed explicit identity component; default CLI prefix `com.fulcradynamics.csv.v1`. |
| `--tz` | IANA timezone for naive timestamps; default `UTC`. |
| `--unit`, `--aggregation` | Exact Metric metadata plus constant nested source-unit fallback; aggregation defaults to discrete. |
| `--dry-run` | Local strict preview with a syntactic full target. |

The CLI opts into strict parsing: malformed CSV, duplicate headers/mappings,
missing requested columns, malformed/blank required rows, invalid values and
nonfinite JSON fail with diagnostics. It materializes the whole current input
before current tag/record mutations. The parser library's default skip behavior,
confidence helpers and media consumers are unchanged; `parse_csv(strict=True)`
is an explicit opt-in.

## Native payload and identity

[The pure codec](fulcra_csv/v1.py) uses flat native base fields:
`id`, `sources`, `tags`, `start_time` and `end_time`, plus Metric `value`.
Every row declares required `csv_annotation_type`, `note`, nullable `title` and
`csv_tag`, `csv_extra_tags`, `csv_data_fields` and `external_ids`.
Event additionally declares scalar/null `csv_value`. Arbitrary finite nested
source fields stay nested, including keys named like base fields; they cannot
overwrite identity, times or values.

Canonical units live in Metric destination metadata. No top-level record
`unit` or `csv_unit` is submitted. Original nested source units remain in
`csv_data_fields`, including the existing constant fallback behavior.
Primary and extra tag labels resolve through guarded shared UUID bindings;
long/slashed labels retain their original spelling in enrichment.
Existing labels are planned read-only, and finalized tag unions must pass before
current effects. Missing-tag creation requires a schema proof that is valid for
an unknown returned UUID; unsupported constraints hold before tag creation or
exclusion repair. Known existing bindings are remembered only after full proof;
a changed/disappeared planned identity holds instead of creating a replacement.

The unchanged primary formula is
`derive_source_id(prefix, ts_raw, note, tag, explicit_id)`: SHA256 of the
pipe-joined fields, None as empty, first 16 hex characters, with the prefix.
The trimmed original timestamp spelling participates; equal normalized instants
with different spellings can have different source IDs. Parser default prefix
`fulcra-csv.v1` remains distinct from the CLI default. The native UUID is
destination-qualified through the shared mapping. No fingerprint/alias or
cross-source-content suppression is imposed.

Existing UUIDs must have the exact incoming primary source first in a valid
sources list. A different or alias-only primary holds. Compatible existing
enrichment is merged before staging: incoming source-owned fields replace;
absent map keys remain, explicit null/scalar/list replaces, and object/object
maps recurse. Valid user tags and provenance sources survive with stable dedup.
Only declared compatible enrichment is retained; server metadata is not copied.
Malformed/incompatible existing state fails rather than losing fields.
This is patch semantics, not a deletion API. Identical merged rows skip;
authorized same-primary corrections keep the UUID and require exact readback.

## Recovery and truthful progress

[The CSV writer](fulcra_csv/standalone_v1.py) uses private `cli-csv` state under
the selected Collect profile (`FULCRA_COLLECT_HOME`, otherwise
`~/.config/fulcra-collect`). Original immutable pending rows recover before
opening/parsing a newly selected source, even when that file is missing or empty.
Preview never drains them.

Authority binds account, explicit target, codec/spec, unit/aggregation and
historical selection. File path, column mappings, timestamp prefix, timezone
and preview are invocation choices, not authority changes. Changing authority
holds original work; do not reset, change names or source prefixes to evade it.
Pending recovery uses original frozen merged rows, never the new source.

Every new batch is staged before dispatch. Exact native ID and submitted-field
readback, exclusions and once-only dispatch fences govern completion. A lost
reply is UNKNOWN until reconciled; an issued batch is never blindly resubmitted.
Recovery is not proof the newly selected source completed.

Success reports `total`, `skipped_existing`, `posted`, `verified` and separately
`recovered`. Skips include identical input duplicates, exact compatible existing
rows and exclusions. Posted means known delivered current candidates, not HTTP
attempts. Failures retain known complete-group lower bounds through later
receipt, clear, tag, chunk and context-close errors. A partially finished group
may have visible records without a positive count; zero is not absence proof.
Preserve original errors/pending evidence; no success footer is emitted on
failure. There is no CSV cursor or caught-up/history claim.

## Legacy export and administration

`fulcra-csv export` remains a legacy read/audit surface. It is not a native v1
export or verification of the new importer. Keep its existing definition/type
filters and decoder semantics:

```bash
fulcra-csv export --definition-id 11111111-2222-3333-4444-555555555555 \
  --start 2026-01-01 --end 2026-05-01 \
  --columns start_time,end_time,title,note,tag,data.score --out legacy.csv
```

`--data-type` can select a legacy underlying series; `--start` is required,
`--end` defaults to now. `--columns` supports known fields, `data.KEY` and
`external_ids.KEY`; `--date-format iso|epoch|local` and `--tz` control rendering.
Formula-leading cells keep the quote guard; booleans keep their existing
lowercase representation. `ExportOptions.guard_formulas=False` is a library
override, not a CLI flag.

`fulcra-csv soft-delete <legacy-definition-uuid> --confirm` remains explicit
legacy administration. Full Event/Metric targets are refused before auth.
Soft-delete does not establish native record deletion, a migration or a reset.

The old CSV client record seams `_build_record`, `ingest_batch` and `run_import`
now fail before authentication, input iteration, empty success or preview.
Read/admin/tag compatibility remains. Do not restore wrapped annotation writing
as a fallback.

## Development checks

```bash
uv run --offline pytest -c pyproject.toml packages/csv-importer/tests \
  packages/collect/tests/test_standalone_v1.py \
  packages/media-helpers/tests/test_generic_csv_importer.py -q
```

Tests use synthetic inputs, isolated profiles and MockTransport through the
actual Click/session/outbox/kernel paths. Live provider, installed app, native
history conversion and publication remain separate gates. For agent operation,
read [the packaged skill](skills/fulcra-csv/SKILL.md).
