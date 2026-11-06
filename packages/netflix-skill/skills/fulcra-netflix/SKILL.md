---
name: fulcra-netflix
description: "Use when a user wants to obtain or import their Netflix viewing-history export into their own Fulcra account, or resume a saved Netflix import."
homepage: "https://github.com/ashfulcra/fulcra-tools"
license: "MIT"
user-invocable: true
metadata: { "openclaw": { "emoji": "🍿" } }
---

# Netflix history in the user's own Fulcra account

Guide the user through **HELLO → AUTH → EXPORT → IMPORT → SHARE**. Personal native
import can finish at IMPORT; SHARE is an optional, explicitly legacy annotation
workflow only. The development importer writes owned v1 **Event** records to
Watched. Native Event share propagation/group-pool discovery is UNKNOWN here.
Never promise that an annotation browser share exposes these native records.

Use the installed `fulcra-netflix` console or the same canonical script under the
matching pinned checkout's `.venv/bin/python`. Live upload requires installed
Netflix/common/Collect/media siblings; a skill-folder copy alone supports only
stdlib local check-only. No PEP 723 `uv run script.py` upload promise, dynamic
package install or annotation fallback. Installation/authority details:
[package guide](../../README.md); [auth](references/auth.md);
[export](references/netflix-export.md); [native fields/IDs](references/record-schema.md).

## Where to start — state and recovery probes

Use the intended installed checkout interpreter and preserve the runtime profile.
Default `FULCRA_COLLECT_HOME` is `~/.config/fulcra-collect`; `cli-netflix` is a
separate CLI namespace and uses CLI auth independently of the daemon account.

| Situation | Probe/action | Evidence and next step |
|---|---|---|
| Matching runtime installed? | `.venv/bin/python -c "from fulcra_netflix.native import require_runtime; require_runtime()"` from its checkout | Exit 0 proves versioned installed capability, without auth/profile/source. Failure needs matching installation; it does not authorize fallback. |
| CLI account usable? | `fulcra-api user-info` | Successful identity is preliminary account evidence. Nonzero/network error is UNKNOWN; diagnose before reauth. Real import freshly proves captured principal/target. |
| Source available and understood? | `fulcra-netflix viewing-history.csv --check-only --json` | Local parse/count only. No target/runtime/httpx/auth/profile/DB/recovery; failure is not empty history. |
| Saved work or interrupted import? | Re-run live `fulcra-netflix viewing-history.csv --json` with the SAME account/profile/explicit target | Original pending targets are proven/drained before opening new source, even if missing/empty/bad. UNKNOWN holds original payload; never erase journals/outboxes or blindly retry POST. |
| Optional sharing? | No CLI probe establishes native Event sharing | Finish personal import independently. Only explain the legacy broad annotation flow if the user chooses it and confirms a recipient. |

Stable pending authority includes codec/spec and explicit target or null; paths,
variants/new rows/discovered targets do not rebind it. Account/profile/pin/spec
changes hold pending. All actual pending destinations get fresh owned watched-spec
proof before drain; kernel payload/schema/dispatch validation stays per recovered
group. Later-group failure may retain partial complete-group evidence. Do not
claim a clean slate from an unreadable read or a local count.

## State 1 — HELLO

Explain that their export goes into their own Fulcra account as native Watched
Events. Slim has date-only estimates; rich has real times/profile information.
Check-only and a review come first. Sharing is optional. Native sharing support is
unverified; the separately retained legacy share is all annotation data, broader
than Netflix. Do not pitch that legacy share as native onboarding completion.
Honor existing import/install authorization; surface a specific missing choice or
prerequisite rather than generic permission-to-continue questions.

## State 2 — AUTH

Reuse the intended CLI account when usable. If auth is needed, use the installed
CLI's split browser flow from [auth.md](references/auth.md):

```sh
fulcra-api auth login --get-auth-url
fulcra-api auth login --device-code <DEVICE_CODE> --poll-timeout=5
```

Relay the web URL/code; keep device code and bearer tokens private. Bounded polls,
one active flow, respect returned expiry/interval. A network/read failure is not
proof that credentials are absent. Never change accounts or clear pending state
to bypass a recovery hold. Do not invoke/relay `auth print-access-token` as a
manual diagnostic step; the shared runtime owns authentication.

## State 3 — EXPORT

Use [the current export guide](references/netflix-export.md) and the user's
chosen profile/export. Netflix's official pages describe profile Viewing activity
CSV download and a personal-information request that may take up to 30 days after
verification. Follow actual download/expiry instructions; no promised typical
turnaround, link lifetime or complete history coverage from a parse success.

