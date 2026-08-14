# Atomic Identity Claim, Queue-Visible Reviews, and Agent File Transfer

**Date:** 2026-08-14
**Status:** Proposed
**Scope:** `packages/coord-engine`

## Problem

Two independent gaps can make coordination work appear complete while leaving an
agent unreachable.

First, activating an identity currently requires several unrelated commands:

1. establish the role in the team store;
2. provision timeline tags;
3. claim the role lease;
4. write presence;
5. announce a Bus claim;
6. prove delivery.

The engine accepts `--from <identity>` before those steps are complete. When the
team has a tag registry but the sender has no entry, the write succeeds with only
the channel tag and a warning. The event remains routable, but the operator can
mistake a partially activated identity for a fully claimed one.

Second, `review request` writes a durable review register and one canonical task
document per reviewer, but its reviewer path calls `_write_directive` directly.
Unlike `tell`, it does not call `_emit_dispatch_companion`. The reviewer can find
the task through a reconciled inbox or `needs-me`, but a normal Bus V3 queue read
has no `v:1` event to deliver. The command's success text therefore overstates the
notification guarantee.

These are separate defects. Identity completeness concerns the sender. Review
delivery concerns the recipient. The implementation should fix both without
coupling team topology to the public repository.

Coord also lacks a standard way for one agent to hand files to another. Agents
can upload arbitrary paths to the Fulcra File Store and can put `ptr` in a Bus
event, but there is no convention for exact bytes, filenames, multiple files,
integrity checks, acknowledgement, or partial upload recovery. Ad hoc transfers
therefore risk stale pointers, overwritten bytes, and messages that look complete
before their payload is durable.

## Goals

- Provide one idempotent command that activates and proves an existing Bus-owned
  identity.
- Refuse to call an identity claimed when any required layer is absent or
  unverified.
- Make every fresh review-request directive visible through the reviewer's Bus
  V3 queue.
- Preserve the review register as the authoritative merge gate.
- Preserve the task document as the durable directive body.
- Keep team-specific identities, roles, machines, clouds, and mappings in the
  team store rather than this repository.
- Return structured evidence that automation can verify.
- Define a content-addressed, pointer-backed agent-to-agent file transfer
  convention over the Fulcra File Store and Bus V3.
- Make transfer publication atomic from the recipient's perspective: incomplete
  uploads never receive a Bus event.

## Non-Goals

- Automatically create role documents or choose team policy.
- Infer platform, harness, or model declarations.
- Replace `needs-me`, reconcile, or durable task documents with transient events.
- Make file-store writes and annotation writes transactionally atomic.
- Change the Bus V3 envelope schema.
- Add a resident listener or scheduled tick.
- Store credentials or secrets in transfer payloads or manifests.
- Turn the Bus event stream itself into a binary transport.

## Evidence and Root Cause

### Identity activation

The write path intentionally permits a missing sender tag. `bus_tags.tags_for`
prints a warning and returns the channel tag, allowing `records.emit_event` to
succeed. This is useful for backward compatibility, but it means event success is
not evidence that the sender holds a role, has presence, or announced a claim.

The role, tag, presence, and Bus claim verbs each work independently. No command
checks the full conjunction. Documentation describes the pieces, but there is no
machine-checkable terminal state equivalent to `CLAIMED`.

### Review delivery

`_create_directive` follows this sequence:

1. `_write_directive` verifies a fresh task write;
2. `_emit_dispatch_companion` emits a pointer-backed `v:1` Bus event.

`_deliver_review_directive` performs step 1 only. Existing tests prove that the
task appears after reconcile, but none proves that a raw Bus queue contains the
review request. The missing test mirrors the missing product behavior.

## Considered Approaches

### A. Documentation-only repair

Update bootstrap prose to list the commands in the correct order and tell review
requesters to send a second manual event.

This is low effort but preserves both failure modes. Agents can still skip a
claim layer, and review delivery remains two user actions with no shared identity
or recovery contract. Rejected.

### B. Make all untagged writes fail

Change `records.emit_event` to reject any sender absent from `tags.json`.

This is attractive but too broad for the first change. It would alter every event
producer, break bootstrap claims for unregistered identities, and make timeline
metadata availability a hard dependency of durable task delivery. It also does
not prove the role lease or presence. Deferred.

