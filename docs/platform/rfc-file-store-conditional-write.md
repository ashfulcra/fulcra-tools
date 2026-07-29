# RFC: Conditional writes (compare-and-swap) on the Fulcra File Store

- **Status:** DRAFT — ask to the Fulcra platform team
- **From:** the fulcra-tools agent fleet (coord-boss; respec working group)
- **Date:** 2026-07-29
- **Evidence base:** `packages/coord-engine` cursor v2 (PR #496, merged),
  shipped with its activation gate **closed** pending exactly this feature.

## The ask, in one sentence

Expose a per-file version token on File Store reads and honor a conditional
upload against it — `If-Match`/ETag semantics with a `412` on mismatch — so
that a write can atomically mean "replace the version I read, or fail."

## Why

The File Store is the durable substrate for multi-agent coordination state:
read cursors, leases, ledgers, task documents. All of these follow the same
loop — read a file, compute an update, write it back. Today every upload is
last-writer-wins: when two writers interleave (two agents; or two containers
of the *same* agent, one stale and finishing an old turn while its
replacement starts), one write silently vanishes. No error, no trace.

For most documents that is tolerable. For a **cursor** it is data loss with
a delay: overwrite a peer's coverage advance and events are skipped forever,
or replayed as new. Our fleet has hit both failure shapes in production this
month, and the engine's new transactional cursor (stage → process → commit)
is precisely the machinery that turns them into loud, recoverable errors —
but its commit is only trustworthy if the store can **reject a stale write**.
Without that, a "commit" is an overwrite with good intentions.

### Why client-side workarounds are not equivalent

- **Compare-then-write on timestamps** (list the file, compare
  created/uploaded time, then upload) is two operations with a gap. Two
  writers both observe a clean timestamp and both write; the second silently
  wins. Agent fleets wake in synchronized bursts (timers, broadcast events),
  so the gap is hit at the worst moments, not rarely. Listing timestamps are
  also minute-granular — same-minute writes are indistinguishable.
- **Write-then-read-back** proves only that your write was current as of the
  read-back; a third write a moment later still wins silently. It looks like
  a safety check and verifies nothing durable. The engine deliberately
  refuses to present read-back as CAS (`doctor` reports the CAS transport
  gate; cursor schema v2 fails closed on transports without it).
- **Client-side leases** (our FileLease) reduce collisions among cooperating
  writers but are themselves lease *files* updated by last-writer-wins
  writes — turtles all the way down until the store can reject staleness.

The defining property we cannot build client-side: the version compare and
the write must be **one atomic server-side operation**.

## Proposed surface

1. **Version token on every file.** A strong ETag (or a monotonically
   increasing per-file generation integer) returned on: upload response,
   download response headers, and file listing entries. Server-assigned;
   opaque to clients.
2. **`If-Match: <token>` on upload.** Token current → write lands, response
   carries the new token. Token stale → **HTTP 412 Precondition Failed**,
   file untouched. The compare and the swap execute atomically under
   whatever concurrency control the store already uses for the write itself.
3. **`If-None-Match: *` on upload** — create-only: succeed only if the file
   does not exist (412 otherwise). Gives fleets atomic "first writer mints
   the document" (lease acquisition, identity minting) for free.
4. *(Optional, weaker)* honor `If-Unmodified-Since` with documented
   second-resolution caveats, for clients that only have timestamps.
5. **Capability discovery.** Advertise conditional-write support
   discoverably (API root document, `OPTIONS`, or a documented version
   floor) so clients can gate behavior without probing by side effect. Our
   engine ships a fail-closed transport gate today; it flips on when the
   capability is provable.

Backward compatible by construction: requests without the headers behave
exactly as today. No data-model change — the store already tracks the
versions it shows in listings.

## What is ready to consume it (day one)

- **coord-engine cursor schema v2** (merged, inactive): stage/commit
  delivery with CAS loser-recovery, waiting on this primitive to activate.
- **FileLease** (bridge + roles): lease acquisition becomes genuinely atomic
  via create-only + If-Match renewal.
- **BridgeLedger** (tracker bridge) and the prefs **Privacy Ledger**:
  append-safe under concurrent writers.
- Every future cursor, ledger, or checkpoint any agent keeps in the store.

## Alternatives considered

- **Records-plane CAS** (conditional annotation writes): does not help; the
  coordination state that needs atomicity lives in files by design (documents
  are the truth; records are wake hints).
- **Server-side locking API** (explicit lock/unlock): heavier, stateful,
  and fails badly with crash-prone ephemeral clients. Conditional writes are
  stateless per-request and match how the fleet already behaves.
- **Living with it**: the fleet's dominant remaining data-loss class. We
  built everything client-side that can be built; this is the floor.

## Filing note

Authored in ashfulcra/fulcra-tools (the working fleet this generalizes
from). Cross-org copy to the platform's tracker is filed separately;
this document is the canonical text.
