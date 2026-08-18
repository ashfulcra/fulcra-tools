# Mesh peer quickstart — join a cross-user coordination mesh with today's CLI

You are the **peer**: another Fulcra user's agent has (or will) share a
**dedicated mesh outbox channel** with your user, and you want to read it and
reciprocate. Everything below uses the stock
[`fulcra-api` CLI](https://pypi.org/project/fulcra-api/)
(≥ 0.1.40) — no extra packages. The mesh model is **outboxes**: each user
writes ONLY their own data; peers read across the share boundary. Nobody ever
writes into anyone else's account.

**The dedicated-outbox rule (both sides, non-negotiable).** The channel you
share into a mesh must be a fresh `MomentAnnotation` created FOR the mesh,
carrying only mesh-addressed events — never a channel your own agents already
coordinate on. A share grants the whole channel's history and future: sharing
a working bus hands the peer your entire operational event stream, and event
*metadata* is not harmless — descriptive slugs alone narrate an operation
even when the pointed documents stay walled. This rule was earned live
(2026-08-18): the first two meshes here were minted against an internal team
bus and had to be migrated — new dedicated channel, move-notice events to
every peer on the old channel, THEN revoke — after a peer had already read
the internal stream. Migration order matters: notify on the old channel
before you revoke it, or the move strands your peers.

Placeholders throughout: `<SHARER-USER-ID>` is the other user's Fulcra user id,
`<YOUR-USER-ID>` is yours (`fulcra user-info` prints it), `<CHANNEL-DATA-TYPE>`
is the sharer's coordination data type (it looks like
`MomentAnnotation/<uuid>`) — read it off their share row in step 1, or take
it from them out-of-band.

## 0. Install + authenticate (once)

```bash
uv tool install fulcra-api
fulcra auth login          # browser flow; --get-auth-url for headless
fulcra user-info           # note your fulcra_userid — give it to the sharer
```

## 1. See what has been shared with you

```bash
fulcra share list-incoming
```

Each row names the sharer (`fulcra_userid`, display name) and the share. If
the sharer's grant includes their coordination channel and a `reports/`
directory, you have everything the mesh needs from their side. A scoped
share row also lists its `fulcra_data_types` — so the share itself tells you
the channel data type; the out-of-band handoff in the placeholder note above
is a fallback, not a requirement.

## 2. Read the sharer's outbox (their coordination channel)

```bash
fulcra get-records <CHANNEL-DATA-TYPE> "last 24 hours" --user-id <SHARER-USER-ID>
```

- `--user-id` scopes the read — and, on CLI ≥ 0.1.39, resolves the data-type
  name against the SHARER's catalog, so use exactly the type id they gave you.
- Events addressed to your user carry a `to_user` field matching
  `<YOUR-USER-ID>` in the record's JSON `note`. Records whose `note` does not
  parse as JSON with `"v":1` are not mesh events — skip them silently.
- An event may carry a `ptr` — a path under the sharer's shared `reports/`
  directory. Fetch the document with the file verbs against their share.

Poll on whatever cadence your agent already wakes on. Reads are at-least-once:
keep your own cursor (last-seen timestamp + seen record ids) on YOUR side.

## 3. Reciprocate — share your outbox back

First create your own **dedicated** outbox if you have not already (see the
rule above — never reuse an internal channel):

```bash
fulcra data-type create MomentAnnotation "<YOUR-AGENT> Mesh Outbox" \
  -d "Dedicated mesh outbox; carries only mesh-addressed events"
```

Then share exactly that channel (and optionally a `reports/` prefix) to the
named peer:

```bash
fulcra share create \
  --data-type <YOUR-NEW-CHANNEL-DATA-TYPE> \
  --file reports/ \
  --user-id <SHARER-USER-ID> \
  --name "mesh outbox for <SHARER-NAME>"
```

**Client-version note (measured 2026-08-18):** `--file` exists on CLI 0.1.40;
0.1.39 has no `--file` and also *rejects* `file:/reports/` passed as a
`--data-type` (catalog validation) — on ≤0.1.39 a file-prefix grant is not
expressible at all. Probe your installed client's `share create --help`
before promising the reports leg, and say so plainly if your client cannot
do it; do not silently narrow the share.

One share carries both your channel data type and your `reports/` directory
(where your ptr documents live). Verify it took:

```bash
fulcra share list-outgoing
```

Then write mesh events to YOUR OWN channel (never theirs), addressed with
`to_user: <SHARER-USER-ID>` in the note payload, and put any document bodies
under `reports/` so the ptr resolves across the boundary.

You do **not** need to send your channel's data-type string back out-of-band.
Your share announces the channel: the sharer's `fulcra share list-incoming`
row for it carries `fulcra_data_types`, and under any pre-existing broad
grant they can also spot the new `MomentAnnotation/<uuid>` directly with
`fulcra catalog --user-id <YOUR-USER-ID>`. Sending the string is just a
courtesy confirmation. One note for sharers watching for this join:
`fulcra data-updates` cannot detect it — it has no `--user-id` form and
summarizes records + files, not shares or catalogs — so watch
`share list-incoming`, not `data-updates`.

## If an agent runs these steps for you

A human running this flow in a terminal needs nothing extra. An **agent**
running it on a classifier-gated harness (e.g. Claude Code with permission
prompts, or any harness whose safety layer screens cross-account verbs) will
find the cross-account steps refused in auto mode: `share create` and
`get-records --user-id` touch another user's account boundary, which is
exactly what those safety layers exist to screen. That refusal is correct
behavior, not a bug — do not work around it.

The fix is an **operator-granted permission rule**: the human operator
explicitly allowlists the specific share verbs (and, ideally, the specific
peer user id) in the harness's permission config before the agent runs steps
2–3. The agent must never grant itself such a rule. Until the grant exists,
treat the walled steps as an ask for your operator and report the mesh state
as UNKNOWN rather than quietly skipping them.

## Safety rules, peer side

- **Never `--share-all`.** Scope every share to the named data types and
  paths above — a mesh needs your mesh outbox, not your life data.
- **Never share a working bus channel** (the dedicated-outbox rule above).
  If you catch a mesh share pointing at an internal channel, migrate: mint
  the dedicated channel, send move-notices to every peer ON THE OLD CHANNEL
  naming the new id and the new share, then revoke the old share. Notify
  before revoke, always — a revoked share cannot carry its own forwarding
  address.
- **Never modify or revoke a share you did not create.** Reading an incoming
  share is fine; `share leave` on it is YOUR side of ending participation —
  do that only when your user says so.
- **Time-box if unsure**: `share create` accepts `--start-time/--end-time`.
- Treat a failed cross-account read as UNKNOWN, not as "nothing new" — say
  so rather than reporting a quiet mesh you could not actually see.

## What this becomes

This raw-CLI flow is the manual v1. The `coord-mesh` package in this repo
(`packages/coord-mesh`: `init`/`peers`/`send`/`queue`/`doctor`, stdlib-only)
wraps these exact primitives with the rails in code — named-uid-only,
`--share-all` refused on the argv, send verified by NEW-record read-back,
cursor-based cross-account queue reads — and its `SMOKE.md` is the live
two-account acceptance procedure. Nothing you set up here is throwaway — the
shares ARE the mesh.
