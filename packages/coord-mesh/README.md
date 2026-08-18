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

## Two disciplines worth knowing before you edit

**The field contract lives in `wire.py`, verified against a real row.** A
`record_id`-vs-`id` mismatch once poisoned every row of a fold while the suite
stayed green, because the fake emitted what the code wanted. `tests/fixtures/`
holds a real captured record; the contract tests assert against its shape.

**UNKNOWN is never quiet.** A failed peer read is UNKNOWN, not empty — a mesh
that reports "no messages" when it could not read is worse than one that fails.
