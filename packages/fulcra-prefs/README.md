# fulcra-prefs

`fulcra-prefs` is a user-owned preference layer on top of Fulcra: you capture
typed signals (preferences, facts, consent) with half-life decay, which are
compiled deterministically into per-platform preference documents; a consent-gated
export path keeps every disclosure logged — what the spec calls the Privacy Ledger.
It is built entirely on Fulcra annotation records and the Fulcra file library,
with no separate database and no vendor lock-in beyond your own Fulcra account.

---

## Install & auth

```
uv tool install fulcra-prefs
```

(From the workspace for now — PyPI release tracks the v1 stabilisation window.)

Requires a Fulcra account:

```
fulcra auth login          # device flow; a free account is created on first login
fulcra-prefs onboard       # creates the Preference Signals annotation definition
```

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

Status messages go to stderr; JSON output goes to stdout. Scripts can safely
pipe `fulcra-prefs get` or `fulcra-prefs inject` without filtering noise.

---

## Claude Code session hook

Add to `~/.claude/settings.json`:

```json
{"hooks": {"SessionStart": [{"hooks": [{"type": "command",
  "command": "fulcra-prefs compile >/dev/null 2>&1; fulcra-prefs inject --platform claude-code"}]}]}}
```

The hook recompiles first (the spec requires compile to run at every tier-1
session start so the preference docs reflect the latest signals), then prints
the preference block as session context. If you haven't onboarded yet, or the
compiled doc is empty, it emits nothing at all — both commands are designed to
fail silent, so this hook never breaks a session start.

---

## How it works

Signals are annotation records posted to `/ingest/v1/record` via the Fulcra
ingest API. Each signal carries a key (dot-namespaced), a typed value, a strength
in [-1, 1] (negative = aversion), a half-life, and a scope (`global` or
`platform:<name>`). The `capture` command posts the record and writes a per-signal
cache shard under `prefs/signals-cache/` in your Fulcra file library — one file
per signal id, so concurrent captures from different platforms never race on a
shared file.

Compile is a pure function of `(signals, now)`. It folds signals by key using
half-life decay to compute effective weights, resolves conflicts to the signal
with the highest absolute effective weight (ties broken by `observed_at`, then
signal id for full determinism), drops superseded signals including chains and
cycles, and writes canonical JSON to `prefs/compiled.json` and per-platform
overlays under `prefs/platforms/`. The output is byte-identical for the same
inputs regardless of input order — the determinism contract tested in
`tests/test_determinism.py`. Full design rationale: [`docs/SPEC.md`](docs/SPEC.md).

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
uv run --package fulcra-prefs pytest packages/fulcra-prefs/tests -v
```

The suite includes `test_determinism.py`, which asserts byte-identical output
across repeated calls and shuffled input orderings. If those tests ever flake,
treat it as P0 — the determinism contract is load-bearing for the cache shards
and cross-platform compile consistency.

---

## v1 limitations (honest)

- **Signals cache workaround.** `compile` reads signals from
  `prefs/signals-cache/` shards instead of calling `get-records` directly.
  This is because `fulcra-api` has no record-read-by-definition helper wired yet.
  The replacement (real `get-records` reads scoped to the Preference Signals
  definition) is tracked on the bus and lands when CLI annotation commands ship.
- **Single-user.** The solver takes pre-compiled docs as input; there is no
  multi-user sync layer in v1.
- **No MCP write path.** The Fulcra MCP exposes read operations today; capture
  and compile require a CLI-capable agent. Filed as a platform gap.
- **Cron documented, not installed.** The recommended cron (`fulcra-prefs compile`
  on a timer) is described in the skill; the installer is out of scope for v1.
