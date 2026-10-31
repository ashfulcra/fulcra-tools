# AGENTS.md — autoloaded by Aider / Cursor / Continue.dev / Claude Code / OpenHands

fulcra-attention is **fully relayless**. The real work lives in the Chrome MV3 extension under `chrome/`: it signs in with an Auth0 device flow and POSTs records **directly to the Fulcra API** (`https://api.fulcradynamics.com/ingest/v1/record/batch`). There is no localhost daemon involvement, no pairing, no per-extension token, and no relay route — the relay-era Python backend (CLI / `ingest.py` / `fulcra.py` / `state.py`) has been retired.

The Python package (`fulcra_attention/`) is now just the Fulcra Collect *pointer* plugin (`collect_plugin.py`): it does no collection and only surfaces an "Attention" entry that tells the user to install the browser extension and sign in via the browser.

Development has an opt-in [v1 contract](fulcra_attention/v1_contract.json) and
[Python](fulcra_attention/v1.py), [TS](chrome/src/relayless/v1Wire.ts) and
[Swift](<safari/FulcraAttention/macOS (App)/V1Wire.swift>) pure encoders. Default
senders remain legacy. The opt-in [IndexedDB journal](chrome/src/relayless/v1Journal.ts)
provides transactional account/generation transitions and durable Event creation
intents. It does not verify a principal: bind it only after API identity proof.
Never reset an unresolved intent after a lost response or treat a same-account
new generation as permission to POST again; reconcile the original intent.
Preserve generations, use strict write durability and fail loudly on unreadable
state/capacity. Default writers remain legacy. The source-only
[Chrome worker auth owner](README.md#worker-auth-owner-development) routes actual
opted-in auth/settings publishers through versioned commands, private finite
receipts and bounded concrete operation lifetimes. Broker IndexedDB owns
noncredential settings atomically. Page `settings-read` commands wait for the
worker's local initialization without starting online recovery; page readers
cannot create authority directly or refresh. Initial controls wait for settings
and failed reads surface held status.
Preserve the separate once-only initialization witness and hold missing
state/witness without reset or legacy reimport. Local revocation must not await
online recovery; pause retains selected auth with action admission closed.
The unused [bounded evidence APIs](README.md#bounded-local-evidence-development)
use independent noncreating read handles and one shared monotonic deadline/record/byte
budget. Their raw capture point reads are unvalidated evidence; retained old or
terminal owners grant no action authority. Preserve full original-body reproduction,
private credential access and existing strict writer/lifetime predicates. Do not
activate a caller or infer native acceptance from this prerequisite.
The inert [candidate presentation slice](README.md#candidate-presentation-preparation)
has a separate [explicit protection producer](README.md#explicit-protection-setup-source)
in the worker. Only `protection-setup` with null payload may install real DNR
rules, under an already admitted original broker lease; it must not start online
admission or run on boot/display. Preserve exact state/credential/gate checks,
deadline and synchronous cancellation immediately before update. Keep one native
installer reserved until actual settlement after caller UNKNOWN; never fake
quiescence, rollback or reusable readiness. Source tests do not clear original
native/C0/browser/Safari gates. List/choose/create, descriptors and drains stay held.
The candidate presentation slice itself
accepts only bounded versioned full Event ID/name metadata matching current display
account data; it grants no principal/schema/ownership/setup proof. The actual
worker list/choose/create commands remain `SETUP_HELD`. Never connect unprotected
JSON inventory to this UI as if native protection/setup existed. Candidate display
has no default choice, setup write or advance; ordinary synthetic tests cannot
clear Task 4/C0/native/Safari gates. Shared test setup denies global fetch before
imports and reinstalls it before each case after global restoration; later fixture
hooks may supply synthetic transports. This is not a socket/OS/native sandbox;
follow the [fetch-seam limits](README.md#candidate-presentation-preparation).
Keep private labels, IDs and payload errors out of logs. Both opted-in drains remain disabled. Retain legacy state and unknown original
owners. Source-only [held ingress](README.md#held-capture-ingress-development)
now retains actual visit/backfill input under strict atomic 64 MiB serialized-row
accounting, without eviction or an uploaded claim. Positive capture descriptors
remain absent until completed setup; actual inputs stay unattributed. Keep raw
clock evidence distinct from measured focus, preserve restart/scope gaps and
privacy suppression, and retain originals until durable seal. Read failures are
UNKNOWN, never zero. Original legacy stores remain evidence; unobserved old-writer
eviction is unresolved. Actual draining, installed sender fencing and Safari
native integration remain gates. The [shared vectors](tests/fixtures/v1_wire_cases.json)
pin flat fields, existing v3 sources and common UUID5 mapping; new captures keep
milliseconds while source identity keeps whole seconds. URL inputs must already
be JS-scrubbed capture keys within the documented shared boundary; hold unsupported
input unchanged instead of applying a second native URL normalization. Reject invalid or
unrepresentable times, do not round. The schema permits historical null device
identity; a new capture context must supply its installation slug. The spec
fingerprint is frozen: use a new version for semantic schema changes. No codec
call authorizes a destination, replay or upload. Follow the
[remaining transition gates](README.md#v1-contract-development) before switching
queues, native bridge or default transport; unknown legacy queue accounts must
remain held, and actual installed browser identities/state must be preserved.

Key extension details: records use the `com.fulcra.attention.v3.` source_id namespace, folding the identity slug into the hash for multi-browser distinctness. **The slug and the human label are two different things and are sourced differently per browser — do not conflate them.** Chrome derives both from the identity label its wizard refuses to skip. Safari has no wizard and cannot read the device name (`UIDevice.name` is entitlement-gated since iOS 16 and returns the bare model without it — and the *simulator returns a real-looking name*, so never validate that path on a simulator), so `DeviceIdentity.swift` mints an automatic per-installation slug in the shared App Group and leaves the label nil. Consequence: a `machine:<slug>` tag exists only where a human label does. Anything touching this must preserve injectivity — two distinct installs must never produce one slug; over-counting a device is acceptable, merging two is not, because source_ids cannot be recomputed after the fact. Cross-check `chrome/src/relayless/*` (`oidc.ts`, `signIn.ts`, `relaylessSender.ts`, `ensureDefinition.ts`, `wire.ts`, `config.ts`) and `fulcra_attention/collect_plugin.py` before making claims about the flow.
