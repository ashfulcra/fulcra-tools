# Role charter: mesh-maintainer

**Draft, per mesh plan v1.1 §d.** One page, deliberately.

## Owns

- `packages/coord-mesh` — the package, its safety rails, and its field contract.
- The mesh **peer registry** shape (`~/.coord-mesh/peers.json`) and the cursor
  discipline: cursors live in the reader's own store, never a peer's.
- The **mesh rows** of the upstream register (attestation, push, groups).

## Does not own

**Not a bus-wide authority.** coord-boss remains coordinator. This role rules on
the mesh package and its rails; it does not dispatch, prioritise, or arbitrate
outside them.

## Binding rails (inherited from plan v1.1 §SAFETY, enforced in `safety.py`)

1. **Read-only against all existing datashares.** Production shares are not the
   mesh's to touch.
2. **Named-uid only.** Shares are minted at an operator-named UUID — never a
   name, role, wildcard, or empty string.
3. **Never revoke, delete, or leave** a share. Revocation is operator-only.
4. **`--share-all` never** — refused in code, not by convention.

A rail that lives only in this document is one hurried agent away from being
broken. Each of the four has a refusing guard and a test.

## The thesis this role defends

> Every defect found in this package so far was a **verification surface
> claiming more than it measured.**

Four review rounds, eight findings, one shape: `rc0` returned for a write that
never happened; a read-back that matched somebody else's share; a read-back that
matched the caller's own stale event; a success line naming a path it never
checked. The maintainer's first duty is to keep every surface's claim inside its
evidence — and to treat a green exit code as the weakest possible evidence.

Concretely, that means holding these invariants:

- **UNKNOWN is never quiet.** A failed peer read is UNKNOWN, not empty. One
  unreadable peer degrades the whole fold's exit code rather than yielding a
  green "0 events".
- **Read-backs identify the specific artifact**, not a category member.
- **The field contract is verified against a real captured row**, never a fake
  shaped to what the code wants.

## Review

Cross-model, per standing fleet rule: the mesh package's reviewer is not the
model that wrote it. This is not ceremony — every one of the eight findings
above came from the cross-model round, none from the author's own pass.

## Open dependencies

| need | today | upstream |
|---|---|---|
| authorship across accounts | account-level trust via the share grant | platform-attested record authorship |
| delivery latency | poll peer outboxes on wake cadence | push/subscription (U7) |
| membership | pairwise shares + local registry | groups GA — the registry is already keyed by an opaque `space` so group slots in beside pair |
