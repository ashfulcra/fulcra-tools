# coord-fold

`coord-fold` maintains an agent's open obligations by replaying annotation
events into a durable checkpoint. It is the annotation-native bus-v4 fold,
separate from [coord-engine](../coord-engine/README.md)'s file-backed views.
It can run without Collect. The engine contains the bridge, comparison, and
cutover controls; having this package installed does not activate a team's cutover.

## Install and prerequisites

Python 3.11+ is required. From the repository root:

```bash
uv run --package coord-fold coord-fold --help
```

The package depends on [fulcra-common](../fulcra-common/README.md) for CLI
lookup. Store access uses an installed, authenticated `fulcra-api` executable.
The team must already have `team/<team>/_coord/bus-v4/records.json` with
`data_type` and `api_version`. There is no default channel and no `init` verb;
a missing or unreadable channel configuration is refused.

## Commands

```bash
coord-fold fold example-team --agent agent-a
coord-fold status example-team --agent agent-a
coord-fold fold example-team --agent agent-a --verify-pointers
```

Use `uv run --package coord-fold` before these commands when running from the
workspace without a separate tool installation.

| Verb | Behavior |
|---|---|
| `fold TEAM --agent AGENT` | Read new events and save the agent's checkpoint. `--max-events` defaults to 5000; a bounded remainder is reported for the next pass. |
| `status TEAM --agent AGENT` | Read the saved checkpoint without fetching new events. It is not a fresh fold. |
| `emit TEAM --from AGENT --to RECIPIENT --kind KIND --slug SLUG --pri P2 --ptr PATH` | Write an event. Kinds are `open`, `close`, `claim`, `release`, and `note`; priorities are P0–P3. `open` and `close` require a file pointer. |
| `claim TEAM SLUG --agent AGENT` | Emit a claim for a row already open in this agent's checkpoint. |
| `release TEAM SLUG --agent AGENT` | Emit a release; the fold removes that obligation from the open set. |
| `close TEAM SLUG --agent AGENT --evidence PATH` | Verify that the evidence file is readable, then emit a close. |

`fold`, `emit`, `claim`, `release`, and `close` write remote state. `status`
only reads. Claim/release/close emit events; run the fold again to reflect them
in the checkpoint. During a dual run with the old bus, use the engine's task and
response workflow to settle the file-backed obligation too; a standalone
`coord-fold close` does not update it.

## Storage and failure behavior

The checkpoint lives at `team/<team>/member/<agent>/fold/checkpoint.json`.
It contains the cursor, open rows, bounded seen-record IDs, remainder count,
unreadable pointers, and generation/writer identity. Obligations belong to
their recipient; a broadcast opens for every recipient except its sender.
An agent's own claim/release/close events also apply to its fold.

Ordinary passes read forward with a five-second overlap and deduplicate seen
record IDs. `--rebuild` replays from the epoch under the current folding rules,
retaining generation/writer checks. It is recovery work, not an ordinary poll.
`--verify-pointers` reads the files referenced by open rows. Team-relative
pointers such as `task/example.md` resolve under `team/<team>/`.

Exit codes are **0** for a completed pass, **2** for refusal, and **3** for
UNKNOWN. A bounded remainder can return 0; inspect `unread_events` before
claiming the stream is caught up. Failed reads and saves return UNKNOWN.
Corrupt checkpoints are refused. A generation change observed before saving
is refused rather than overwritten; this re-read is not a transactional CAS.
The CLI currently renders text, without a `--json` option.

## Development

From the repository root:

```bash
uv run --package coord-fold --extra dev --no-editable pytest packages/coord-fold/tests -q
```

Every maintained Python module is limited to **400 lines**, including the
[`scripts/`](scripts) tree. The [structural checks](tests/test_structural.py),
[tripwire](tests/test_tripwire.py), and
[process-boundary proof](tests/proof/run_proof.py) test different claims.
The proof uses a macOS sandbox to observe and bound store access for the
measured run; it returns UNKNOWN where that sandbox is unavailable. It does
not establish that enumeration is impossible to write in Python.

The [source modules](coord_fold) and [tests](tests) describe current behavior.
See the [coord system specification](../../docs/coord/SYSTEM-SPEC.md) for the
broader file and event model; bridge and cutover controls live in
[coord-engine](../coord-engine/README.md).
