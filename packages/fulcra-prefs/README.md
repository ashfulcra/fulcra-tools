# fulcra-prefs

> **Alpha (0.1.0).** The CLI, compiler, solver, and hooks are implemented, but
> the signal schema, file layout, and CLI surface may
> still change without a migration path. Expect to re-onboard or recompile
> across early versions. Don't build anything load-bearing on it yet; do try it
> and file issues.

`fulcra-prefs` is a user-owned preference layer on top of Fulcra — how agents
know their user and become more helpful over time: you capture
typed signals (preferences, facts, consent) with half-life decay, which are
compiled deterministically into per-platform preference documents; a consent-gated
export path keeps every disclosure logged — what the spec calls the Privacy Ledger.
It is built entirely on Fulcra annotation records and the Fulcra file library,
with no separate database. Collect is not required.

Agents append tiny typed events → a deterministic compiler folds them into
per-platform truth → every agent boots with that truth injected → groups decide
over consented slices, with every disclosure on the record.

---

## The idea: two layers

**Layer 1 — signals (the history).** Every preference, fact, or consent event is
one immutable, timestamped record on your Fulcra timeline. `capture` builds a
typed signal — kind, dot-namespaced key, scope (`global` or
`platform:claude-code`), signed strength (−1..1, where negative means aversion),
confidence, and a half-life — then records it to the Fulcra timeline as a
`MomentAnnotation` record (via the typed ingest surface) linked to your
"Preference Signals" definition. If the
ingest call fails after onboarding, the signal spools to a local outbox for
the next `compile`. The deterministic temp-id preserves a "this replaces my old
preference" reference across that gap. Capture still needs the remote onboarding
metadata, so this is not a fully offline client.

**Layer 2 — compiled docs (the current state).** The compiler
reduces the available signals to "what's true now":

1. **Decay.** `weight = strength × 2^(−age / half_life)`. A 0.9-strength signal
   with a 90-day half-life is worth ~0.45 after 90 days. A half-life is a
   positive number of days, or `null` for a durable fact — facts don't decay
   but are flagged stale after 180 days. (`capture` rejects a zero or negative
   half-life: a 0 would divide by zero in every later compile.)
2. **Supersedes.** Corrections drop the replaced signal; chains are followed;
   cycles are silently dropped.
3. **Conflicts.** Highest `|decayed weight| × confidence` wins — so a confident
   explicit preference beats a low-confidence *inferred* (auto-captured) one of
   similar strength. This is a scoring rule, not an absolute priority for explicit
   statements: a stronger inferred signal can still win.
   Ties break to the newer signal (then signal id for full determinism). The
   emitted weight is still the raw decayed weight; confidence only decides which
   signal wins.
4. **Scope overlay.** Platform-scoped signals beat global ones; one compiled doc
   per platform is written alongside the global doc.

Output is byte-identical for the same signals at the same instant, regardless of
input order — tested, not aspirational (see `tests/test_determinism.py`).

---

## Install & auth

```
uv tool install "git+https://github.com/ashfulcra/fulcra-tools#subdirectory=packages/fulcra-prefs"
```

(Python 3.11+. From the repository root, the local-source equivalent is
`uv tool install ./packages/fulcra-prefs`. The Git install above selects the
package directly; it does not install or run Collect.)

Requires a Fulcra account:

```
fulcra auth login          # authenticate with the Fulcra CLI
fulcra-prefs onboard       # creates the Preference Signals annotation definition
```

### Headless / remote hosts (no browser: CI, cloud agents, sandboxes)

`fulcra auth login` opens a browser. On a host without one, use the device flow's
two-step form — print the URL, approve it on any device, then finish with the code:

```
fulcra auth login --get-auth-url            # prints a verification URL + device code
# open the URL on your phone/laptop and approve, then:
fulcra auth login --device-code <DEVICE_CODE>
```

This two-step form still uses the CLI, so it is the **tier-1** onboarding path for
browserless hosts (tier 2 is the raw-HTTP/no-shell fallback). Two caveats on
proxy-intercepting sandboxes (e.g. some cloud CI): the auth CLI's device flow uses
raw `http.client` and can bypass an `HTTPS_PROXY`, and access tokens expire
(~24 h). When the CLI login itself can't run, drive the same Auth0 device-code
exchange with a proxy-aware HTTP client and write the result to
`~/.config/fulcra/credentials.json`. The exact credential-file schema (the four
fields, with the ISO-local-naive expiration conversion from `expires_in`) and the
`refresh_token`-grant refresh recipe — which renews an expiring token without
re-prompting a human — are documented in §1b ("Writing the CLI credential file + refreshing") of the
raw-HTTP reference
([`skill/references/fulcra-prefs-tier2-http.md`](skill/references/fulcra-prefs-tier2-http.md)).

---

## Quickstart

```bash
# 1. Onboard (once per account)
fulcra-prefs onboard

# 2. Capture a preference
fulcra-prefs capture \
  --key dining.cuisine.thai \
  --value '{"liked": true}' \
  --strength 0.8 \
  --platform claude-code

# 3. Compile signals into preference docs
fulcra-prefs compile

# 4. Read back the compiled doc (JSON to stdout; status messages to stderr)
fulcra-prefs get

# 5. Inject compiled prefs as a session context block
fulcra-prefs inject --platform claude-code
```