### C. Composite claim plus shared directive delivery

Add an explicit identity activation verb and route both ordinary and review
directives through one delivery function. This addresses each root cause at its
own boundary while retaining backward-compatible low-level verbs.

**Recommended.**

## Design

### 1. `coord-engine identity claim`

Add:

```text
coord-engine identity claim <team> <identity> \
  --platform <platform> --harness <harness> --model <model> \
  [--summary <text>] [--json]
```

The identity name is also the role name. The command does not create the role.
The role document is team topology and must already exist at:

```text
team/<team>/roles/<identity>.md
```

The command executes four phases.

#### Phase 1: read-only preflight

- Resolve the current Bus channel authority.
- Verify the engine is current enough to support the command.
- Read and parse the role document.
- Fold the role and refuse `CONTESTED` or `UNKNOWN`.
- If held by another identity, refuse takeover.
- Read the tag registry and verify it is valid.
- Validate all declarations before making any write.

No write occurs unless every preflight passes.

#### Phase 2: identity metadata

- Provision or refresh `agent`, `platform`, `harness`, and `model` tags.
- Require all four dimensions after provisioning. Partial registration is not a
  successful claim.

Declarations remain explicit. The engine must not infer model or harness.

#### Phase 3: role and liveness

- Claim or refresh the role lease using the existing nonce collision guard.
- Write presence for the identity, including the optional summary.
- Use session engagement when an explicit expiry is provided; otherwise preserve
  the existing presence default.

Every write must be read back or otherwise use the existing verified-write
contract. A failed write returns nonzero and names the completed phases.

#### Phase 4: Bus claim and proof

- Send a tagged `claim` event to the team's coordination owner using slug
  `on-bus-v3-<identity>`.
- Run the delivery proof against the claimed identity.
- Print `CLAIMED` only when the event is proven on the current channel with all
  registered tags.

The text result names each layer. `--json` returns:

```json
{
  "state": "CLAIMED",
  "identity": "example-reviewer",
  "role": "HELD",
  "tags": "COMPLETE",
  "presence": "CURRENT",
  "claim_event": "PROVEN",
  "partial_writes": []
}
```

Terminal states are `CLAIMED`, `BLOCKED`, and `DEGRADED`. `BLOCKED` means a
known policy or argument failure. `DEGRADED` means the engine cannot prove a
required read or write. Neither is success.

The existing low-level commands remain available for repair and expert use.

### 2. One directive delivery primitive

Introduce an internal primitive that owns the complete delivery sequence:

```text
deliver_directive(
  durable task identity,
  recipient,
  sender,
  priority,
  event mode,
) -> delivery result
```

It must:

1. verify or deduplicate the canonical task document;
2. emit a pointer-backed Bus V3 event when the document is freshly written;
3. report file-only degradation explicitly;
4. expose whether the result was `written`, `deduped`, `signaled`, or `failed`.

`tell`, `broadcast`, and reviewer notification call this primitive. Callers no
longer remember whether they must invoke `_emit_dispatch_companion` separately.

For the first implementation, preserve current retry semantics: only a fresh task
write emits a companion. A later design may add durable delivery receipts for
safe re-notification, but that is not required to close the current fresh-review
gap.

### 3. Review request behavior

After the review register lands, each required reviewer is delivered through the
shared primitive with:

- `kind=directive`;
- a P1 priority by default;
- a pointer to the canonical reviewer task;
- the exact review slug, artifact, verdict path, and active head in the task
  body.

The request returns nonzero if any reviewer task fails to write. If the task
writes but the event cannot be emitted, the command reports `FILE_ONLY` for that
reviewer and returns nonzero while preserving the authoritative register and task
documents. The recovery instructions name `needs-me` and an explicit re-notify
path; they do not claim queue delivery.

The review tally remains unchanged. An event is a delivery hint, never approval
evidence.

### 4. Agent-to-agent file transfer

Use the Fulcra File Store as the data plane and Bus V3 as the notification plane.
The Bus carries no file bytes. Its existing `ptr` field points to one immutable,
content-addressed transfer manifest.

#### 4.1 Store layout

Every transfer has a sender-generated UUID and lives under:

```text
team/<team>/_coord/transfers/<transfer-id>/
  payload/<sha256>-<safe-filename>
  manifest-<manifest-sha256>.json
  receipts/<recipient-key>-<receipt-sha256>.json
```

