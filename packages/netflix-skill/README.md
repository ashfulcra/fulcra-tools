# fulcra-netflix-skill

An agent-guided Netflix export/import workflow plus an installed native importer.
The canonical script serves the console entrypoint and the bundled skill; it
writes owned v1 Event records to the shared Watched destination on this development
branch. Released app/native-sharing/historical-conversion acceptance remain
separate. The original annotation writer is retired.

The [agent guide](skills/fulcra-netflix/SKILL.md) covers authentication, export,
reviewed import and the optional explicitly legacy sharing boundary. The
[original design](docs/design.md) records the earlier annotation-based approach;
it is historical context, not the native runtime contract.

## Install and invocation

Use a checkout pinned to the verified published release commit, Python 3.11+ and
`uv`. From its repository root:

```sh
uv sync --all-packages --all-extras
source .venv/bin/activate
fulcra auth login
fulcra-netflix viewing-history.csv --check-only --json
fulcra-netflix viewing-history.csv --json
```

The final public release must supply and verify its exact release ref and clean
installation; the current development checkout is not that receipt. The package
requires the installed `fulcra-common`, `fulcra-collect` and
`fulcra-media-helpers` siblings. Do not assume current PyPI version numbers expose
the required development APIs or dynamically install them from the script.

The console entrypoint delegates through `fulcra_netflix.loader` to the same
canonical script. An installed checkout's `.venv/bin/python` can also invoke
`packages/netflix-skill/skills/fulcra-netflix/scripts/netflix_import.py` directly.
The script is no longer a self-contained PEP 723 upload. A skill-folder-only copy
supports local check-only under Python 3.11+; live upload requires the installed
adapter/runtime. Missing/incompatible version/namespace/API capabilities fail
before authentication, runtime entry or source access, without a legacy fallback.

## Source and destination

Headers select the export variant; main opens one reader after recovery and
materializes the full input before new metadata/record effects. Existing public
parser algorithms and source hashes stay aligned with their established vectors:

- **Slim:** `Title,Date`; noon UTC, one-second interval, low confidence. Repeated
  identical raw date/title pairs retain separate file-order occurrence IDs.
- **Rich:** GDPR ViewingActivity; real UTC timestamp/duration, profile/device
  provenance and high confidence. Supplemental trailer/promotional rows remain
  excluded by the existing parser.

Native records keep original note/title/integer duration/external fields,
including nested fingerprint and confidence, plus declared service/importer/
category/top-level confidence. Tags stay empty; fingerprint is provenance, not an
alternate primary source. Primary record UUID qualifies the unchanged source ID
by full Event destination. Full [native schema/identity contract](skills/fulcra-netflix/references/record-schema.md).

Optional `--data-type Event/UUID` pins an owned compatible destination. Without it,
the shared full-spec Watched resolver/journal may adopt/create after local input
validation. It proves ownership, base, API version, recordability and served
schema; it never retypes or discovers a native target through the legacy
annotation description marker. Shared key `collect.media.watched.v1` means a
prior media binding constrains target substitution and preserves renames. A bound
missing/deprecated/incompatible destination holds; explicit pin cannot silently
replace it. Never reset journals to evade that conflict.

All finalized new payloads pass complete served-schema and current primary-ID
proof before current record mutation. A differing existing incoming field holds
the whole new input before chunk one; unrelated extra top-level cloud fields may
remain. Nested external-ID changes, source/tag/time or note differences are
conflicts. Same source hashes do not prove two tools emitted equal native content.
This is local serialization, not provider compare-and-swap or a correction API.
Already-staged recovery retains its original payload/kernel behavior.

## Recovery, chunks and counts

Live authority uses CLI authentication independently of Collect's daemon account,
within the selected `FULCRA_COLLECT_HOME` profile (default
`~/.config/fulcra-collect`) and dedicated `cli-netflix` namespace. Stable authority
captures codec/spec and explicit target or null, not CSV path/variant/current rows
or discovered target. Changing profile/account/pin/spec holds pending work.

Original pending targets are fully envelope/scope checked, then all are freshly
proven against the watched spec with no target creation before drain. Recovery
finishes before opening/detecting/parsing new input, even missing/empty/bad CSV.
Kernel payload/schema/dispatch proof remains per recovered group; it is not a
whole-pending-payload preflight. Unknown issued POSTs stay retained and reconcile
by exact reads rather than blind resend. Do not erase outboxes or invoke the old
CLI token/annotation helpers to clear a hold.

The whole NEW input is parsed and preflighted first, then at most 500 records per
chunk are staged and drained serially. Only the in-flight chunk is durable.
Unstarted remainder requires the original source on a later invocation; this is
not a durable whole-file queue or atomic import. Every delivered record needs
exact v1 readback, not upload acceptance or a three-row sample. Exclusions are
separate. `--no-verify` is refused before auth; verification cannot be disabled.

`--check-only` is purely local and needs no target/runtime/httpx/auth/profile/DB
or recovery. It reports parse intent; it does not prove ownership or clear old
pending work. Malformed late source prevents NEW metadata/record effects, while
original-pending recovery may already have completed. CLI errors are sanitized;
inspect local source separately instead of expecting raw values in logs.

`--json` retains the original envelope keys with explicitly versioned native
count semantics. `posted` and `verified` are known current completed-chunk/group
verified lower bounds, including exact already-visible records, not POST attempts,
new-record counts or sampled verification. `skipped_existing` remains null. `would_post` is check-only intent. Recovery
counts remain separate; never sum retries. If a chunk lacks complete group proof,
its lower bound may be zero despite some rows being visible. Earlier completed
chunks survive later failures/clear/receipt/close errors. A nonzero exit/UNKNOWN
holds pending work; no output claims rejected-or-empty provider state from a
failure. Final JSON success follows context cleanup, not merely its last POST.

## Sharing and preserved history

Personal native import can complete without sharing. Native Event share propagation
and movie-night pool discovery are UNKNOWN here; a legacy annotation browser
share does not prove it exposes these new records. Do not promise Netflix-only
native sharing, select a default recipient or change permissions automatically.

The retained optional legacy workflow is explicitly all-annotation sharing through
Context Web, only for a recipient the user chooses/confirms. Its broad scope must
be disclosed whenever that workflow is discussed. Preserve original annotation
records, shares and permissions. Legacy wrapped-reader/export shapes and historical
conversion need separate evidence; no native history migration is implied.

## Tests and distribution limits

From the repository root:

```sh
uv run --offline pytest -c pyproject.toml packages/netflix-skill/tests -q
```

Synthetic actual-main tests exercise the installed adapter, real session/outbox
and MockTransport with fake credentials. Package-local function-scoped guards
block subprocess/OS shell, connection/DNS, real HTTP transports and saved-auth
fallbacks before canonical script loading, then restore at test teardown. This is
a Python test boundary, not an OS sandbox; it cannot prove absence of effects in
an earlier unguarded process. Trace and fence both pre-migration and replacement
auth paths before an actual-caller RED test.

Wheel bytes must include the same adapter and bundled script. Clean pinned-checkout
installation plus non-editable installed-entrypoint acceptance remain release
gates; editable imports and source tests do not certify them. No real account,
CSV, native sharing, app rollout or historical readback completion is established
by synthetic tests alone.
