# fulcra-dayone

Import selected [Day One](https://dayoneapp.com) journal entries into your
Fulcra account, carrying normalized entry text, Day One tags, and lightweight
metadata (journal, location, word and photo counts). On the development branch,
Collect and the standalone CLI share the true v1 Journal Event writer below.
Signed app publication and historical conversion remain pending.

## Standalone setup

Python 3.11+ and `uv`. From the repository root:

```bash
uv sync --all-packages --all-extras
source .venv/bin/activate
fulcra auth login
fulcra-dayone --help
```

The package depends on `fulcra-common`, `fulcra-csv-importer`, and
`fulcra-collect` for its shared client and plugin interface. Running the CLI
does not require a Collect daemon. JSON exports can be read off macOS; reading
the local app database requires the Mac that has that database.

## Standalone v1 authority and recovery (development branch)

The CLI uses explicit environment/CLI authentication through the shared client,
with fresh API account verification. It does not adopt Collect's saved sign-in
or require an enabled Day One daemon source. Collect's own writer continues to
require Collect authentication. Use `--destination "Event/<UUID>"` to pin an owned,
compatible v1 Journal target; omitting it uses shared owned-type resolution.
A bare legacy annotation UUID is rejected and requires an explicit v1 mapping.

Each real invocation uses the fixed `cli-dayone` namespace under the selected
Collect profile, separate from daemon Day One state. A bounded exclusive CLI
lease, shared task/config and account-record leases serialize work within that
profile. Settings include absolute export/explicit DB paths, filters and target.
Before opening the source, discovering a default DB, returning an empty selection
or creating metadata, the CLI resumes its original durable pending batch. Changed
account/options retain pending work and hold; restore the original invocation to
reconcile it. An issued upload that remains absent is UNKNOWN and is not sent
again. Do not erase pending state to unblock an import. This runtime uses the
same [DB protocol and rollback requirements](../collect/README.md#v1-writer-migration-development-branch)
as Collect; stop older writers before upgrading shared profile state.

With `--local-db` and no `--db-path`, the stable default selector is captured and
the current default database is discovered only after recovery. It may select a
different database on a later invocation. Pass an explicit `--db-path` to bind a
particular absolute path. Pending payload recovery does not reread the old source.

`--dry-run` reads and filters local input only: no authentication, profile/DB
initialization, metadata, provider requests or pending drain. It is different
from Collect's configured preview, which can compare against its existing target.

The Python annotation API is retired. `ensure_journal_definition()` and the old
GenericEvent `run_import()` raise explicit migration errors. Python callers use
`with DayOneFulcraClient().open(settings) as writer:`, call `writer.resume()`
before source access, then pass original `DayOneEntry` values to
`writer.run_import(entries)`. The schema, source identity, exact readback and
import-once behavior are shared with Collect. This is profile-local serialization,
with no cross-host or cross-profile transaction guarantee.

## Use with Collect

Day One is included in the [Mac installer](../../docs/collect.md#get-started-new-user).
Choose **Day One → Set up** in the dashboard, select a local database or export,
and follow the access and destination steps. Local database import requires Full
Disk Access for **Fulcra Collect** from Applications. A source install needs the
same access for the terminal app that starts the daemon. Keep Day One open so
entries from other devices can reach this Mac.
See the [source guide](../../docs/how-do-i-get-my-data.md#day-one) for details.

The commands below describe the standalone CLI, including its filtering options.

### Collect v1 writer (development branch)

[`JournalWriter`](fulcra_dayone/collect_v1.py) captures Collect's signed-in account,
source epoch and raw settings through the [shared session](../collect/README.md#v1-writer-migration-development-branch).
It reconciles an immutable pending batch before reading the next source. An
account/epoch/settings mismatch retains that batch and blocks further source
access; uncertain uploads cannot produce a successful receipt. **Preview only**
compares entries without creating metadata, writing bindings or draining pending
uploads. Configure the destination first so preview has an existing v1 target
and replay-exclusion control to read.

Setup uses `v1_destination_ids.journal` with a full owned `Event/<UUID>` target
whose served schema is compatible with [`JOURNAL_SPEC`](fulcra_dayone/v1.py).
An existing annotation selection requires an explicit v1 mapping; the old
definition and watermark remain intact. Tag labels longer than 30 characters
use the shared tag resolver without shortening their record enrichment.

Records have equal UTC `start_time`/`end_time`, the original case-sensitive
`com.fulcra.dayone.<hash>` source and the shared destination/source UUID mapping.
Declared fields are `note`, nullable `title`, `external_ids` and
`dayone_tag_names`. Live entries retain the ordered original tag labels; a
historical converter may use null only when those original labels are unknown.
Existing normalization replaces Day One media links with `[photo]`; photos and
attachments are not uploaded. Do not fabricate original text from cleaned notes.

Import-once behavior is preserved: an existing exact ID with the same primary
source is skipped, retaining cloud edits and tags. New entries are staged before
upload and counted only after exact readback. Replay exclusions are checked
before existing-ID shortcuts and around delivery; observed revivals are repaired
with native deletion, and excluded entries do not count as uploaded or verified.
Conflicting duplicate source payloads or invalid later entries reject the whole
input before metadata mutation. Receipt ordering compares UTC instants, including
subsecond timestamps. Real-source, historical conversion and packaged-app QA
remain [migration gates](../../docs/collect-v1.md).

### Opt-in historical Journal contract (development branch)

[`UNIFIED_JOURNAL_SPEC`](fulcra_dayone/v1.py) is a separate local contract
revision, `dayone.journal.v2`, named **Day One Journal**. Its wire datatype is
still API v1 Event. The existing `JOURNAL_SPEC`, creation fingerprint, live
writer defaults, cached pins and pending batches remain unchanged. No resolver
or importer selects the new contract automatically. An existing v1 target is
not implicitly upgraded; a different target changes destination-scoped record
IDs and needs an explicit canonical mapping and replay/cutoff plan.

The new contract requires `note`, `title`, `external_ids`, `dayone_tag_names`
and `legacy_record`. Historical enrichment that was never retained becomes
explicit null; malformed retained values quarantine rather than disappear.
The opt-in `build_unified_journal_record` calls the existing strict live builder
first and adds `legacy_record: null`, preserving live-source validation.

[`JournalHistoryConverter`](fulcra_dayone/history_v1.py), converter ID
`collect.journal.reader.v1`, converts only the observed legacy MomentAnnotation
reader shape. It checks the plan's typed definition ID, account, moment kind,
both annotation-source bindings, exactly one lowercase Day One hash primary,
UUID tags and an aware in-range scalar time. These are enumerated binding
checks, not proof of a whole definition snapshot. It uses the shared
destination/source identity mapping and equal UTC event timestamps.

Historical `legacy_record` has a closed versioned envelope: `version: 1`,
`converter`, `source_api_version: v1alpha1`, `source_data_type` and `raw`.
The complete original JSON object stays in `raw`, including unknown fields,
omitted versus null values, source order, original time spelling and the
independent record/definition display names. This preserves JSON content;
the retained response artifact preserves original bytes. Raw and proposed
payloads each have the existing 1 MiB canonical encoded-JSON bound. A retainable
row whose duplicated provenance makes the payload too large quarantines without
truncation. The codec rejects schema reference keywords in every branch and
validates locally against the frozen contract and full destination schema.

This is a pure library codec, with no creation, routing, upload or completeness
authority. The [Collect registry and ledger](../collect/README.md#offline-historical-migration-foundation-development)
retain originals, regenerate conversion proofs and quarantine conflicting
proposals. Provider schema acceptance, explicit old-pin recovery, source cutoff,
historical apply/readback and coverage remain separate gates. Existing live
import-once behavior still preserves cloud edits; historical conflicts do not
authorize overwriting them.

## Input modes

Day One has no read API and its CLI is write-only, so entries come from
either a JSON export or the app's local database:

- **JSON export** — in Day One, File -> Export -> JSON. Pass the resulting
  `.zip`, or an unzipped folder.
- **Local database** — `--local-db` reads Day One's local SQLite store
  directly (no manual export). Unofficial: it can break on a Day One
  update, and it skips entries with no readable text.

## Usage

```bash
# JSON export, filtered
fulcra-dayone import ~/Downloads/Export.zip --journal Personal --tag fulcra
fulcra-dayone import ~/Downloads/export-folder --since 2024-01-01 --starred

# Explicit owned v1 target (replace the placeholder with its UUID)
fulcra-dayone import ~/Downloads/Export.zip --all --destination "Event/<UUID>"

# Local database
fulcra-dayone import --local-db --tag fulcra

# Preview without posting
fulcra-dayone import ~/Downloads/Export.zip --all --dry-run
```

Filter categories (`--tag`, `--journal`, `--since`, `--until`, `--starred`)
combine with AND; repeated tags or journals match any value within that category.
Date boundaries are inclusive UTC dates. With no filter, `--all` is required — a guard against an
accidental full import. Re-running is safe: entries dedup on a stable
`source_id` derived from the Day One entry uuid. This is not update sync:
edits to an entry already imported with that UUID are skipped. Photos and
attachments are not uploaded; only text and metadata are imported.
`--db-path` overrides the database location when paired with `--local-db`.

## Develop

```bash
uv run --package fulcra-dayone --extra dev pytest -c pyproject.toml packages/dayone/tests -q
```