Payload and manifest paths are write-once by convention. A sender must refuse to
publish if any target path already exists with different bytes. Content hashes in
filenames make accidental overwrite and stale-path reuse visible.

`safe-filename` is a sanitized basename for display only. Identity comes from the
SHA-256 digest, never from the original local path. Local absolute paths are not
stored.

#### 4.2 Manifest schema

The manifest is UTF-8 JSON with schema `coord.file-transfer.v1`:

```json
{
  "schema": "coord.file-transfer.v1",
  "transfer_id": "018f4f4e-0000-7000-8000-000000000000",
  "from": "build-agent",
  "to": "review-agent",
  "created_at": "2026-08-14T15:00:00Z",
  "purpose": "Review the generated compatibility evidence",
  "files": [
    {
      "name": "evidence.zip",
      "path": "_coord/transfers/018f4f4e-0000-7000-8000-000000000000/payload/0123abcd-evidence.zip",
      "sha256": "0123abcd...",
      "size": 1048576,
      "media_type": "application/zip"
    }
  ],
  "retention": "team-default"
}
```

Required fields are `schema`, `transfer_id`, `from`, `to`, `created_at`,
`purpose`, and a non-empty `files` list. Each file requires `name`, team-relative
`path`, full lowercase SHA-256, byte `size`, and `media_type`.

The manifest filename contains the SHA-256 of the canonical JSON bytes. The
receiver hashes the downloaded manifest before trusting any field.

#### 4.3 Publication sequence

`coord-engine transfer send <team> <recipient> <path>...` performs:

1. preflight identity, readable local files, safe names, sizes, and media types;
2. compute every payload hash and the complete manifest locally;
3. upload each content-addressed payload;
4. read back or download each payload and verify size and SHA-256;
5. upload the content-addressed manifest last and verify its hash;
6. emit one pointer-backed Bus `directive` event to the recipient.

The event uses:

```json
{
  "v": 1,
  "to": "review-agent",
  "kind": "directive",
  "pri": "P2",
  "slug": "file-transfer-018f4f4e-0000-7000-8000-000000000000",
  "ptr": "_coord/transfers/018f4f4e-0000-7000-8000-000000000000/manifest-<sha256>.json"
}
```

The manifest is the publication boundary. Payloads uploaded before a failed
manifest write are unreachable staging artifacts, not delivered transfers. A Bus
event is emitted only after the complete manifest and every payload are proven.

#### 4.4 Receipt sequence

`coord-engine transfer receive <team> <transfer-id> --dest <directory>`:

1. resolves the event pointer without advancing another identity's cursor;
2. verifies manifest path, hash, schema, sender, and intended recipient;
3. rejects absolute paths, `..`, duplicate output names, and hash/size mismatch;
4. downloads into a temporary directory;
5. verifies every completed file before atomically moving it into the destination;
6. writes a content-addressed receipt and sends a pointer-backed Bus `response`
   event to the sender.

Receipt schema `coord.file-transfer-receipt.v1` records `transfer_id`, sender,
recipient, manifest hash, status (`accepted` or `rejected`), verified file hashes,
timestamp, and a human-readable reason. It never records the recipient's local
absolute path.

An acknowledgement is evidence of integrity and acceptance, not evidence that a
subsequent task using the files is complete.

#### 4.5 Security and limits

- Existing Fulcra account and sharing permissions remain authoritative.
- Transfers inherit the Bus rule forbidding secrets, credentials, and tokens.
- Symlinks, device files, sockets, and directories are rejected. Callers archive
  a directory explicitly when that is the intended payload.
- A configurable per-file and total-size limit fails before upload. Defaults are
  conservative and printed in `--help`.
- Media type is advisory; hashes and sizes are authoritative.
- Receivers never execute, import, or open transferred files as part of receipt.
- Retention and garbage collection are separate policy. V1 records a retention
  class but does not silently delete payloads.

#### 4.6 Idempotency and recovery

Re-running `transfer send` with the same transfer ID and identical bytes verifies
the existing payloads and manifest. Notification is at-least-once: a recovery may
emit a duplicate Bus event because the File Store and annotation write are not one
transaction. Receivers deduplicate by `transfer_id` plus manifest hash. Different
bytes under an existing transfer ID fail closed.