Status messages go to stderr. `get` writes JSON; `inject` writes a plain-text
context block. Neither mixes status messages into stdout.

---

`--platform` records where a signal came from; it does not set its scope.
Add `--scope platform:claude-code` when the preference should apply only there.
The default scope is `global`.

## Auto-capture (batch)

Agents shouldn't need an explicit "remember this" — they can passively notice
preferences during a session and record them all at once at the end, with a
single consented call:

```bash
fulcra-prefs capture-batch --file candidates.json --platform claude-code
```

where `candidates.json` is a JSON array of signal specs (`key`, `value`,
`strength`, and optional `kind`/`scope`/`confidence`/`half_life_days`/
`supersedes`). Mark inferred-but-unconfirmed signals with a lower `confidence`
(0.4–0.6) to reduce their weight in conflict resolution. That is a useful
constraint, not a guarantee that inferred signals cannot beat explicit ones.
The when/what heuristics live in
[`skill/references/fulcra-prefs-capture.md`](skill/references/fulcra-prefs-capture.md).

## Platform hooks

CLI-capable platforms can install managed hooks:

```bash
fulcra-prefs install-hooks --platform claude-code
fulcra-prefs install-hooks --platform codex
```

The SessionStart hook recompiles first (the spec requires compile to run at
every tier-1 session start so the preference docs reflect the latest signals),
then emits JSON hook output containing the preference block as session context.
If you haven't onboarded yet, or the compiled doc is empty, it emits nothing at
all — hooks are designed to fail silent, so they never break a session start.

Auto-capture hooks drain candidate files at lifecycle boundaries. Agents write
the same JSON array accepted by `capture-batch` to:

```text
~/.local/state/fulcra-prefs/candidates/<platform>/<session_id>.json
```

The helper command is safer than hand-editing JSON:

```bash
fulcra-prefs notice \
  --platform codex \
  --session "$CODEX_SESSION_ID" \
  --key docs.style.human_agent_quality \
  --value '{"preference":"Write direct, concrete docs for humans and agents."}' \
  --strength 1.0 \
  --confidence 1.0 \
  --half-life 365
```

If the agent has raw session text rather than a key/value pair, use the
conservative extractor. It only emits candidates for explicit preference
language and never ingests directly:

```bash
fulcra-prefs extract-candidates \
  --platform codex \
  --session "$CODEX_SESSION_ID" \
  --text "I prefer concise tone in status updates." \
  --write
```

Claude Code drains on `PreCompact` and `Stop`; Codex drains on `PreCompact`
because Codex `Stop` fires every turn. On successful capture the file is renamed
with a `.captured` suffix so repeat lifecycle hooks do not double-ingest it.
`install-hooks --uninstall` removes the config entries but leaves the generated
scripts on disk; they are inert unless referenced by the platform config.

Other agents use the same queue and signal shape. See
[`skill/references/platforms.md`](skill/references/platforms.md) for Claude,
Claude Code, ChatGPT, Codex, OpenClaw, and Hermes.

---

## How it works