Slim uses synthetic noon UTC and a one-second interval, marked low confidence.
Within-file identical date/title rows have distinct occurrence IDs, but slim has
no profile identity: identical tuples across profiles/files can collide, and
partial overlap cannot prove correspondence of same-day rewatches. Rich retains
profile/real timestamps/durations and filters existing supplemental rows. Inspect
represented profiles before importing a household export. Rich and slim coexist;
one does not delete or upgrade the other's records automatically.

## State 4 — IMPORT

From the installed checkout, `--check-only` sanity-checks locally first:

```sh
fulcra-netflix viewing-history.csv --check-only --json
```

Review parse intent and source/target choice using the user's existing authorization.
Then invoke the actual native importer:

```sh
fulcra-netflix viewing-history.csv --json
# Optional explicit owned destination (replace illustrative UUID):
fulcra-netflix viewing-history.csv --data-type Event/00000000-0000-4000-8000-000000000001 --json
```

`--data-type` is optional. Default uses the full owned compatible Watched resolver;
explicit full Event/UUID pins cannot silently replace a prior shared media binding.
The profile/account spec journal preserves renames; a missing/deprecated/incompatible
bound type holds. Old annotation markers/UUIDs are not native identity. Repeat
explicit target selection unchanged on recovery; no target is needed for check-only.
`--no-verify` is refused before auth/source, including check-only; exact readback
cannot be skipped.

Main opens one source/reader after original recovery. All new input is materialized,
locally checked and finalized against the served schema/current primary IDs before
current record mutation. Existing incoming fields must match: extra unrelated
top-level cloud fields may remain, but nested/source/tag/time/note differences hold
the whole new input. No fingerprint/alias authorizes a skip or overwrite.
Already-staged recovery keeps its immutable payload/shared kernel behavior;
preflight is locally serialized, not provider CAS or cross-host edit atomicity.

New data uses serial chunks of at most 500, after whole-input proof. Only the
in-flight chunk is durable; unstarted remainder needs the original CSV again.
Do not promise an atomic or whole-file durable queue. Unknown issued work waits
for exact readback rather than another POST. Never reset private state or re-run
old token/annotation helpers to clear it.

Only `--json` is a machine contract. Original keys remain, with explicit
`counts_semantics="native-v1-complete-groups"` and native `status`:

- `posted` and `verified`: known current completed-group verified lower bounds,
  including exact already-visible records. They are not new-record counts, HTTP
  attempts or sampled rows. A fully matching replay can issue zero POST.
- `excluded`: separate from verified; `duplicate_input`: distinct input dedup.
  `skipped_existing` remains null; do not invent remote skip counts.
- `would_post`: check-only intent. `total` describes the current parsed events.
- `recovery`: separate verified/excluded evidence, never added to current counts
  or summed across retries. Original recovery may succeed before new-source error.
- A failed/partly visible chunk may add zero lower bound until its complete group
  is proven. Earlier completed chunks survive stage/receipt/clear/close failures.
  Nonzero/UNKNOWN is a hold, not empty or proven rejected provider state.

Final success follows runtime cleanup and exact readback. Report actual current
versus recovered evidence and limits. Do not say every rerun/overlapping CSV is a
server-side no-op or claim cross-tool equality from matching source hashes.

## State 5 — SHARE (optional legacy annotation workflow)

Personal native import is complete without sharing. Native Event propagation and
pool discovery remain UNKNOWN/unsupported by this skill until independently
proved; this legacy workflow is not a native share.

If the user specifically chooses to share legacy annotation data, obtain and
confirm their chosen recipient's Fulcra ID. No default recipient. Explain that
the browser workflow includes **all annotation data, not just Netflix**. The user
performs the share in their own browser at
[Context Web sharing](https://context.fulcradynamics.com/sharing?type=sending),
using the intended account and confirmed recipient. Ask them to confirm completion
or skipping; no CLI probe here verifies either legacy scope or native propagation.
Do not create/change permissions automatically or tighten the disclosure based
on an old TODO/upstream PR assumption. Skipping is valid.

## Boundaries

Keep original legacy records/shares/permissions. Native historical wrapped-reader
mapping/conversion needs separate evidence; no source test proves it complete.
Installed app, clean pinned install, wheel acceptance and live-account QA remain
separate release gates. Use [the native record reference](references/record-schema.md)
for source/UUID fields; fingerprints are provenance, not observation identity.
