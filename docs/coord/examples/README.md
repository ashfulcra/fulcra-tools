# Coord example fixtures

Documents that live in a team's File Store, kept here as reference copies. They
are **examples, not the live bytes** — nothing here is read at runtime. Upload a
copy (edited for your account's real ids) to the path each one names.

## `bus-v3-tags.json` → `team/<team>/_coord/bus-v3/tags.json`

The tag registry the engine reads to tag every bus write. Schema
`coord.bus-tags.v2`:

| field | meaning |
| --- | --- |
| `schema` | exactly `coord.bus-tags.v2`. Any other value — including the identity-only `coord.bus-tags.v1` that preceded it — is INVALID. The engine will not guess at an unknown shape, and will not migrate one. |
| `base` | uuid of the channel tag. Attached to **every** event, so a timeline filter on it is the whole bus. |
| `agents` | `{"<agent name>": {<dimension>: "<tag uuid>"}}`. May be `{}` in a freshly seeded registry. |

### The four dimensions

Each is one tag, so each is one timeline filter:

| dimension | tag name convention | example |
| --- | --- | --- |
| `agent` | `agent:<name>` | `agent:coord-boss` |
| `platform` | `platform:<name>` | `platform:claude-code` |
| `harness` | `harness:<name>` | `harness:ccr` |
| `model` | `model:<name>` | `model:opus-5` |

`agent` is **required** in any registered entry — an entry that cannot say who
sent the event has no reason to exist. The other three are optional and may be
filled in later; an entry carrying only some of them is a legitimate *partial*
entry, and its events are tagged with what it has, silently.

`model` is **declared, not detected**: no engine can see which model drives it.
A stale declaration is a presence-integrity bug, and the fix is cheap — re-run
`tag-provision --model <new>`, which rewrites that dimension and leaves the
other three alone.

Every value is a tag **uuid**: the ingest endpoint validates record tags as
uuids and rejects names. Get them from `GET /user/v1alpha1/tag`, or let
`coord-engine bus-v3 tag-provision <team> --agent <name> --platform <p>
--harness <h> --model <m>` create and record them.

### About the values in the fixture

`base` and the coordinator's `agent` uuid are **placeholder-shaped** values in
the fixture; a team's real channel and agent uuids live on its own coordination
store (resolve them from `records.json` at run time — a uuid copied into a repo
fixture is a future silent failure).

The `platform`/`harness`/`model` values are **placeholders, and deliberately
not well-formed uuids** — uploading this file unedited makes the registry
INVALID and loud, rather than tagging real events with fiction. The live seed
comes from the operator at cutover, once those tags exist on the account.

### The states, and what a write does in each

None of them may ever cost a write — see
[`packages/coord-engine/coord_engine/bus_tags.py`](../../../packages/coord-engine/coord_engine/bus_tags.py):

- **absent** — team has not adopted tagging. Untagged writes, silently.
- **sender missing from `agents`** — base tag only, plus a one-line warning
  naming `tag-provision`. Never silent, never a failed write.
- **sender partial** — the dimensions it has, plus base. No warning: a partial
  entry is a deliberate state, and nagging would bury the warning that matters.
- **malformed** — loud on every write, untagged, and **never auto-recreated**.
  A human fixes the bytes; the engine will not overwrite the evidence.