Signals are `MomentAnnotation` records posted to the typed ingest endpoint
`POST /ingest/v1/record/{data_type}` (the base type in the path; the "Preference
Signals" definition rides in the record's `sources`). The package drives that endpoint through its
own transport (`store.ingest_signal`) so the offline outbox and shard cache
share one record representation. See the
[write-path modernization proposal](docs/proposals/write-path-0138-modernization.md)
for the deferred library migration. Each signal
carries a key (dot-namespaced), a typed value, a strength in [-1, 1]
(negative = aversion), a half-life, and a scope (`global` or `platform:<name>`).
The `capture` command posts the record and also writes a per-signal
*write-through cache shard* under `prefs/signals-cache/` — one file per signal id,
so concurrent captures never race on a shared file, and a signal captured offline
still reaches compile after the next flush.

Compile reads signals **authoritatively from get-records** (so a capture from
*any* platform is visible — including shell-less tier-2 agents that only POST to
ingest and never write a shard), unioned with the Fulcra Files shard cache to cover
offline-captured-not-yet-ingested signals and ingest→read indexing lag, deduped
by capture identity. Once a shard's signal is confirmed in get-records the shard
is pruned, so the cache stays bounded rather than growing forever. The fold
is a pure function of `(signals, now)`; the CLI handles its reads and writes: it folds signals by key using half-life
decay to compute effective weights, resolves conflicts to the signal with the
highest `|effective weight| × confidence` (ties broken by `observed_at`, then
signal id for full determinism), drops superseded signals including chains and cycles, and
writes canonical JSON to `prefs/compiled.json` and per-platform overlays under
`prefs/platforms/`. The output is byte-identical for the same
inputs regardless of input order — the determinism contract tested in
`tests/test_determinism.py`. Full design rationale: [`docs/SPEC.md`](docs/SPEC.md).

A platform view is always *global + that platform's overrides*: `get --platform X`
and `inject --platform X` return the merged platform doc, and when platform `X`
has no overrides of its own they fall back to the plain global doc (rather than
nothing) — a platform with no special-casing simply sees your global prefs.

How agents consume the compiled doc: `inject` prints a compact preference block
at session start (e.g. `- comms.tone.concise: {'preferred': True} [+0.90]`). It
reads the compiled document from Fulcra Files through the API; it does not
re-run the compiler math. Because every platform reads the same
compiled file, preferences are consistent across Claude Code, Codex, ChatGPT, or
any other agent you run. Shell-less agents that can't run the CLI follow the
raw-HTTP recipes in `skill/references/fulcra-prefs-tier2-http.md`: same
device-flow auth, capture = one POST, read = one file download. No agent
re-derives the compiler math — the compiled file is the shared source of truth.

---

## The skill

Agents that need to read or capture preferences use the fulcra-prefs skill at
[`skill/SKILL.md`](skill/SKILL.md). It routes by capability: CLI-capable agents
use the commands above; shell-less agents have a raw-HTTP reference at
[`skill/references/fulcra-prefs-tier2-http.md`](skill/references/fulcra-prefs-tier2-http.md)
covering device-flow auth, file reads, and record posts with no CLI dependency.

---

## Solving group decisions

Give `solve` a list of options (each with the preference keys it touches) and a
map of participant compiled docs:

```bash
fulcra-prefs solve \
  --options options.json \
  --participants docs.json
```

Where `options.json` is e.g.:

```json
[{"id": "thai-spot", "keys": ["dining.cuisine.thai", "dining.noise.quiet"]},
 {"id": "bbq-barn",  "keys": ["dining.cuisine.bbq"]}]
```

And `docs.json` is `{"alice": <compiled doc>, "bob": <compiled doc>}`. The
output is a ranked list plus a human-readable trace explaining every score and
veto — the trace is the deliverable, not a debug artefact.

The solver ranks deterministically: weighted-sum scoring, hard-veto on any
participant's strong aversion (anyone at or below the veto threshold kills an
option outright), lexicographic tie-breaks, no LLM in the loop. A trace line
might read: `bbq-barn: VETOED by bob on dining.cuisine.bbq (−0.80)` — so the
why is auditable without replaying the math.

---

## Consent & the Privacy Ledger

```bash
fulcra-prefs consent grant --key-glob "dining.*" --audience ea-agent
fulcra-prefs consent revoke --key-glob "dining.*" --audience ea-agent
fulcra-prefs consent list
fulcra-prefs get --for ea-agent     # filtered export + logged disclosure
```

`get --for <audience>` filters the compiled doc to keys covered by active grants,
then logs a `consent.disclosure.<audience>` signal recording exactly what was
shared and to whom. Every disclosure is an annotation record on your Fulcra
timeline — the Privacy Ledger is that record stream, not a separate audit log.
If the ingest call fails, the disclosure record is spooled to the local outbox
and flushed on the next `compile` — a disclosure is never emitted unlogged.

---

## Testing

```bash
uv run --package fulcra-prefs --extra dev pytest packages/fulcra-prefs/tests -v
```

The suite includes `test_determinism.py`, which asserts byte-identical output
across repeated calls and shuffled input orderings. If those tests ever flake,
treat it as P0 — the determinism contract is load-bearing for the cache shards
and cross-platform compile consistency.

---

## Current limitations

- **A successful compile can still be incomplete.** A failed live signal read
  falls back to the Files shard cache; HTTP errors warn on stderr, while some
  transport and malformed-response failures fall back silently. Confirmed shards
  are pruned, so that fallback cannot reconstruct the full history. `inject` also
  returns success with no context on a read or parse failure (warning on stderr).
  Do not use either exit status alone as proof that all preferences were loaded.
- **Corrections use `supersedes`.** The compiler resolves those references;
  there is no preference replace/update command. Consent revocation changes
  later exports, but it cannot take back a document already disclosed. This
  package does not provide a physical-erasure guarantee. The
  [write-path proposal](docs/proposals/write-path-0138-modernization.md) records
  the open questions about tombstones and retention.
- **Single-user.** The solver takes pre-compiled docs as input; there is no
  multi-user sync layer in v1.
- **No packaged MCP write server.** The command path uses CLI/API access.
  Shell-less capture can use the [raw-HTTP recipe](skill/references/fulcra-prefs-tier2-http.md);
  compiling still needs a host running the Python compiler.
- **Lifecycle support differs by platform.** Claude Code and Codex have managed
  local hook installers. ChatGPT and general Claude need an app/action/MCP or
  raw-HTTP bridge. OpenClaw and Hermes use the same candidate queue from their
  own lifecycle surfaces.
- **A double-ingest corner case.** If a capture's ingest POST *succeeds* but the
  follow-up cache-shard write fails (rare: network blips between the two calls),
  the record is spooled and re-POSTed on the next `compile` flush — so the raw
  Fulcra timeline ends up with two annotation records for that one signal.
  Compile dedupes by signal id, so compiled output and weights stay correct;
  only the underlying record stream carries the duplicate. That corner case
  remains in this implementation.
