---
name: fulcra-csv
description: Use when a user wants to import timestamped CSV data into Fulcra, map columns or units, recover a CSV import, or audit legacy annotation exports.
---

# fulcra-csv

Standalone CSV imports use owned v1 Event/Metric targets on this development
branch. This is separate from installed-app/provider acceptance. Collect's
Generic media CSV plugin shares the pure parser and keeps its own media writer.

## Where to start

| Probe | Command or evidence | Next step |
|---|---|---|
| Installed? | `fulcra-csv import --help` | Use the repository checkout and `uv sync --all-packages --all-extras` if missing. |
| Full target selected? | Recorded `Event/<canonical UUID>` or `Metric/<canonical UUID>` for `--data-type` | Choose an existing compatible owned target, or explicitly run `fulcra-csv bootstrap --base-type Event --name "Imported CSV"`. Import proves ownership/schema afresh. |
| Local rows valid? | `fulcra-csv import input.csv --data-type "$CSV_TARGET" --dry-run` with intended mappings | Fix diagnosed rows/mappings. Preview checks no account, server schema or new-row count. |
| Original work unresolved? | Retained failure/pending evidence from the authorized import | Repeat the original authority to reconcile before new source intake. UNKNOWN is not clear; preserve pending work and original errors. |

Real imports/bootstrap use CLI/environment auth, separate from Collect sign-in.
Use `fulcra auth login` when sign-in is required; do not print access tokens.
An authorized live import recovers original work before opening the selected
file, including missing/newly empty files. Do not replace authority to escape
a hold. There is no standalone CSV status/reset command or cursor.

## Choose a v1 target

`--data-type` is required even for `--dry-run`: full Event/UUID or Metric/UUID.
Bare/annotation/native names such as BodyMass are refused locally.
`--definition-id` is only an opaque retained historical selection paired with
that full target, never target proof or a new annotation marker.

Event supports spans and scalar/string/bool/null values; Metric needs a finite
actual numeric nonbool value. Bootstrap requires `--base-type Event|Metric` and
`--name`, accepts `--description`, and prints the full target. Metric uses exact
`--unit` (omitted means unitless) and `--aggregation discrete|cumulative`
(default discrete); import must supply matching metadata. Event bootstrap
rejects explicitly supplied Metric metadata. Old bootstrap annotation/value/tag
options are removed.

Retry uncertain bootstrap with original name/description/base/metadata. Changed
same-name setup holds; distinct names are distinct intentional tracks. Explicit
pinned import accepts compatible custom names and renames without name-based
ownership assumptions.

## Import and preview

```bash
fulcra-csv bootstrap --base-type Metric --name "Manual weight" --unit kg
# Save the printed full target as CSV_TARGET.
fulcra-csv import weights.csv --data-type "$CSV_TARGET" \
  --annotation-type instant --ts-col date --value-col kg --unit kg --dry-run
# After checking local rows, the authorized real import omits --dry-run.
```

Preview is local strict parsing/codec/label/conflict validation and first-five
sampling only: no runtime/profile/DB/auth/metadata/pending lookup, recovery or
writes. It cannot prove destination compatibility or count new records.

The CLI materializes strict input and validates existing primary/enrichment
compatibility before current metadata/record writes. Finalized tag unions are
proved before effects; unknown new-tag UUID constraints hold rather than create
metadata first. Known tag identities retain guarded rename bindings after proof.
Malformed CSV, missing
requested headers/rows, conflicting identities, nonfinite values and backwards
times fail with diagnostics. Pure parser consumers retain default skip behavior.

| Import options | Purpose |
|---|---|
| `--annotation-type duration\|instant` | Duration default, explicit `--end-col`/`--duration-col` or existing one-second fallback; instant has equal start/end. |
| `--ts-col`, `--tz` | Timestamp header (default timestamp), timezone (default UTC). |
| `--title-col`, `--subtitle-col`, `--note-col` | Title/default-note mapping or explicit note. |
| `--value-col`, `--value-type float\|int\|str\|bool` | Existing parser coercion; empty becomes null. Metric refuses null/string/bool. |
| `--unit`, `--aggregation` | Metric metadata; mapped source unit wins over constant fallback and must equal declared unit without conversion. |
| `--tag-col`, `--tag` | Per-row/default labels; extra labels also retain their order. |
| `--data-field COL=KEY`, `--extra COL=KEY` | Nested `csv_data_fields` and `external_ids`, repeatable. |
| `--source-id-col`, `--source-id-prefix` | Hashed source component, never direct ID; CLI default `com.fulcradynamics.csv.v1`. |

Canonical Metric unit is destination metadata. No record-level unit/csv_unit is
submitted; original nested source units remain. Event unit is only nested source
data. Values, notes/titles, original labels, finite nested data and raw timestamp
identity are preserved by the [canonical payload contract](../../README.md#native-payload-and-identity).

## Recovery, corrections and counts

Stable `cli-csv` authority binds account, full target, spec/unit/aggregation and
explicit legacy selection. File/mappings/prefix/timezone do not rebind original
pending rows. Source IDs keep the existing raw timestamp/note/tag/explicit-ID
hash; changed timestamp spelling can change identity. Native UUIDs qualify the
primary source by destination.

Existing UUIDs need the exact incoming primary source first. Wrong/alias-only
primary or malformed enrichment holds. Compatible user tags/provenance and
declared enrichment survive. Map objects overlay recursively; absent keys remain,
explicit null/scalar/list replaces. These are corrections at the same UUID,
not a deletion interface. Frozen pending rows recover without reparsing a new
source or recomputing its merge.

Exact native readback and exclusions govern completion. Unknown issued uploads
are reconciled, never blindly resent. Retain errors and original pending state;
legacy export or a successful preview cannot prove delivery.

Success reports total/skipped_existing/posted/verified plus separate recovered.
Posted means known delivered current candidates, not HTTP attempts. Failure
counts are complete-group lower bounds; partial group visibility can exceed
those counts. Known counts survive later receipt/clear/tag/chunk/close failures,
and recovery never inflates current counts. There is no caught-up/history claim.

## Legacy audit and administration

`fulcra-csv export` is retained legacy audit, not native v1 export or new-import
verification. Use `--definition-id` and/or legacy `--data-type` with `--start`,
optional `--end`, `--columns`, `--date-format iso|epoch|local`, `--tz` and `--out`.
Existing decoder/filter/formula safeguards remain; it is not a byte-identical
round-trip format.

`fulcra-csv soft-delete <legacy-definition-uuid> --confirm` is explicit legacy
administration; full Event/Metric targets refuse before auth. It is not a native
delete, migration or recovery reset. Retired client record methods fail rather
than fall back to annotation writing.

Read the [package guide](../../README.md) for full schemas, identity/merge rules,
authority, output limits and development checks. Repeated/scheduled ingestion
requires the user's authorization; deterministic IDs alone do not establish
delivery or authorize changing an unresolved import's account/target.
