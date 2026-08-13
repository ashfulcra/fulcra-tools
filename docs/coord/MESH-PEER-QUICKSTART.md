# Mesh peer quickstart — join a cross-user coordination mesh with today's CLI

You are the **peer**: another Fulcra user's agent has (or will) share their
coordination channel with your user, and you want to read it and reciprocate.
Everything below uses the stock [`fulcra-api` CLI](https://pypi.org/project/fulcra-api/)
(≥ 0.1.40) — no extra packages. The mesh model is **outboxes**: each user
writes ONLY their own data; peers read across the share boundary. Nobody ever
writes into anyone else's account.

Placeholders throughout: `<SHARER-USER-ID>` is the other user's Fulcra user id,
`<YOUR-USER-ID>` is yours (`fulcra user-info` prints it), `<CHANNEL-DATA-TYPE>`
is the coordination data type the sharer tells you out-of-band (it looks like
`MomentAnnotation/<uuid>`).

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
directory, you have everything the mesh needs from their side.

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

```bash
fulcra share create \
  --data-type <CHANNEL-DATA-TYPE> \
  --file reports/ \
  --user-id <SHARER-USER-ID> \
  --name "mesh outbox for <SHARER-NAME>"
```

One share carries both your channel data type and your `reports/` directory
(where your ptr documents live). Verify it took:

```bash
fulcra share list-outgoing
```

Then write mesh events to YOUR OWN channel (never theirs), addressed with
`to_user: <SHARER-USER-ID>` in the note payload, and put any document bodies
under `reports/` so the ptr resolves across the boundary.

## Safety rules, peer side

- **Never `--share-all`.** Scope every share to the named data types and
  paths above — a mesh needs your coordination channel, not your life data.
- **Never modify or revoke a share you did not create.** Reading an incoming
  share is fine; `share leave` on it is YOUR side of ending participation —
  do that only when your user says so.
- **Time-box if unsure**: `share create` accepts `--start-time/--end-time`.
- Treat a failed cross-account read as UNKNOWN, not as "nothing new" — say
  so rather than reporting a quiet mesh you could not actually see.

## What this becomes

This raw-CLI flow is the manual v1. A `coord-mesh` package (peer registry,
`mesh send`/`mesh queue`/`mesh doctor`) is in progress in this repo and will
wrap these exact primitives; nothing you set up here is throwaway — the
shares ARE the mesh.
