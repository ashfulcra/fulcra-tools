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
a second surface: `tests/fixtures/` now also holds a verbatim
`share create --help` and a real share row carrying a file grant, and
`test_share_create_contract.py` asserts that **every flag in the argv we execute
exists in the captured help**. A wished-for flag now fails a unit test instead
of a live leg. The same round retired a disclaimer that was itself an
over-claim in the other direction — r3 had concluded a reports prefix was
unobservable from a share row, and the live surface shows it plainly as the
data type `file:/reports/`.

Live legs are documented in [SMOKE.md](SMOKE.md); the role charter is
[docs/coord/MESH-MAINTAINER.md](../../docs/coord/MESH-MAINTAINER.md).

## Two disciplines worth knowing before you edit

**The field contract lives in `wire.py`, verified against a real row.** A
`record_id`-vs-`id` mismatch once poisoned every row of a fold while the suite
stayed green, because the fake emitted what the code wanted. `tests/fixtures/`
holds a real captured record; the contract tests assert against its shape.

**UNKNOWN is never quiet.** A failed peer read is UNKNOWN, not empty — a mesh
that reports "no messages" when it could not read is worse than one that fails.
