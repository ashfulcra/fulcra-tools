# coord-mesh

**L2 of coord.** Cross-user coordination as a mesh of per-user *outboxes*, read
across scoped datashare boundaries.

The load-bearing design choice (plan v1.1 §b): **do not seek cross-account
WRITE.** Each user writes only their OWN channel — their outbox — and peers READ
it across the share boundary. A mesh of outboxes needs no
ingest-into-someone-else's-space primitive, no consent inversion, and inherits
each account's integrity.

## Verbs

| verb | does |
|---|---|
| `mesh init` | create the OUTBOUND share (channel data type + reports dir → named peer uid); read-back verified |
| `mesh peers` | roster fold: incoming shares + my outgoing shares + local registry |
| `mesh send` | write a `to_user`-addressed event to MY channel (+ ptr doc), read-back verified |
| `mesh queue` | poll each peer outbox, fold to one inbox, per-peer cursors, at-least-once |
| `mesh doctor` | per-peer health; LOUD on any UNKNOWN |

## The rails (enforced in `safety.py`, not by convention)

- Read-only against **all** existing datashares — production shares are not ours to touch.
- Test shares only to an operator-**named** uid (UUID-shaped; never a name, role, or wildcard).
- Never revoke, delete, or leave a share — revocation is operator-only.
- `--share-all` is **refused in code**.

## The thesis

> Every defect found in this package so far was a **verification surface
> claiming more than it measured.**

Four cross-model review rounds, eight findings, one shape: `rc0` returned for a
write that never happened; a read-back that matched somebody else's share; a
read-back that matched our own stale event; a success line naming a path it
never checked. Before you add a surface that reports success, ask what it
actually measured — and make the message say only that.

**Instance nine came from the live run, and it is the one worth reading.** The
first two-account smoke died in argparse: `share_create` sent `--file <prefix>`,
an option `fulcra-api share create` has never had. Eighty-five tests were green.
They *could not* have caught it — the author's host cannot run cross-account
share verbs, so every test drove a fake, and the fake accepted the flag its
author wished for. The tests measured the author's belief about the CLI, then
reported it as knowledge of the CLI.

The fix is the same one `wire.py` already applies to `get-records`, extended to
a second surface: `tests/fixtures/` now also holds a captured
`share create --help` and a real share row carrying a file grant, and
`test_share_create_contract.py` asserts that **every flag in the argv we execute
exists in the captured help**. A wished-for flag now fails a unit test instead
of a live leg. The same round retired a disclaimer that was itself an
over-claim in the other direction — r3 had concluded a reports prefix was
unobservable from a share row, and the live surface shows it plainly as the
data type `file:/reports/`.

**Instance ten is the fix for instance nine getting it wrong.** The capture that
closed the `--file` defect was labelled 0.1.40 and came from 0.1.38, so a test
asserting "`--file` does not exist" passed here and was refuted on a reviewer's
genuine 0.1.40. Three hosts each believed they ran 0.1.40; two were wrong. The
version had never been measured, only assumed — and an assumption written into a
docstring is indistinguishable from a measurement afterwards. The lesson is one
layer deeper than "test against a real surface": **a capture must record its own
provenance, measured at capture time.** `tools/capture_fixtures.py` writes the
fixture and stamps it with the installed version it read from the installer;
nothing writes that fixture by hand, and the tests assert against the version it
recorded rather than one a person typed.

**Instance eleven was a test measuring the network's cooperation.** CI caught it:
one test patched the write and left the pre-write snapshot unpatched, so the
snapshot shelled out to the real client. On a host with credentials that read
succeeded, execution reached the patched write, and the test passed — having
made a live network call inside a unit test. On a credential-less runner the
read failed, an earlier guard correctly refused, and the assertion looked at the
wrong branch. Auditing the rest of the suite by eye would have been the same
mistake one level up, so `tests/conftest.py` stubs both routes out of this
package — `transport.run` and `transport.record` — to RAISE. A test that wants
transport behaviour patches it explicitly; a test that reaches the network by
omission gets a named failure. The guard found exactly one offender, which is
also the sweep's result.

The runtime learned the same lesson. `fulcra-api` has no version surface at all
— no `--version`, no `version` subcommand — so `init` does not ask the client
what it *is*; it asks what it can *do*, by probing the help of the binary it is
about to invoke, and **refuses** a reports prefix it cannot deliver instead of
minting a smaller share and reporting success.

Live legs are documented in [SMOKE.md](SMOKE.md); the role charter is
[docs/coord/MESH-MAINTAINER.md](../../docs/coord/MESH-MAINTAINER.md).

## Two disciplines worth knowing before you edit

**The field contract lives in `wire.py`, verified against a real row.** A
`record_id`-vs-`id` mismatch once poisoned every row of a fold while the suite
stayed green, because the fake emitted what the code wanted. `tests/fixtures/`
holds a real captured record; the contract tests assert against its shape.

**Cursors anchor to the NEWEST row, and the platform returns the OLDEST first.**
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
  idempotent. `mesh queue` now says so on stderr when it happens, naming the
  cursor and the window, so a replay never costs an investigation again.
- **A read that cannot identify a row degrades the whole peer**, not just that
  row. Position in an ordered stream is unknowable past an unidentifiable
  record, so a partial slice would be a claim we cannot support.

**UNKNOWN is never quiet.** A failed peer read is UNKNOWN, not empty — a mesh
that reports "no messages" when it could not read is worse than one that fails.