If the event write fails after manifest publication, the command returns
`FILE_READY_EVENT_FAILED` and prints an exact `bus-v3 send` recovery command using
the immutable manifest pointer. It never re-uploads or invents a new transfer ID.

### 5. Guardrails on `--from`

Do not globally reject unregistered senders in this change. Instead:

- `identity claim` is the only command allowed to print `CLAIMED`;
- Bus write warnings use the explicit phrase `UNCLAIMED IDENTITY` when the team
  registry exists but the sender is absent;
- `bus-v3 send --strict-identity` returns nonzero instead of emitting when the
  sender lacks a complete registration;
- high-level automation uses strict mode after migration.

This introduces a safe path without breaking repair and bootstrap workflows.

## Failure Semantics

- An unreadable authority is `DEGRADED`, never absent.
- A malformed role or tag registry is `BLOCKED`; the command never rewrites it.
- A contested exclusive role is `BLOCKED` before any write.
- A partial claim never prints `CLAIMED`.
- A review register can remain durable when notification degrades, but the
  command returns nonzero and names each undelivered reviewer.
- Queue event failure never deletes the task or review register.
- Event success never substitutes for exact-head verdict evidence.

## Tests

### Identity claim

- Missing role document produces `BLOCKED` and zero writes.
- `CONTESTED` and `UNKNOWN` role folds produce zero writes.
- Invalid declarations produce zero writes.
- A complete first claim writes tags, lease, presence, and one tagged claim.
- Repeating the same claim is idempotent and refreshes lease/presence without
  creating duplicate role shards.
- A failure in each phase returns nonzero and reports prior completed phases.
- JSON output never reports `CLAIMED` unless all four layers are proven.
- Strict send rejects an incomplete identity; compatibility mode warns.

### Review delivery

- A fresh review request emits one `v:1` directive event per required reviewer.
- Every event points to the canonical reviewer task.
- Every event carries the requested exact head indirectly through that task.
- Multiple reviewers receive distinct recipient events.
- A failed register write emits no tasks and no events.
- A failed task write emits no event for that reviewer and returns nonzero.
- An event failure preserves the register/task, returns nonzero, and prints
  `FILE_ONLY`.
- An idempotent same-head request does not emit duplicate events.
- Raw queue filtering accepts the emitted event and routes it only to the named
  reviewer or `all`.

### File transfer

- One and multiple-file transfers produce canonical manifests and stable paths.
- Payload corruption, truncation, or manifest hash mismatch fails before receipt.
- A payload or manifest upload failure emits no Bus event.
- An event failure preserves the proven manifest and prints a deterministic
  recovery command.
- Re-sending identical content is idempotent; changed content under the same
  transfer ID fails closed.
- Path traversal, absolute paths, symlinks, duplicate names, and oversize payloads
  are rejected before destination mutation.
- Receive stages in a temporary directory and moves files only after all hashes
  pass.
- Accepted and rejected receipts are pointer-backed Bus responses to the sender.
- Manifests and receipts contain no local absolute paths or team-specific policy.

## Rollout

1. Land the shared directive primitive and review queue tests.
2. Land the file-transfer manifest/receipt schemas and `transfer send/receive`
   verbs; this is independent of identity-claim migration and can ship next.
3. Land `identity claim` and structured status tests.
4. Update generic bootstrap docs to prefer `identity claim` and document transfer
   recovery.
5. Keep existing low-level commands documented as repair primitives.
6. Migrate fleet automation to `--strict-identity` only after every active
   identity has a complete Bus-owned registration.

Team-specific role documents, machine mappings, movement history, and migration
status remain on the Bus throughout rollout.

## Acceptance Criteria

- A newly claimed identity has one command whose rc and structured output prove
  role, tags, presence, and Bus claim.
- A fresh `review request` is visible in the required reviewer's Bus V3 queue
  without a second manual message.
- The queue event points to the same durable task surfaced by `needs-me`.
- An agent can send one or more files by publishing verified File Store payloads
  and one content-addressed manifest pointer, and the recipient can verify and
  acknowledge them without trusting filenames or local paths.
- Exact-head approval remains the only merge-discharge evidence.
- No team-specific identity or machine mapping is added to the repository.
