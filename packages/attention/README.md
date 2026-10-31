# fulcra-attention

Capture foreground browsing, with page titles and time-on-page, into your own
[Fulcra](https://fulcradynamics.com) account. The useful question is fairly
ordinary: *what was that article I read on Tuesday?* Capture depends on browser
permissions, activity detection, and your privacy settings; this is not a record
of every page you've ever opened. It belongs to the [unsupported monorepo](../../README.md).

The capture pipeline is **fully relayless**: the Chrome extension signs in through your browser with an Auth0 device flow and POSTs records **directly to the Fulcra API** (`https://api.fulcradynamics.com/ingest/v1/record/batch`). There is no localhost daemon involvement, no pairing, no per-extension token, and no relay route. The Python package in this repo is now just the Fulcra Collect *pointer* plugin — a static signpost that tells the user to install the browser extension and sign in.

## Install with the Collect download

The [Collect disk image](../../docs/collect.md#get-started-new-user) includes an
optional Chrome Attention extension folder. Follow its included instructions to
load it into Chrome, then sign in from the extension. Collect's Attention entry
points to that setup; installing the Mac app alone does not start browser capture.
The extension works independently of the Collect daemon.

For a source build, see the [Chrome README](chrome/README.md).

## Package contents

This package holds:

- **`fulcra_attention/`** — the Fulcra Collect pointer plugin (`collect_plugin.py`). It does no collection: its description and Run Now receipt give installer users the steps for loading the extension folder from the disk image and signing in through the browser. No credentials, setup wizard, or definition binding.
- **`chrome/`** — Chrome MV3 extension. Foreground-only capture, optional sharper-AFK content script, onboarding wizard, right-click context menu, branded UI. This is where all the real work happens — sign-in, definition resolution, and direct-to-Fulcra ingest. See [chrome/README.md](chrome/README.md) for build + load instructions and [local style resources](chrome/README.md#local-style-resources-development).
- **[`safari/`](safari/)** — macOS and iOS containing apps and Safari extensions,
  sharing the browser capture code with native authentication and device identity.

## Setup

Setup happens entirely in the browser extension. Collect only shows the loading
instructions in the plugin description and its **Run Now** receipt.

With the Collect download, reopen `Fulcra-Collect-macOS-arm64.dmg`. Open
`chrome://extensions`, turn on **Developer mode**, choose **Load unpacked**, and
select the **Fulcra Attention Extension** folder in the disk image.

For a source checkout, run `npm ci && npm run build` in [`chrome/`](chrome/),
then load `chrome/dist/` instead.

Open the extension and click **Connect to Fulcra**. Approve the browser sign-in
page, choose the Fulcra Attention definition to use or create a new one, and
name this browser. Finish the wizard.

From then on the extension captures and ingests on its own, straight to the Fulcra API.

## Architecture

- **Relayless, direct-to-cloud.** The extension POSTs batches to `https://api.fulcradynamics.com/ingest/v1/record/batch` with a Bearer token obtained from its own Auth0 device-flow sign-in. No daemon, no loopback endpoint, no pairing handshake. See `chrome/src/relayless/` (`oidc.ts`, `signIn.ts`, `relaylessSender.ts`, `ensureDefinition.ts`, `wire.ts`, `config.ts`).
- **Per-device identity.** Every install carries an identity slug, and it is obtained two different ways depending on the browser. **Chrome** asks: onboarding will not continue until you name the browser, and that label both slugifies into a `machine:<slug>` tag and folds into the source_id. It is prefilled from the signed-in email (`<email> browser`) and editable in the wizard / popup. **Safari** does not ask: the containing app mints an automatic per-installation identity on first use and stores it in the shared App Group, because Safari has no onboarding wizard and iOS will not hand an app the user's device name without a specially-granted entitlement. Naming a Safari device is therefore *optional and additive* — the distinctness below holds with or without a name; a name only adds the readable `machine:<slug>` tag.
- **Each accepted event** becomes one `DurationAnnotation` under the resolved `Attention` definition, tagged `attention` + `web` (plus the `machine:<slug>` tag when a human label is set — so Chrome records carry it, and Safari records carry it only once you name that device).
- **Source-id namespace.** `com.fulcra.attention.v3.<sha256(scrubbed_key|start_time_second|identitySlug)[:16]>`. Different identity slugs make the same URL and second produce distinct source IDs; Chrome labels must therefore resolve to distinct slugs. Dedup is server-side on source_id; the extension also keeps a client-side sent-set to avoid re-POSTing.

  > **Identity needs care.** Safari's automatic identity lives in the App Group
  > container; deleting that data and reinstalling creates a new identity. A name
  > makes the device readable to a human but does not restore the old source IDs.
  > Chrome derives its slug from your chosen label, lowercases it, replaces
  > separators, and truncates it. Use distinct labels that differ near the start:
  > two labels that collapse to the same slug can collide on the same URL and second.

Three-tier privacy posture (Tier 1 always-on, Tiers 2 + 3 user-driven from the extension popup):

| Tier | Action | Default |
|---|---|---|
| **1 — Param strip** | Remove ~80 auth/tracking params | Always on |
| **2 — Categorize** | Replace URL/title with category slug (e.g. `banking`) | Empty by default |
| **3 — Ignore** | Drop event entirely | Empty by default |

## Multi-machine + multi-identity

Each browser signs in independently and carries an identity slug. Distinct slugs keep records from different installs distinguishable; use distinct Chrome labels as described above. Chrome additionally carries a `machine:<slug>` tag, which is what makes them distinguishable *by eye* at query time; a Safari device gets that tag once you name it, and until then it is distinct in the data but shows up unnamed. The extension's user-managed ignore list propagates across Chrome profiles via Chrome sync (`chrome.storage.sync`).

Users with multiple Chrome profiles (one per company / client / personal) have their `chrome_identity` carried through to `external_ids` on every annotation, so you can group by `external_ids.chrome_identity` at query time. The identity is captured from `chrome.identity.getProfileUserInfo()` (the Google account email signed into that Chrome profile) or a free-text user-set label.

## Development

```bash
# From the repository root after workspace setup:
uv run --package fulcra-attention --extra dev pytest packages/attention/tests/ -q
```

The browser extension is built and tested under [`chrome/`](chrome/) — see [chrome/README.md](chrome/README.md).

### Safari (macOS + iOS)

The Safari app and extension live under [`safari/`](safari/). Four Xcode targets
(app + extension, each for macOS and iOS) are built on every change by
[`.github/workflows/xcode.yml`](../../.github/workflows/xcode.yml), which also
runs the Swift test suite:

```bash
# Build BOTH extension bundles first. The test scheme builds the macOS app,
# which embeds the extension, which copies a built bundle as resources — so
# from a clean checkout the test fails on missing files that have nothing to
# do with the tests themselves.
cd packages/attention/chrome
npm ci && npm run build && npx vite build --config vite.safari.config.ts

cd ../safari/FulcraAttention
xcodebuild -project FulcraAttention.xcodeproj \
  -scheme FulcraAttentionTests -destination 'platform=macOS' test
```

`FulcraAttentionTests` is hosted by the macOS app, so `@testable import
FulcraAttention` reaches the app module. Platform-agnostic logic therefore
belongs in the **macOS app target** even when only the extension uses it at
runtime — that is what makes it testable.

**Shipping to TestFlight:** `safari/scripts/release_testflight.sh` does the whole
mechanical path — builds both JS bundles, archives, verifies the archive really
contains the embedded extension *with* its bundle inside, exports for App Store
Connect, validates, and uploads. Run it with `--dry-run` to exercise everything
except the upload.

The script checks these release prerequisites before building:

| Needed | What the release runner needs |
|---|---|
| **Apple Distribution** certificate | Available with its private key to the release runner. Developer ID Application is for direct distribution; it cannot sign an App Store build. |
| App record for `com.fulcra.attention` | Created once by hand in App Store Connect. |
| App Store Connect API key (`.p8`) | Downloadable exactly once, by the account holder. Pass via `ASC_KEY_ID` / `ASC_ISSUER_ID`. |

The build number (`CURRENT_PROJECT_VERSION`) must increase on every upload —
App Store Connect rejects a repeat.

## Status

### v1 contract development

The installed/default Chrome and Safari senders still use the legacy transport.
An opt-in [schema contract](fulcra_attention/v1_contract.json),
[Python builder](fulcra_attention/v1.py), [TypeScript encoder](chrome/src/relayless/v1Wire.ts)
and [Swift encoder](<safari/FulcraAttention/macOS (App)/V1Wire.swift>) now declare
flat v1 Events under the owned `collect.attention.v1` spec. The schema ships in
the Python wheel and is imported directly by TypeScript; its initial fingerprint
is pinned by a test. Changes to this published semantic spec need a new version,
rather than silently changing an existing creation intent.

The [shared synthetic vectors](tests/fixtures/v1_wire_cases.json) cover prepared URLs,
categories, nulls, Unicode, offsets, pre-epoch time and distinct
installations/destinations. The existing `com.fulcra.attention.v3.` primary
source algorithm remains unchanged. URL events enter the new encoder **after JS
capture scrubbing**, with the exact prepared URL frozen as the source key. The
initial common boundary accepts ASCII DNS/IPv4 hosts, canonical nondefault ports,
RFC3986 paths and scrubbed form queries. It refuses inputs that need another URL
normalization (default ports, host case, dot segments), malformed encodings,
fragments and unsanitized queries; IPv6 literals and other unsupported URL shapes
also remain held. A caller must retain the original queued input on refusal and
must not invent a replacement identity. The installed legacy helpers are unchanged;
raw/history records keep their stored primary source verbatim. Record UUIDs use the common URL-namespace
UUID5 mapping over `fulcra-tools:v1:{full destination}:{primary source}`.
New captures retain their millisecond bounds and actual fractional duration;
the primary source still uses its original whole-second time. Invalid calendar
dates, missing/invalid offsets, reversed bounds and nonzero submillisecond input
fail rather than normalize silently. Historical conversion preserves the
precision actually stored; a new encoder cannot recover discarded milliseconds.
The schema permits a historical null device, while new capture contexts require
a nonempty installation slug. TypeScript snapshots every input before async
hashing; Python snapshots tag/source iterables before validation.

The opt-in [browser journal](chrome/src/relayless/v1Journal.ts) adds a persisted
account/generation gate and Event datatype-creation intents in IndexedDB.
Transitions use transactional compare-and-set across extension contexts, require
a new never-reused generation, and retain an explicit disabled signed-out state.
Creation reservations publish with strict transaction durability before a future
resolver may POST; an unknown result stays pending across restarts and cannot
silently authorize another creation. Reconciliation under a fresh generation of
the same account can resolve that original intent to one destination. Schema or
destination substitution, malformed durable state and capacity exhaustion fail
loudly. This journal verifies no API principal and grants no upload authority;
its caller must establish current account/consent and prove the catalog/schema
before resolution. Existing auth, raw queues and default senders do not call it.
Tests use a pinned development-only IndexedDB implementation; they do not prove
actual browser disk persistence, quota handling or signed Safari storage sharing.

The v2 database upgrade preserves those v1 stores and adds durable tag intents.
The opt-in [tag resolver](chrome/src/relayless/v1Tags.ts) freezes the original
label, uses Python-compatible short alias names, and reserves a unique
account/create-name before its one creation attempt. Only a target lookup's
404 permits creation; a principal-endpoint failure cannot supply that evidence.
A returned UUID is persisted as submitted before the required name readback,
so a failed readback/restart cannot silently substitute another UUID. Unknown
POST results remain pending and may reconcile a positive lookup, never create
again. Resolved account/label UUID bindings survive cloud renames; they record
historical ownership, not current liveness or upload authority. Name collisions,
unreadable state and the 1,000-intent capacity refuse without eviction. Labels
are capped at 1,024 Unicode codepoints; new names at 30. The
browser resolver explicitly refuses `.` and `..` labels, whose lookup paths
would normalize as dot segments. The
[synthetic tests](chrome/tests/relayless/v1Tags.test.ts) cover v1 upgrades,
concurrent reservations, quota rollback, strict durability and lost responses.
No default sender or browser auth path calls this resolver yet.

The opt-in [v1 HTTP boundary](chrome/src/relayless/v1Http.ts) captures a canonical
account/generation scope and request JSON before awaiting. It verifies the token
candidate UUID using the API's current-user response before a v1 request, then
checks current token/gate around provider and body reads. Same-account renewal
can supply a new bearer; account/generation/consent changes refuse a late result.
It writes no credentials and performs no refresh or automatic mutation retry.
Its future auth caller must publish a changed gate before changing capture
account. Requests use allowlisted production routes, no cookies/redirects and a
bounded abort timer plus absolute monotonic deadline checks, including around
synchronous callbacks; invalid/lossy JSON and oversized retained bodies fail closed.
**Browser fetch exposes decoded bytes. This cap does not bound raw-wire bytes or
browser-engine decompression allocation.** It supplies no catalog/schema,
creation, exact-delivery or replay-exclusion proof and has no default caller.
The [synthetic transport tests](chrome/tests/relayless/v1Http.test.ts) do not prove
actual browser auth/CORS behavior or a production sender upgrade.

The opt-in [owned custom-type reader](chrome/src/relayless/v1OwnedTypes.ts)
uses the [direct input-service authority](../fulcra-common/README.md#v1-datatype-migration-boundary),
including retired types, and validates the entire response before returning a
deeply frozen inventory. It rejects duplicate/noncanonical IDs, contradictory
ownership and malformed type metadata rather than returning a filtered subset.
Its HTTP boundary allows only the exact direct-list query, requires a complete
200 response, rejects pagination/conflicting totals and requests `cache: no-store`
for identity and proof reads. Redirected or wrong-URL responses carry a scope
failure before HTTP-status classification; their 404 cannot authorize tag
creation. The [synthetic reader tests](chrome/tests/relayless/v1OwnedTypes.test.ts)
exercise these boundaries through the actual injected-fetch HTTP helper. This
inventory is not served-schema proof, a destination binding or upload authority;
those and actual browser/CORS/upgrade QA remain rollout gates.

The pure [Attention specification/schema proof](chrome/src/relayless/v1AttentionSpec.ts)
emits a frozen creation body with the pinned Python specification fingerprint.
It accepts a deliberately closed served-schema family: the five known Event
base fields (verified nullable or nonnull shapes), all ten exact Attention
fields, their declared requirements, supported annotations and boolean root
`additionalProperties`. Unknown assertions, extra optional fields, references,
unrecognized dialects or incompatible constraints refuse. This avoids treating
a shallow field match as complete validation; it is not a general JSON Schema
interpreter. Schema text is capped at 1 MiB, depth 64 and 100,000 parsed nodes,
with finite, lossless Unicode JSON required. The parsed snapshot freezes before
hashing. Its `schemaHash` is SHA-256 of the exact UTF-8 `schemaText` supplied by
the caller, retained alongside it; different formatting may have a different
digest. This is captured serialized text, **not** a raw HTTP wire-body hash or
a claim of general Python/Swift JSON-number serialization parity. It grants
no account, destination, creation or upload authority. The
[synthetic proof tests](chrome/tests/relayless/v1AttentionSpec.test.ts) pin the
cross-language creation fingerprint and incompatible-schema refusals. Fresh
owned-type/schema binding and actual provider/browser QA remain next gates.

The opt-in [Attention destination binder](chrome/src/relayless/v1Destination.ts)
connects those checks for explicit setup. It may publish a durable **local**
binding even when remote creation is disabled; do not use it as a read-only
preview. Transport, tag and destination effects must use the identical journal
handle within an operation, so HTTP checks and local commits cannot consult
different account gates. Each extension context may still own its own handle.
Existing full-ID pins survive renames and cannot be replaced by another choice.
New type creation requires `allowCreate: true`, a strict durable reservation,
and fresh owned-type/schema proof. The provider may redirect after committing;
that or a lost receipt is reconciled through explicit reads, never another
blind POST. A returned full UUID persists as submitted before further network
work, and missing visibility cannot authorize substitution or a second create.

Both the authoritative input schema and served schema must pass the closed
check. The input endpoint exposes immutable base transform annotations that the
served endpoint redacts: only nonempty `x-fulcra-transform` strings on `id`,
`start_time` and `end_time` may be removed for this check, and they are never
evaluated. Unknown/custom transforms still refuse. The **original input text**
and its exact UTF-8 digest are retained alongside the served-text proof. A final
owned-type read must show the same ID, active state and unchanged original input
schema before the current-generation journal commit. A stale served-schema
view alone therefore cannot approve incompatible current constraints. The
[synthetic binder tests](chrome/tests/relayless/v1Destination.test.ts) cover
concurrent setup, restart/lost receipt, pin/rename, late account changes and
schema/retirement races. This creates no records, grants no queue/exclusion or
capture authority, and has no default sender or UI wiring yet. Actual provider
creation/CORS and browser persistence remain rollout gates.

The opt-in [immutable outbox](chrome/src/relayless/v1Outbox.ts) adds local pending
capture. Its v3 IndexedDB upgrade preserves the v1/v2 gate, generations, datatype
and tag intents. Each entry retains the exact account/generation, full Event
ID, specification fingerprint, original input and served schema texts/digests,
installation identity, original event, tag UUIDs, primary and reproduced flat
record. The payload digest covers the retained serialized record; the envelope
digest covers its exact retained serialized text. These are local integrity and
reproduction checks, not provider receipts or authenticated provenance.

Caller JSON is detached before hashing; invalid/lossy values and accessors
refuse. Capture requires an enabled current gate plus the resolved local type
and account tag bindings in the same strict transaction. Every read reproves
both schemas/digests, specification, record/primary and payload, then rechecks
the current gate. The unique account/type/record identity accepts exact replay
but refuses changed content or generation; old generations remain held without
rebinding. Capacity is 5,000 entries and 32 MiB total encoded envelope bytes,
with a 4 MiB individual envelope and 64 KiB original-event limit. Full journals,
quota failures and unconfirmed strict durability reject without eviction.
The [synthetic queue tests](chrome/tests/relayless/v1Outbox.test.ts) exercise
reopening, concurrent contexts, corruption, capacity and account races. This
local queue has no POST, acknowledgement, deletion or settlement API; it does
not adopt legacy arrays or persist active visits. No default sender calls it.
Actual browser disk/quota, upgrade, auth/CORS and Safari sharing QA remain open.

The opt-in [exact record reader](chrome/src/relayless/v1Records.ts) asks the
scoped HTTP boundary for at most 32 canonical Event/Metric record UUIDs. It
builds instant equality/OR selectors with the full datatype repeated in each
group and the captured account; it uses no latest/count/history-window shortcut.
The provider's JSONL stream must finish successfully as an unambiguous 200,
with no pagination/partial markers or conflicting exposed owner/total headers.
Any exposed row-level `userid` or `fulcra_userid` must also equal the captured
account; malformed values or conflicting aliases refuse the whole read.
The existing decoded response cap and account/token/deadline checks still apply.
JSONL permits at most 32 rows and 4,096 physical lines; malformed suffixes,
duplicate JSON keys (including escaped aliases), duplicate/unrequested IDs,
invalid Unicode, lossy numbers and stream errors refuse. A complete empty 200
stream means only that these exact IDs were absent at this read.

Results are detached and deeply frozen. Submitted-field comparison requires
all submitted fields, exact nested values and list order; it permits root
server-added fields and equivalent lossless RFC3339 microsecond instants.
Comparison retains the admitted payload's 4 MiB UTF-8 JSON bound, including
root server fields; an oversized or invalid value cannot match.
The [synthetic readback tests](chrome/tests/relayless/v1Records.test.ts) cover
framing, identity scope, corruption and late account changes. This is read-only
one-read evidence, not a provider completeness contract, current schema/ownership
proof, replay-exclusion proof, a mutation lease or delivery acknowledgement.
It has no default caller or queue settlement effect.

Chrome record reads additionally require the opt-in
[network encoding protection](chrome/src/relayless/v1ReadProtection.ts).
Native Fetch can accept truncated gzip as empty or as a complete-looking row,
so decoded EOF alone cannot authorize an exact-ID absence. Explicit setup
installs four fixed rules:1002/1003 protect this extension's own HTTPS production
`/data/v1/records?` GETs;1004/1005 protect the typed ingest POST receipt leg
described below. Each pair requests identity encoding and blocks encoded replies.
Read and write assertions require the complete four-rule set.
Read-only assertions require exact native rule readback before principal proof,
before the record fetch/body, and after body completion, within the HTTP deadline.
An exposed encoded-response header also refuses. Missing protection cannot
silently fall back to unprotected reads or install itself during a read.
The generic JSON reader permits only canonical catalog paths under `/data/v1/`;
record reads must use the typed bounded reader. Encoded path aliases and other
data routes refuse before any provider request.

Only the exact existing Auth0 rule may coexist with these rules. Unknown own
rules, session rules or enabled static rule sets refuse rather than silently
remove or override policy. Setup changes must follow the caller's account/
generation transition discipline. The response-header condition requires the
Chrome 128 API; older/unsupported runtimes must fail setup/readback, never omit
the encoded-response block. This is Chrome protection; Safari needs its own
native identity/response-completeness transport. Existing profiles and default
senders are unchanged. The
[protection tests](chrome/tests/relayless/v1ReadProtection.test.ts) and
[readback tests](chrome/tests/relayless/v1Records.test.ts) cover rule inspection,
late changes, deadlines and response refusals. The development QA used Chrome
155 in a fresh profile: the actual source helper and fixed production rules
accepted unencoded present/empty replies and blocked full/truncated gzip against
an emulated HTTPS endpoint; unrelated paths and another initiator remained
unaffected. This is synthetic transport evidence, not provider/auth or Safari QA.

The [shared exclusion contract](../fulcra-common/fulcra_common/record_exclusions_contract.json)
is the canonical specification used by Python and the browser's pure
[exclusion spec wrapper](chrome/src/relayless/v1ExclusionSpec.ts). Its creation
fingerprint remains `92f7c8ea19c922fcafb1e9274e90ef9ff5974bc3e8644973fef9bd1bc5029af0`;
required-field order participates in that identity and is preserved. Attention
and exclusions share one [closed Event schema proof](chrome/src/relayless/v1EventSchema.ts),
selected only between those two static families. The exclusion reason enum is
an exact string set; adding a restore state or dropping a reason refuses.
The existing schema/transform/bounds rules still apply. Duplicate JSON schema
keys, including escaped aliases, refuse before any assertion can be discarded.
These helpers create
nothing and establish no owned policy binding or current exclusion/absence
proof. [Cross-language contract tests](../fulcra-common/tests/test_record_exclusions_contract.py)
and [browser tests](chrome/tests/relayless/v1ExclusionSpec.test.ts) protect the
existing Python identity and Attention behavior.

The opt-in [owned exclusion-control binder](chrome/src/relayless/v1ExclusionDestination.ts)
adds explicit local setup. It uses the complete authenticated input inventory
and inspects every Event input schema, including retired types, with a 64-Event
discovery cap. A compatible renamed control is found by its schema. Missing or
unreadable input, a plausible incompatible/retired control, or multiple matches
holds setup; none authorizes a new empty policy. Distinctive target/action fields
or the canonical name make a type plausible. An explicit existing full Event
pin can recover from ambiguous/capped discovery while still requiring fresh
ownership, active identity and closed input/served proofs. The original durable
UUID cannot be replaced. A first binding repeats discovery at the final owned
read, so new ambiguity or unreadable input also holds publication. A submitted
receipt preserves identity but does not bypass this first-binding discovery on
retry; only an explicit pin or an already resolved binding does so.

Creation requires `allowCreate: true` and a strict durable reservation before
the one POST. A lost/redirected result is reconciled through positive fresh
discovery; empty discovery never retries it. A known receipt is retained as
submitted before further proof, even if the account gate has changed. This
receipt-only journal write requires the exact original reservation and its
generation; it creates no intent and resolves no authority. Both browser
binders use it. A contradictory known UUID commits a permanent hold retaining
both UUIDs before returning an error. Ordinary reads, resolution and receipt
replay cannot clear that hold; there is no reset API.

Setup uses one monotonic budget, default and
maximum 10 seconds, with each HTTP operation capped at 8 seconds or the remaining
budget. The HTTP `timeoutMs` override is captured before awaiting and cannot
extend the configured transport timeout. Creation journal callbacks check the
optional deadline before local writes. An overall timer also bounds blocked
database/crypto waits; late callbacks still check the same deadline before
resolving. A late verified receipt may only be retained as historical evidence.
Generic JSON also rejects duplicate keys before constraints
can disappear. The [binding tests](chrome/tests/relayless/v1ExclusionDestination.test.ts),
[HTTP tests](chrome/tests/relayless/v1Http.test.ts) and
[journal tests](chrome/tests/relayless/v1Journal.test.ts) cover discovery, races,
recovery, deadlines and original-pin retention. This binder grants no current
policy, exclusion/absence or mutation authority. Policy reads, native repair,
installed browser QA and default sender integration remain rollout gates.

The pure [exclusion record codec](chrome/src/relayless/v1Exclusions.ts) preserves
Python's stable primary `collect:v1:excluded:{full target type}:{target UUID}`
and the control Event's UUID5 mapping. It accepts canonical Event/Metric targets,
rejects self-control targets and restore states, and emits the same flat fields,
single primary source, empty tags and point bounds as Python. Inputs detach
before hashing; the supported limit is 64 KiB total JSON, 128 nonempty evidence
references and 1,024 code points per reference. Unsupported values remain held.
The action UUID, time and evidence must already have a durable audit before a
caller may write a policy; encoding creates no such audit or permission.

The shared [instant parser/formatter](chrome/src/relayless/v1Time.ts) preserves
RFC3339 instants through microsecond precision using integer arithmetic. It
rejects invalid calendars, zones, precision loss and UTC years outside 1–9999;
its bounded text input is at most 128 characters. UTC formatting matches Python
with six fractional digits when nonzero, including dates before 1970. Generic
record comparison uses these exact instants; Attention capture still rejects
nonzero submillisecond input. The [shared synthetic vectors](tests/fixtures/v1_exclusion_cases.json),
[Python conformance](tests/test_v1_exclusions.py),
[codec tests](chrome/tests/relayless/v1Exclusions.test.ts) and
[time tests](chrome/tests/relayless/v1Time.test.ts) protect identity, fields,
precision and immutable capture. Current-policy reads and native repair are
separate operations; no default sender calls the codec.

The opt-in [policy reader](chrome/src/relayless/v1ExclusionPolicy.ts) requires an
already resolved original control intent before making provider requests.
`verifyExclusionV1` rechecks that existing binding without any local writes,
discovery, adoption or receipt promotion. It checks the same original reservation
inside the journal's account-gate/intent transaction after fresh owned input and
served-schema proof. Missing, downgraded or conflicting authority stays held.

Policy reads capture 1–32 canonical target UUIDs and a full Event/Metric target,
derive their stable control UUIDs, and read protected exact JSONL. Each returned
row must reproduce the full permanent exclusion: target, action, reason, evidence,
UUID, primary, empty tags and point timestamps. Supplied root `fulcra_userid` or
`userid` must agree with the captured account; other root server additions may
remain. The reader repeats the ownership/schema proof afterward, requires the
same schema hashes and reservation generation, and performs one final original
intent/gate predicate. It never repairs a missing intent during a read.

The returned mapping is deeply frozen and includes account, generation, target,
control and schema context. Use it as evidence of that read. Writers still need
before/after-write policy checks, a current mutation lease, durable actions,
native absence verification and controlled old-writer cutoff. A complete empty
mapping describes these exact IDs only. Any unreadable, malformed, partial,
encoded or late proof holds queued work. The overall monotonic/timer budget is
at most 30 seconds, each record request at most 8 seconds/remaining and each
binding verification at most 10 seconds/remaining. Exact-record timeout arguments
use the HTTP boundary's captured budget and cannot extend its configured limit.
Blocked local waits also time out. Late proofs are discarded, and resumed policy
steps recheck the completion flag and deadline. The
[policy tests](chrome/tests/relayless/v1ExclusionPolicy.test.ts)
cover corruption, ownership, original-authority loss, deadlines and account races.
This reader has no POST, native deletion, queue acknowledgement or default caller.

These pure encoders perform no authentication, upload or legacy queue
migration. Owned target/schema/exclusion bindings, durable account/generation/
identity/payload queues, exact readback, Safari protocol receipts and active-visit
checkpoints must switch together before the default senders change. Preserve
unknown-account legacy queues; a current sign-in is not evidence of their past
intended account. Replay floors and explicit legacy mappings must protect old
records, including backfills whose previous writer stored whole-second bounds.
Actual browser upgrades, account checks and signed App Group/Keychain QA remain
separate from unsigned builds or codec tests.

- **Relayless extension:** shipped, lives under [chrome/](chrome/). Direct-to-Fulcra ingest via Auth0 device flow. Foreground-only attention, AFK detection, pause control, onboarding wizard, right-click context menu, branded UI.
- **Python package:** reduced to the Fulcra Collect pointer plugin (`collect_plugin.py`). The relay-era backend (CLI, `ingest.py`, `fulcra.py`, `state.py`) has been retired.

The network-free [mutation journal](chrome/src/relayless/v1Journal.ts) serializes
one pending logical operation per account across IndexedDB connections. Its
atomic v4 upgrade preserves v1/v2/v3 gate, creation, tag and queued data and
rejects malformed prior store/index structures. Every reservation retains its
original account/generation/action, delivery-or-exclusion operation and bounded
request snapshot. The request digest covers that immutable identity envelope,
including the operation. A retained current pointer and count/byte frontier agree
with immutable history; missing or inconsistent metadata holds the account.
There is no expiry, release, reset or eviction of uncertain work.

`reserveMutation` commits preparation before returning; `issueMutation` has one
CAS winner and returns false for an already issued/verified intent. A restart
reads the original intent and cannot issue it again. `verifyMutation` stores
immutable caller evidence and advances the retained pointer; an old terminal
replay cannot release a newer owner. This method does not independently establish
provider truth. Prepared work may record an already proven result without
inventing a dispatch. Same-account generation rotation retains and holds pending
work; a future explicit recovery path must reconcile original authority. Another
account remains independent. Current and requested row digests run outside transactions; older unrequested
rows receive count/frontier checks, not a fresh content audit. An exact context
recheck follows inside the final strict-durability transaction. Absolute
deadlines are checked in resumed callbacks and before writes. Every mutation
API accepts an optional `AbortSignal` after its absolute deadline. Cancellation
while a transaction is unfinished aborts its writes, including cancellation after
a put succeeds but before commit. Shared database opening is preserved for other
callers. A cancelled digest/open continuation cannot later publish authority.
Cancellation after a committed issue keeps that phase issued; it never restores
dispatch or authorizes a retry.

Legacy logical requests and all proofs are limited to64 KiB; phased bounds
are described below. History permits100,000 actions globally and32 MiB retained
request/proof bytes per account. Reservations leave room
for a maximum-size proof before any issue. Quota/capacity failure preserves
history. The [ownership tests](chrome/tests/relayless/v1Mutations.test.ts) cover
atomic upgrades, restart, competing connections, original-generation holds,
corrupt/missing metadata, rollback, byte capacity and asynchronous deadlines.

This primitive performs no network request or queue acknowledgement. Future
typed actors must win a separate issued-once CAS for **each physical effect**,
keep one logical owner across policy writes, native tombstones and revival
repair, and bind verified delivery/exclusion to the exact original queued rows
in one settlement transaction. An issued source write followed by target absence
is UNKNOWN. Native absence is a deletion result only with verified permanent
policy and original authority. A second action reuses an existing exclusion's
original evidence as `already-excluded`. Old-generation outbox recovery,
default sender/native Safari integration and bounded terminal archival remain
rollout gates. The opt-in transport, actor and settlement layers below provide
separate explicit APIs; these are not yet the default sender.

The [phase journal](chrome/src/relayless/v1Journal.ts) adds an IndexedDB v5
version fence without rewriting v4 payloads. Explicit intent/pointer format1
remains local logical ownership only; it never acquires phases by interpreting
opaque legacy request JSON. New closed format2 uses `reservePhasedMutation` to
capture the complete original manifest and details before hashing. Delivery has
exactly `deliver`/`repair`; exclusion has exactly `policy`/`delete`. Every manifest
contains fixed destinations and canonical finite JSONL bytes/digests, at most32
records per inline body. Old manifest1 retains its64 KiB request limit; new
manifest2 uses the bounded body references and inline limits described below.
Larger unsupported requests fail before reservation and retain queued data.
Never truncate, silently remove or split one record to evade a body limit.

`issueEffect` atomically issues one prepared phase and marks the logical owner
issued. Competing contexts and restarts cannot win that phase twice. Earlier
phases must be verified before a later phase issues or verifies. `verifyEffect`
stores immutable **caller evidence**, including explicit `present`, `absent`,
`already-excluded` or `not-needed`; it does not prove a provider result. Its digest
binds original request, phase index/role, destination and body digest. A phase
can record a genuinely preexisting result while prepared. Finishing all phases
retains logical ownership until separate final `verifyMutation`. Original
account/generation, pointer/history, deadline and no-reset rules still apply.
Nested input detaches before awaits and returned states are deeply frozen.

Preparation reserves room for both maximum-size phase proofs plus final logical
proof within the existing 32 MiB account frontier. Actual phase proof bytes count
once; replay cannot change evidence or release another owner. The
[phase tests](chrome/tests/relayless/v1Effects.test.ts) cover legacy v4 bytes,
malformed-upgrade rollback, ordering, restart, corruption, proof context, input
races, quota, proof headroom and deadlines. No v4 payloads are hash-rewritten
inside a versionchange transaction. Current/requested rows receive reproof before
authority; unrequested history retains count/frontier checks.

This remains network-free storage. It does not validate actual record schemas,
prove policy/native absence or settle queued rows. The typed JSONL receipt
transport performs the winning guarded phase CAS itself immediately before its one
fetch; caller-supplied booleans/nonces cannot authorize dispatch. Legacy rows
cannot use that transport. Timeout after CAS remains issued/UNKNOWN; exact read
and permanent-policy reconciliation must follow without repeating that POST.
Repeated repair phases are unsupported: a second revival holds for explicit
reconciliation. Native browser/default/Safari and account recovery gates remain.

`issueGuardedEffect` is the network-free dispatch predicate for that future
transport. Before the action is reserved, retain the exact resolved local
creation/tag tuples in immutable `details.dispatchAuthority` as
`{creations: [...], tags: [...]}`. Guarded issue requires the same canonical tuples,
then reads them inside the strict transaction that checks account gate, owner,
history and referenced bytes and issues the phase. Missing, replaced, downgraded
or newly substituted tuples hold. A caller supplying a fresh replacement that
matches the current store cannot replace the original retained authority.
Creation keys and tag labels sort by exact code-unit order, without Unicode
normalization; property/array order alone does not change identity. Original
reservation generations may predate the current action. Non-native phase
destinations must occur among the creation tuples. Old requests without guards
cannot use this entry point. `issueEffect` remains a storage-only compatibility
CAS and must never be used by HTTP actors.

These predicates establish local continuity only. They do not prove current
provider ownership, schemas, live tags, permanent policy or correct record
content. Typed actors must retain a sufficient original guard set and fresh-prove
those remote facts. The existing64 KiB details cap may refuse a large guard set,
even when its record fits the outbox; visible refusal preserves the row. Complete
admitted-row dispatch coverage remains an actor rollout gate. The
[dispatch guard tests](chrome/tests/relayless/v1DispatchGuard.test.ts) cover original
tuples, canonical ordering, older reservation generations, competing contexts,
restart, cancellation and write rollback. No HTTP/default delivery or settlement
is enabled by this primitive.

The opt-in `V1Http.submitEffect` in the [HTTP transport](chrome/src/relayless/v1Http.ts)
closes generic `json()` record-ingest access. It materializes original pending
bytes with the same journal, fresh-verifies target Attention and permanent-control
owned input/served schemas and original resolved tuples, then checks every
retained tag through its canonical UUID lookup. A tag rename is allowed, but at
least one matching `userid`/`fulcra_userid` is required; missing or contradictory
ownership holds until that endpoint's caller-owned contract is independently
established. No tag creation or replacement occurs during submission.

The [typed preflight](chrome/src/relayless/v1TypedEffect.ts) admits one original
referenced Attention record or one reconstructed canonical exclusion-policy
record. Both fixed native plans are validated before a primary write, using the
original action/target UUID5 identity shared with Python. Native dispatch also
requires a supported current `DeletedRecord` served schema; unknown assertions,
fields or required properties hold. These are closed schema families, not a
general schema interpreter. Inline arbitrary source records cannot bypass full
original queue reproduction. Full canonical UUID checks remain mandatory.

After fresh proof, submission pins the exact bearer and proves it with API
userinfo. The winning `issueGuardedEffect` is the last authority await before
one fetch with that bearer; only synchronous expiry/cancellation checks follow.
The POST uses the exact original UTF8 JSONL, no trailing newline, explicit
`api_version=v1`, `application/x-jsonl`, no redirects/retries, and at most the
original4 MiB envelope/body bound. A whole monotonic/timer budget includes
capture, hashing, local transactions, preflight, fetch and response. Cancellation
reaches active journal transactions; a committed issue remains issued even if
fetch is never reached. Current gate/JWT, exact pinned-bearer API principal and
native rule state are checked again after the complete receipt body.

Only200/201/202 can return a strict finite duplicate-free JSON object with a
nonempty bounded string `upload_id`; exposed partial/owner/encoding
contradictions refuse. Missing, unreadable, lost or late receipts retain
issued/UNKNOWN without re-POST. No phase proof, logical completion or outbox row
is changed by a returned receipt. **Submission does not establish current replay
permission, reuse an existing policy's evidence, prove delivery/native absence,
settle queued data or prove backend quiescence.** Before/after policy and exact
readback actors remain a mandatory rollout gate, including avoidance of policy
overwrite. Default senders and installed profiles are unchanged.

Receipt protection uses the exact HTTPS API host, literal ingest route, POST,
this extension's initiator and sole `api_version=v1` query. Its Event suffix
`[a-f0-9-]+` deliberately protects a superset of canonical UUID spellings;
DNR grants no dispatch authority. Native Chrome rejected full/fixed-length UUID
regexes at its compiled-memory limit; the simpler protective matcher installs.
The strict transport/journal UUID checks independently refuse malformed targets.
Missing four-rule protection holds without automatic installation. Explicit
setup must repair missing rules; temporary extension reload in isolated QA
cleared them, so production installation persistence is still a separate gate.

The [typed transport tests](chrome/tests/relayless/v1TypedDispatch.test.ts) cover
large exact bodies, competing contexts, restart, original identity/schema/tag
holds, deterministic native identity, receipt ambiguity and timeout. Isolated
Chrome QA observed exact synthetic wire bytes, one POST across restart, and
encoded receipts refused while an otherwise identical unprotected control was
accepted. This is emulated-provider transport evidence; it does not establish
live provider, policy actors, Safari native transport or production installation.
See [Chrome's DNR reference](https://developer.chrome.com/docs/extensions/reference/api/declarativeNetRequest)
for native header conditions and compiled-regex limits; actual native QA is
required in addition to JSON rule readback and synthetic fetch tests.

`verifyAttentionV1` in the [Attention binder](chrome/src/relayless/v1Destination.ts)
provides fresh read-only proof of an already resolved original destination. It
requires the existing local tuple before requests, repeats complete owned-input,
closed input/served-schema and unchanged final-input checks, then atomically
requires the same resolved UUID/fingerprint/original reservation generation with
the current account gate. Missing, pending, submitted, retargeted or changed
original metadata holds without adoption, reserve, resolve, tag or record writes.
The verified result includes `reservationGeneration`; setup retains its existing
capture-compatible result shape. A new current generation for the same account
can verify an existing reservation without rewriting its original generation.

Setup's first-binding final candidate must remain uniquely canonical and have
the selected UUID/input text. A known submitted receipt retains identity but
cannot disambiguate multiple candidates. Explicit pins and previously resolved
original pins retain compatible renamed destinations. Neither path replaces a
known UUID with another candidate.

Setup and verification share one at-most10s monotonic/timer budget and at-most8s
per HTTP read, shortened by remaining time and the transport's own limit. A
terminal flag stops late resumed work even if a timer runs before clock advance.
Creation transactions additionally receive an AbortSignal, check resumed reads,
and abort an active uncommitted transaction when cancelled, including after an
add/put request succeeds. Completion/abort detaches the listener. Already
committed local results remain evidence to reconcile; cancellation cannot erase
an earlier commit. Verified creation receipts already returned by HTTP can still
be stored against the original reservation after outer expiry/account change;
this historical fact cannot resolve authority. Responses rejected within HTTP
remain UNKNOWN and must not trigger another creation POST.

The [binder tests](chrome/tests/relayless/v1Destination.test.ts) cover zero-write
verification, tuple loss/downgrade/retargeting during proof, new account gates,
first-binding ambiguity, blocked callbacks, active transaction rollback, listener
cleanup and late returned receipts. This proof still requires original queue,
policy, phase, exact readback and settlement checks before any delivery claim.

`verifyGuardedEffect` publishes or reuses local phase evidence only while the
original nonterminal action still owns its logical slot. It compares the exact
creation/tag authority retained in the request, then rechecks those tuples in
the same strict gate/owner/history/body transaction before either publication
or idempotent proof reuse. A terminal old action cannot reuse guarded proof
after releasing its slot, even if all bindings still match. Resolved reservation
generations can predate the action. Cancellation, deadlines, immutable proofs
and history limits still apply. `verifyEffect` retains storage-only historical
compatibility. Neither API establishes provider truth or releases an owner;
actors still need fresh remote proof and a closed evidence context. See the
[guarded-proof tests](chrome/tests/relayless/v1GuardedProof.test.ts).

The opt-in [action actor](chrome/src/relayless/v1ActionActor.ts) now reconciles an
original reserved delivery or exclusion against fresh provider reads. It uses
the original body and authority, checks owned target/control/tag/native schemas
around source and canonical policy reads, and stores closed context-bound phase
evidence with `verifyGuardedEffect`. A matching prepared source needs no POST;
an issued source that remains absent stays UNKNOWN without a new application
submission. A later exclusion can trigger a still-prepared repair only after
positive original source evidence. Original policy evidence is retained even
when another action created it. Changed policy/schema, consumed `not-needed`
repair or native revival holds rather than replacing an immutable proof.

Its frozen result is only an as-of-read observation. It keeps the original queue
and logical owner when called directly; UNKNOWN does not assert current
ownership. Nested reads inherit cancellation through an otherwise identical
`V1Http` clone, within one maximum 30-second actor budget. Repeated authority
reads can exhaust that budget on a slow provider, so real-service acceptance is
still required. A guarded application `fetch` can produce multiple native HTTP
attempts: isolated Chrome retried an empty-response POST internally. Receipts,
local phases and absence reads cannot prove backend job quiescence. Default
sender integration and installed-browser acceptance remain separate gates.
See the [actor tests](chrome/tests/relayless/v1ActionActor.test.ts).

### Decision authority

The [decision authority helper](chrome/src/relayless/v1DecisionAuthority.ts)
reduces repeated remote proofs within one decision. Each snapshot closes one
complete opening/closing owned inventory pair around the original target/control
input and served schemas, tags and native deletion schema. The actor observes a
fresh closed snapshot A, reads the exact canonical policy and original source,
then observes fresh snapshot B and compares them. A callback-scoped, exact-bearer
read session shares only its initial principal proof; it grants no metadata,
write or settlement authority. Public helpers retain independent safe defaults.
Opaque in-memory capabilities bind the exact HTTP root, journal, original
request/dispatch authority, source, reservations, signal and deadline. A failed
proof, callback exit, observed bearer replacement or same-root setup/typed POST
invalidates reuse. There is no persisted proof cache or global TTL.

Every POST still requires fresh principal, typed effect and read-protection
checks. The guarded issue CAS remains the last authority await before synchronous
cancellation/expiry checks and application fetch. Standalone callers must publish
a fresh gate generation before every bearer replacement, including same-account
refresh. The source-only [worker auth owner](#worker-auth-owner-development)
provides a narrower same-account renewal path: it closes admission, waits for or
cancels owned operations, retains the exact grant response and proves the same
API principal before publishing an exact successor. Unannounced token-only
replacement remains unsupported. Journal transaction predicates and phase-proof
v1 bytes remain unchanged. The
[authority tests](chrome/tests/relayless/v1DecisionAuthority.test.ts) and
[latency tests](chrome/tests/relayless/v1AuthorityLatency.test.ts) cover exact
capability scope, failure/effect invalidation, bounded streaming and lifecycle
holds. Synthetic delayed-fetch delivery measured 74 application fetches versus
337 before this change, with byte-identical retained settlement proof. Isolated
Chrome with a loopback TLS fixture and actual DNR/IndexedDB also preserved
settlement, restart, UNKNOWN-no-resubmit and zero-HTTP terminal history. These
checks establish neither provider latency nor installed/default/Safari acceptance.

### Worker auth owner development

Chrome has a source-only `v1-held` mode in addition to the default legacy mode.
The [worker owner](chrome/src/relayless/v1AuthWorker.ts) handles versioned display,
sign-in, refresh, sign-out and settings commands from the extension's own pages;
messages carry no bearer credentials. Old token/resolver entrypoints refuse this
mode. Both legacy and v1 drains are disabled, and destination setup reports
`SETUP_HELD`. Changing the mode during a worker lifetime requires restart; this
is not an installed stale-page fence or a rollout procedure.

The [broker](chrome/src/relayless/v1AuthBroker.ts) owns concrete read, setup and
existing action-actor operations under an original bounded deadline and mandatory
cancellation. Scoped [journals](chrome/src/relayless/v1Journal.ts) preserve that
lifetime through descendants and final transactions. No reusable write root is
returned. Confirmed settlement survives late cancellation; uncertain issued work
retains its original identity without a new submission.

#### Explicit protection setup source

The versioned worker `protection-setup` command accepts only a null payload. It
uses the real [protection class](chrome/src/relayless/v1ReadProtection.ts), created
lazily in the worker, under an already admitted broker lease. It does not start
online admission, install on boot or run from the Wizard. Missing native APIs
refuse setup without breaking read-only display or local revocation. Exact
original state, credential, journal gate, deadline and cancellation are checked
around native policy reads and immediately before the native write.

A success result says only `installed-as-of-read`, with capture held and drains
disabled. It is not completed destination setup or future policy authority.
Nonabortable native calls keep the single installer slot and original lease until
they settle, even after a prompt UNKNOWN result on timeout/revocation. A late
write may have occurred; no cleanup or rollback is inferred. A never-settling
native call keeps that worker's installer slot held. A later explicit operation
reads actual rules again rather than adopting a saved success flag.

Current fixed rules protect own-extension JSONL record GETs and Event/DeletedRecord
receipt POSTs; they do not cover JSON inventories, account information, OIDC or
Metric POST. [Synthetic actual-caller tests](chrome/tests/relayless/v1ProtectionSetup.test.ts)
exercise source semantics only. The source-stage Task 4 amendment permits this
producer preparation; actual native invocation requires its exact reviewed
procedure and applicable original C0/browser prerequisites. Real browser policy,
account/destination canary, installed-state migration and Safari acceptance remain
unverified. Default legacy mode, list/choose/create holds, null descriptors and
both disabled drains remain.

The [auth store](chrome/src/relayless/v1AuthStorage.ts) keeps finite attempts and
publication metadata in `fulcra-attention-auth-v1` IndexedDB, separately from
addressed `relaylessV1Credential:<UUID>` Chrome-local credential records. Retain a
complete successful response before proof; missing/torn receipts or unknown
commits hold without repeating the grant. Refresh binds the exact predecessor;
concurrent requests coalesce its successor. Ordinary proved same-account renewal
preserves generation only under broker ownership. Fresh sign-in, sign-out and
settings scope changes rotate generation, preserving old queued/issued evidence.

Noncredential settings commit atomically with authority in broker IndexedDB.
A separate nonsecret Chrome-local `relaylessV1Initialization` witness retains
the original initialization ID/snapshot through `CLAIMED`, `CREATING` and
`COMMITTED`, matching the authority record. Default/read-only opens cannot create
the database. Missing authority or witness holds the settings projection itself;
never reimport stale legacy settings. Missing database after `CREATING` also
holds after a crash before its first create: this conservative availability limit
has no automatic reset. See the [initialization tests](chrome/tests/relayless/v1AuthInitialization.test.ts).
Legacy settings/tokens/cache/sent state stays original evidence, without a live
second-store settings mirror or inferred historical ownership.

Page settings reads use the versioned `settings-read` command and wait for the
worker's local settings initialization, without starting online recovery. Page
readers never create authority directly or refresh credentials. Initial popup
controls stay unavailable until settings resolve; failed reads show an explicit
held state. The standalone read-only projection still cannot create a database.
Local display and scope commands also wait only for local readiness, never online
recovery. Synchronous publisher cancellation reaches auth CAS callbacks, while
late complete device-grant responses retain only their original private receipt.
Pause/disable retains the selected authentication under a matching disabled gate
and reports `paused`/`CONSENT_HELD`; operation admission remains closed. Resume
gets a fresh generation and consent. Sign-out clears the selection. This source
protocol does not migrate an installed broker or enable either drain.

[Synthetic broker tests](chrome/tests/relayless/v1AuthBroker.test.ts) and
[owned-operation tests](chrome/tests/relayless/v1AuthOwnership.test.ts) cover crash,
late result, lineage, consent and cancellation boundaries. Active-visit/raw
checkpoint retention and backfill now have the source-only
[held ingress](#held-capture-ingress-development) below. Bounded pending discovery
and actual draining remain separate work. Native Chrome proof, provider acceptance,
installed identity/storage preservation and sender fencing remain required;
Safari native ownership is not implemented by this Chrome slice.

### Candidate presentation preparation

The source-only [candidate adapter](chrome/src/relayless/v1DestinationCandidates.ts)
and [Wizard](chrome/src/wizard/Wizard.tsx) can display a future synthetic
`{version: 1, status: "candidates", accountId, candidates: [{id, name}]}` response.
The projection accepts only these fields, at most 128 unique full canonical
`Event/<UUID>` IDs, nonblank labels of at most 256 UTF-16 code units without
control characters, and at most 64 KiB of lossless JSON. It rejects malformed
Unicode, duplicate IDs, extra fields (including credentials/schema/provenance),
and an account differing from current display data. Labels and IDs may be private
UI data; neither payloads nor errors containing them are logged. Errors are static.
Account matching scopes presentation only; it proves no principal, ownership,
schema compatibility, freshness or completed setup.

Candidates have no default selection. Choose/create and Continue remain disabled;
no destination/settings write or setup advance follows. Late results after Back
or unmount are discarded, and the current display account is read after the list
response to reject stale account data. This is inert preparation: the actual
worker still returns `SETUP_HELD`, publishes no capture descriptor and keeps both
drains disabled. It does not call `runRead('types')` or install/fake read protection.
Protected production listing, setup/descriptor publication, explicit choice,
Task 4 C0/native sequencing, installed state and Safari remain open gates.

[Wizard regressions](chrome/tests/wizard.test.tsx) use synthetic messages, while
[worker regressions](chrome/tests/relayless/v1AuthCallers.test.ts) preserve the
actual held boundary and original legacy evidence. These are component/source
checks, not production listing or native acceptance. Shared
[test setup](chrome/tests/setup.ts) denies global `fetch` before caller imports and
reinstalls that denial in `beforeEach`, including after a previous fixture's
`vi.unstubAllGlobals()`. Later per-case hooks may install synthetic transports;
Chrome/IndexedDB remain synthetic. A fixture that resets globals within a case
must supply its synthetic transport before invoking a request caller. This guards
that JavaScript fetch seam at those boundaries, not Node sockets, native/browser
APIs or an OS network sandbox. The [reset-survival regression](chrome/tests/fetch-guard.test.ts)
uses only a synthetic fallback even during RED; it never invokes native fetch.

### Bounded local evidence development

The source-only [bounded read job](chrome/src/relayless/v1ReadBudget.ts) supplies
unused evidence methods on the [auth store](chrome/src/relayless/v1AuthStorage.ts),
[journal](chrome/src/relayless/v1Journal.ts) and
[ingress](chrome/src/relayless/v1CaptureIngress.ts). No production caller invokes
these methods. Defining them performs no storage operation, creates no origin or
setup witness, and enables no drain. Default legacy operation and `v1-held`
remain as described above. Native browser QA is incomplete; component tests and
builds cannot supply the missing runtime evidence.

Callers supply one mutable `V1ReadLimits` containing an absolute
`performance.now()` deadline in milliseconds, an abort signal and remaining
record/UTF-8 byte budgets. Nested reads consume that same budget. A read job
installs its timer before the first open/await, ends admission on cancellation or
deadline, aborts its own read transactions and closes its own handles. Late opens
close their returned handles; late local-storage or crypto results are discarded.
Optional synchronous checks in the immutable codec/schema validators stop a late
crypto result from starting another validation operation. Existing callers omit
those checks. Synchronous processing of one capped value and browser scheduling
cannot be preempted by this timer.

Evidence opens request no version upgrade, abort an absent database's implicit
upgrade, and require the exact supported version/store/index shapes: auth v1,
journal v5 and ingress v1. They neither create/migrate a database nor share the
cached writer handle. Existing confirmed-write completion and action lifetime
predicates retain their behavior. Missing/malformed records, unsupported versions,
blocked/failed reads or exhausted budgets return `unknown`, never a replacement
empty authority. The ingress/journal structural probes return `known` with a null
value only after shape validation and handle closure; that is no proof of origin,
checkpoint accounting or fresh installation.

Auth `readStateBounded` includes the separate committed initialization witness
under the same budget; attempt and credential reads validate the original private
record. Credential evidence remains broker-private. `readCaptureRecordBounded`
reads only reserved capture keys in the existing auth `state` store: it returns a
detached, immutable **raw, unvalidated** version-1 JSON object, at most 1 MiB and
within the caller's remaining budget. It does not read an ingress checkpoint or
invent a capture store. The exact singleton keys are `capture-origin`,
`capture-source`, `capture-selection` and `capture-accounting`; the UUID-suffixed
prefixes are `capture-setup:`, `capture-receipt:`, `capture-dispatch:`,
`capture-transition:` and `capture-group:`. Future linked setup/schema/group/origin
validation and producers are separate work; raw point evidence grants none of
their authority.

Journal `readAccountOwnerEvidence` checks the original account pointer, count,
both prepared/issued status scans, action primary/index identity and referenced
body/digests. Full reproduction uses the original captured schema/source/record
inputs, never the current label or a replacement identity. It returns the
original intent, including terminal phase/request/proof bytes, a separate
observed current gate, and the exact verified terminal pointer when present.
Only a positively empty account has no retained owner. An older generation is
evidence to inspect, without action authority; a verified retained intent is
historical evidence rather than a live lease. Missing pointer with surviving
history is unknown. Existing strict `readMutation`/`readEffect` checks remain
required under actual ownership before action. No read snapshot is a perpetual
no-owner or integrity certificate.

These inactive prerequisites may be developed while native observation issues
remain unresolved. Positive setup, fresh origin production, queue discovery,
activation, installed identity preservation and Safari native ownership remain
separate gates. Changed source/build bytes require fresh exact-source review and
native evidence; earlier results are tied to their original bytes.

### Held capture ingress development

In `v1-held` mode the actual worker retains live visit and history input through
[ingress checkpoints](chrome/src/relayless/v1CaptureIngress.ts), separately from
the upload journal. The version-1 `fulcra-attention-ingress-v1` IndexedDB stores
active raw observations, sealed held input, exact prepared candidates and
journal-verified pending acknowledgments. Both drains remain disabled. The worker
currently publishes no positive capture descriptor: actual input stays
unattributed; positive descriptor/enqueue tests use synthetic authority. Completed
setup publication and current-lineage validation are separate activation gates.

A strict transaction updates each checkpoint and its serialized byte/count
accounting together. The budget is 64 MiB of UTF-8 serialized data rows, including
raw input, prepared events, candidate and provenance; engine/disk overhead is not
included. A separate 16 MiB record serialization limit applies. No rows are
evicted. Quota, abort, blocked database or unreadable accounting returns retention
UNKNOWN/backpressure, without a durable receipt. Session and checkpoint writes
are separate transactions: recoverable session input remains until sealing
succeeds, but loss before the first successful checkpoint cannot be called safe.
Status currently scans retained rows to verify accounting; backlog read cost is
not constant.

Checkpoints occur on open, focus freeze/thaw, heartbeat mutation, emission and
the existing one-minute sweep. They retain uncapped clock/focus observations,
without interpreting uncertain long spans or downtime as measured attention.
Restart holds unfinished input; newer surviving session observations remain
separate unowned evidence. A changed local consent/capture epoch closes the old
portion; a new positive focus signal starts another. This local cancellation
marker supplies no account or destination authority. Preparing a captured event
checks that original local lifetime synchronously immediately before its final
transaction write. Revocation preserves raw input as held evidence and refuses
an ordinary success acknowledgment; previously sealed history stays intact.

Durable input keeps the original JS-scrubbed key unchanged; categorized input
omits hidden URL, host, title and metadata. The unsanitized visit URL stays in
session only. Metadata checks current consent, ignore rules and the original
page, including an in-document URL check. History selection and the worker's
versioned backfill command require positive consent. Stable input identities and
exact durable acknowledgments support retry without regenerating prepared data.
The first checkpoint freezes a separate nonsecret source context (client and
label); retry never borrows the latest label. Missing legacy context stays
explicitly unknown and active input is held. This context supplies no principal
or destination proof. Legacy source/UUID mapping is unchanged.

Held capture does not append to the capped legacy outbox or alter legacy
sent/recent/cache evidence. Storage observation preserves current and supplied
old/new rows with privacy-bounded projection and hash/linkage. An unsafe original
already removed from its old store stays durably UNKNOWN across quiet scans and
restarts; an unfenced old writer can still evict input before observation. Popup and wizard distinguish
local retained evidence from uploaded records and never mark held backfill as a
synced upload. Counts include late/conflict evidence rows, so they are not an
event count or proof of complete capture. See the
[actual caller regressions](chrome/tests/relayless/v1CaptureCallers.test.ts) and
[ingress protocol tests](chrome/tests/relayless/v1CaptureIngress.test.ts).

### Atomic settlement

`settleV1Action` adds opt-in atomic settlement around the actual actor. One
maximum 30-second deadline covers historical inspection, provider reconciliation
and the final strict IndexedDB transaction. A private in-memory observation
capability binds that transaction to this journal and the actor's original budget;
a serialized observation or generic caller proof cannot authorize settlement.
The transaction rechecks original authority, retained generations, complete
history, phase proofs and exact referenced queue bytes. It commits the terminal
receipt/pointer and removes exactly the original referenced queue row together.
Inline exclusion writes no queue rows; inline delivery remains held.

Retry first inspects retained settlement history, before body or provider access.
`already-settled` is a historical receipt: it makes no current provider or owner
claim, and leaves a newer owner or requeued same-ID record untouched. Cancellation
or quota failure before commit rolls back local changes; confirmed completion
remains settled even if cancellation arrives afterward. If completion is lost,
the result is UNKNOWN with `localCommit: unknown`; never promise that an UNKNOWN
retained the queue. Retry discovers a committed receipt without resubmission.
See the [settlement tests](chrome/tests/relayless/v1ActionSettlement.test.ts).
Queue draining, safe bounded terminal archival and default rollout remain gates.

Run `npm test` from the Chrome directory. Test and watch scripts use two workers:
unbounded host concurrency caused three otherwise passing deadline tests to
time out; the full unchanged suite passed with this worker bound. Individual
operation deadlines remain enforced.

The [effect body helper](chrome/src/relayless/v1EffectBodies.ts) supports explicit
new phase manifest2: a256 KiB logical-request limit and128 KiB per inline body.
Legacy logical format1 requests and old phase manifest1 remain64 KiB; proofs
remain64 KiB and all actual retained bytes/headroom count toward the same32 MiB
account history frontier. Unknown/mixed manifest versions hold. New reservation
APIs emit manifest2. To recover an old manifest1, read and use its original
stored intent/body/phase CAS; calling the new reserve serializer on the same
nonce holds rather than rewriting old request bytes or changing its action ID.

`reserveReferencedMutation` binds exactly **one** original immutable Attention
outbox row to phase0 (`deliver`); repair remains inline. Its manifest pins the
original account/generation/full Event ID/record ID and envelope hash/byte count.
The plan separately pins the exact transmitted UTF8 single-record JSONL text's
hash/byte count, with no trailing newline. Large admitted events use the existing
4 MiB envelope, avoiding a second body copy in operation history. Declared large
exclusion-policy/action inputs fit the explicit new inline/request bounds.

The journal reads referenced rows under the same gate/history/owner transaction,
uses their original generation/physical key, reproves bytes outside IDB and
rechecks the exact complete state before authority. Both references and the
[outbox](chrome/src/relayless/v1Outbox.ts) use one pure
[queue codec](chrome/src/relayless/v1QueueCodec.ts) to reproduce captured schema,
source, account/generation, record and envelope/payload digests. Changing only
outer metadata cannot reauthorize an old hashed payload under a different scope.
This deterministic local proof still grants no fresh provider ownership, policy
or dispatch authority. A current same-account gate can inspect held original
bytes; it cannot issue or verify an old-generation operation implicitly.

`readEffect` returns original materialized bytes only for unfinished operations.
Prepared/issued work still needs its original body even after phase0 verifies.
Only fully verified logical completion with valid final and every phase proof
hash makes metadata readable after the old queue body is removed; terminal
operations cannot materialize a new dispatch through this API. A later queued
payload with the same UUID cannot impersonate that old body. The body APIs do
not acknowledge queued work. The opt-in settlement API above atomically matches
the original key/generation/envelope hash, action/manifest and final proof before
removing that exact row. Missing data alone never proves delivery.

The [reference tests](chrome/tests/relayless/v1EffectReferences.test.ts) cover
large admitted wire payloads, exact restart/readback, original scope relabeling,
body loss/replacement, terminal/new-owner races, legacy version bounds, quota and
deadlines. Safe bounded terminal archival and default/native Safari rollout
remain integration gates. Journals still refuse capacity/quota failures visibly
without evicting uncertain work.

## License

Personal-use experiment; no package license is declared in [pyproject.toml](pyproject.toml).
