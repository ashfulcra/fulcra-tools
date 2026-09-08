# coord-mesh

**Experimental cross-user coordination.** A mesh of per-user *outboxes*, read
across scoped datashare boundaries.

The design choice: **do not seek cross-account
WRITE.** Each user writes only their OWN channel — their outbox — and peers READ
it across the share boundary. A mesh of outboxes needs no
ingest-into-someone-else's-space primitive, no consent inversion, and inherits
each account's integrity.

## Install and use

Requires Python 3.10+ and an installed, authenticated `fulcra-api` CLI. The
Python package has no runtime dependencies. From the repository root:

```bash
uv tool install ./packages/coord-mesh
coord-mesh --help
```

The executable is `coord-mesh`. `--channel` is a global option and must appear
before `init`, `send`, `queue`, or `doctor`. Supply an existing
`MomentAnnotation/<uuid>` outbox channel; `init` creates a scoped share, not the
channel itself. Collect is not required.

```bash
coord-mesh peers
coord-mesh --channel 'MomentAnnotation/<CHANNEL_UUID>' doctor
coord-mesh --channel 'MomentAnnotation/<CHANNEL_UUID>' queue \
  --peer '<PEER_USER_UUID>' --me '<YOUR_USER_UUID>' --no-advance
```

Replace the placeholders with your own IDs. Queue requires explicit `--peer`
arguments; repeat the flag to poll multiple users. It does not discover queue
recipients from the roster. `--no-advance` leaves local cursors untouched.

`init <PEER_USER_UUID>` grants that user access to the selected channel and,
by default, `reports/`. Use `--reports ''` for a channel-only share. A reports
grant requires a CLI whose help advertises file-grant support; the runtime
probes that capability before creating the share.

The CLI launcher override is `FULCRA_CMD`. Peer cursors default to
`~/.coord-mesh/peers.json`; override the path with `COORD_MESH_PEERS`.
Exit codes are **0** for completed, verified operations, **2** for refusal or
usage errors, and **3** for UNKNOWN. `send --dry-run` prints the payload and
returns 3 deliberately: it has not sent anything.

## Verbs

| verb | does |
|---|---|
| `coord-mesh init` | create a new outbound share (existing channel data type + reports prefix → named peer uid); read-back verified |
| `coord-mesh peers` | roster fold: incoming shares + my outgoing shares + local registry |
| `coord-mesh send` | write a `to_user`-addressed event to my channel, read-back verified; `--ptr` references a document but does not upload it |
| `coord-mesh queue` | poll each peer outbox, fold to one inbox, per-peer cursors, at-least-once |
| `coord-mesh doctor` | per-peer health; LOUD on any UNKNOWN |

## The rails

Enforced in [`safety.py`](coord_mesh/safety.py):

- Read-only against **all** existing datashares — production shares are not ours to touch.
- Test shares only to an operator-**named** uid (UUID-shaped; never a name, role, or wildcard).
- Never revoke, delete, or leave a share — revocation is operator-only.
- `--share-all` is **refused in code**.

## Verify the external contract

A passing fake transport does not prove the installed CLI accepts the same
flags or returns the same fields. The
[share-create contract tests](tests/test_share_create_contract.py) compare the
executed arguments with captured CLI help. The
[capture tool](tools/capture_fixtures.py) records the installed version when it
captures fixtures, so provenance is measured rather than typed into a comment.
Tests also block unmocked transport calls through
[`tests/conftest.py`](tests/conftest.py).

The runtime checks file-grant support through the help of the CLI it will
invoke. If that capability cannot be established, it refuses to report a
reports prefix as shared. Verification procedures are in [SMOKE.md](SMOKE.md);
the [role charter](../../docs/coord/MESH-MAINTAINER.md) describes maintenance.

## Two disciplines worth knowing before you edit

**The field contract lives in [`wire.py`](coord_mesh/wire.py).** A
`record_id`-vs-`id` mismatch once poisoned every row of a fold while the suite
stayed green, because the fake emitted what the code wanted. [`tests/fixtures/`](tests/fixtures)
holds captured record shapes; the contract tests assert against those shapes.

**A cursor is a MONOTONIC WATERMARK plus a LEDGER, never a single row id.**
The position is the newest instant consumed, together with the ids seen at that
instant; a row is new when it post-dates the watermark, or shares it and is not
in the ledger. That test asks membership, never id order — because id order is
not append-monotonic. A record arriving later with the same timestamp and a
lexically smaller id sorts *before* any single-id cursor, lands in the
already-seen slice, and disappears. Same shape coord-engine's E1 fold settled
on, for the same reason. The watermark never moves backwards: a window whose
maximum falls below it — retention trimming, a narrowed `--window`, a peer whose
newest rows stopped being served — leaves the position alone and says so.
"I saw less this time" is not evidence of having seen less overall.

**Cursors anchor to the NEWEST row, and the order is PROVEN on every read.**
`get-records` yields rows in ascending `recorded_at` order. A cursor is a
position in that stream, so it advances to the LAST row of the read, not the
first. Getting this backwards cost a real incident: the cursor sat on the oldest
row, every subsequent read stopped at row 0 and printed "0 event(s)" while
addressed events sat unshown below it, and when that row aged out of the window
the entire window replayed. The replay is what got noticed; the silence is what
it cost. Two consequences worth keeping in mind as a consumer:

- **Expect occasional full-window re-delivery.** If a cursor ages out of the
  read window — a peer goes unpolled longer than `--window` — the window is
  replayed. This is legal under at-least-once and your handling should be
  idempotent. `coord-mesh queue` now says so on stderr when it happens, naming the
  cursor and the window. Events older than the returned window may have been
  missed entirely; at-least-once handling cannot recover events that were never read.
- **A read that cannot identify a row degrades the whole peer**, not just that
  row. Position in an ordered stream is unknowable past an unidentifiable
  record, so a partial slice would be a claim we cannot support.
- **An order we cannot prove is UNKNOWN.** Every row must carry a parseable
  `recorded_at` and the window must be monotonic, or the peer degrades and the
  cursor does not move. The first fix for the anchoring bug took one measured
  ascending response as a permanent transport contract — the same unverified
  assumption with the sign flipped, and a descending response would have
  restored the silent-loss half of the very bug it removed. One measurement is
  an observation; a contract is something you re-check on every read.

**UNKNOWN is never quiet.** A failed peer read is UNKNOWN, not empty — a mesh
that reports "no messages" when it could not read is worse than one that fails.

## Test

Peer and cursor fixtures use invented identifiers; do not copy an account or
device identifier into a test to make it look realistic.

From the repository root:

```bash
uv run --package coord-mesh --extra dev --no-editable pytest packages/coord-mesh/tests -q
```
