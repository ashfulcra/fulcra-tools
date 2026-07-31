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
last-writer-wins for **currency**: when two writers interleave (two agents;
or two containers of the *same* agent, one stale and finishing an old turn
while its replacement starts), the earlier write survives as an archived
version — the store is append-only underneath — but it silently stops being
current, and the superseded writer has no way to learn that at write time.
For a cursor, "silently lost currency" and "lost data" are operationally the
same failure: coverage the fleet believes in has been re-marked by a writer
that never saw it.

For most documents that is tolerable. For a **cursor** the stolen currency
has delayed consequences: re-mark a peer's coverage advance and events are
skipped on every future read (recoverable from the version chain, but only
after someone notices),
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
  so the gap is hit at the worst moments, not rarely — and the comparison is
  non-atomic no matter how precise the timestamps are.
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

## Measured behavior this proposal builds on (probed 2026-07-30)

Raw-REST observations against the live API, reproducible:

1. Upload is two-step: `POST /input/v1/file` (metadata; returns a fresh
   version UUID *before* bytes move) → signed **GCS resumable session**
   (bucket-side object name is stable per path, so GCS generations advance
   on one object per overwrite).
2. Every upload mints a new metadata row; the prior version flips to
   `state: archived` with a timestamp — an append-only, queryable version
   chain (`GET /input/v1/file/{id}`, list by state, plus
   `/input/v1/file/recent_changes`).
3. The bytes-leg response already carries the GCS **ETag**.
4. Client-supplied preconditions are ignored, confirmed by test:
   `x-goog-if-generation-match` (header) and `ifGenerationMatch` (query) on
   the upload leg both returned 200 against a deliberately wrong generation,
   and generation-0 (create-only) returned 200 against an existing object.
   GCS binds preconditions at session creation, which only the server does.

So the storage engine underneath already enforces exactly the semantics this
RFC asks for — today the API mints upload sessions without them.

## Proposed surface

The cheapest sufficient form, given the measured pipeline: **accept an
optional precondition (`if_generation_match`, or a current-version-UUID
match) in the `POST /input/v1/file` body and bind it at the creation of the
GCS resumable session** (GCS accepts `ifGenerationMatch` on JSON resumable
initiation and `x-goog-if-generation-match` on the XML initiation POST). The
contract at the Fulcra boundary: a stale precondition returns **412 without
yielding a usable upload session**. Integration work is small but real, not
zero: if the precondition is a Fulcra version UUID, the server resolves it
to the backing generation, and a rejected request must not leave a
falsely-current metadata row behind. Prefer generation semantics as the
primary write token (per GCS's own guidance); ETag remains useful response
evidence. In general terms:

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
4. **`If-Match: <token>` on delete.** Same semantics as upload: token
   current → file removed; stale → 412, file untouched. Without this, lease
   *release* stays racy even with atomic acquisition and renewal: a stale
   holder can read its old ownership, lose a race with a successor's
   acquisition, and then delete the successor's lease. Conditional delete
   closes the lease lifecycle end to end.
5. *(Optional, weaker)* honor `If-Unmodified-Since` with documented
   second-resolution caveats, for clients that only have timestamps.
6. **Capability discovery.** Advertise conditional-write support
   discoverably (API root document, `OPTIONS`, or a documented version
   floor) so clients can gate behavior without probing by side effect. Our
   engine ships a fail-closed transport gate today; it flips on when the
   capability is provable.

Backward compatible by construction: requests without the headers behave
exactly as today. No content-model change is required; the store already
versions files internally, as exposed by `stat` — the ask is to surface that
version as a precondition token on the write path.

## Consumers

One consumer is wired and waiting; the rest are candidates that need small
adapters (their current implementations do not yet speak preconditions —
claim scoped accordingly):

- **Wired, gated, day one:** coord-engine **cursor schema v2** (merged):
  stage/commit delivery with CAS loser-recovery behind a fail-closed
  transport gate that activates only when a proven conditional-write
  primitive exists. This RFC is that gate's missing half.
- **Candidates needing adapters:** the bridge **FileLease** (today:
  local-filesystem `O_EXCL`) and coord-engine **role leases** (today:
  unconditional transport write/delete) — with create-only acquisition,
  `If-Match` renewal, and conditional delete for release, both become
  end-to-end atomic once ported to store preconditions; **BridgeLedger**
  persistence (today: local tempfile + `os.replace`) similarly. Every
  future cursor, ledger, or checkpoint any agent keeps in the store.

(The prefs Privacy Ledger is deliberately absent: it is an annotation
record stream on the timeline, not a File Store document — conditional file
writes do not apply to it.)

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
