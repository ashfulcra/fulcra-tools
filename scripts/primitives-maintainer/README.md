# primitives-maintainer

Operational tooling for the **fulcra-primitives-maintainer** role: an agent (or
cron) that keeps [`FULCRA-PRIMITIVES.md`](../../FULCRA-PRIMITIVES.md) — the
agent field guide to Fulcra's platform primitives — aligned with the live
platform, so the doc never drifts from reality.

The doc itself is the deliverable; these scripts are the **detection** layer.
Rewrites (model judgment about what changed and how to phrase it) are done by a
session when a script flags drift — the scripts never edit the doc.

## After every substantive doc update: broadcast (required)

Whenever you make a **substantive** change to `FULCRA-PRIMITIVES.md` — a new or
changed CLI/API/MCP surface, tier guidance, or the rewrite trigger firing (NOT
typo/wording fixes) — immediately broadcast a task to **all** agents to review
the updated doc and refactor their work if it touches Fulcra surfaces:

```bash
coord-engine broadcast fulcra "Review updated FULCRA-PRIMITIVES.md (<commit SHAs>)" \
  --from claude-code:<host>:fulcra-primitives-maintainer --workstream fulcra-primitives \
  --summary "WHAT CHANGED: <one-line summary>. IF YOUR WORK TOUCHES FULCRA SURFACES: <concrete refactor, \
e.g. switch raw-REST annotation/tag creation to the CLI/lib; re-check installed fulcra-api version>. \
Reply if the doc is wrong for your platform."
```

This is part of the doc-update procedure, not optional: the doc is only useful
if the fleet re-aligns to it. (Operator directive, 2026-06-15; retargeted to
coord team `fulcra` on 2026-07-04.)

## Scripts

| Script | Cadence | What it checks |
|---|---|---|
| `drift-check.sh` | daily | Fingerprints the **published** agent-facing surface — the top-level verb list and per-group subcommands of `fulcra-api <version> --help` for the version currently on PyPI, the PyPI version itself, the full path→methods map of the OpenAPI spec, MCP OAuth scopes, and `fulcra-api-python` main HEAD — vs `.primitives-state/baseline.json`. Covers the documented full-rewrite trigger for the part that is mechanically visible: `record` and `delete` are **top-level CLI verbs**, so one landing or vanishing moves `cli_verbs` and the alert names it. See the claim/limit table below for what it does **not** see. |
| `lib-alert.sh` | sourced by both | The shared alert path: sender resolution, the role target, reachability verification, rc-checked delivery, and `ALERT-UNDELIVERED.txt`. Sourced rather than copied — this half of the system had the same bug in both scripts precisely because it was copy-pasted into both. |
| `test-drift-check.sh` | on change | Regression suite for `drift-check.sh` and the alert path. Runs the real script against scratch state dirs, a stub CLI, `file://` probe URLs and a stub `coord-engine` — no network, no bus, no live baseline. Run it before shipping a change to the daily check: `./test-drift-check.sh` (`-v` to see each run's output). |
| `weekly-review.sh` | weekly | Wide fingerprint — full path+method set, all schema names, docs page + MCP discovery hashes — vs `weekly-baseline.json`, **and** always drops `WEEKLY-REVIEW-DUE.txt` so a session does a genuine end-to-end human-eyes re-read (catches docs prose / new MCP tools a hash can't judge). |

### What `drift-check.sh` can and cannot see

Read this before treating a clean run as coverage.

| Surface | Covered? | By what |
|---|---|---|
| Top-level CLI verbs, incl. `record` / `delete` | yes | `cli_verbs`, parsed from the published package's `--help` |
| CLI subcommands of every group (`auth`, `data-type`, `file`, `share`, `tag`, and any **new** group — groups are discovered, not hardcoded) | yes | `cli_groups` |
| Beta-gated CLI verbs | yes | `cli_beta_verbs` (`--beta --help` minus the default surface) |
| A release that changes the surface | yes | `pypi_version` + `cli_verbs` |
| REST endpoints: any path added, removed, or re-methoded | yes | `spec_hash` over the full path→methods map |
| Unreleased changes on `fulcra-api-python` main | partial | `cli_head` — a HEAD sha only; says *something* changed, not what |
| **MCP tool list** | **no** | `mcp_scopes` is OAuth `scopes_supported` only. A new MCP write tool under an existing scope moves nothing here, and tools/list needs an authenticated session. `weekly-review.sh`'s human-eyes pass is the only coverage. |
| **Datashare REST endpoints** | **no** | They are not in `openapi.json` at all (verified 2026-07-16: 53 paths, zero datashare paths), so `spec_hash` cannot see them. The CLI `share` group in `cli_groups` is the only coverage. |
| Docs prose; semantics changing behind an unchanged signature | **no** | Nothing mechanical can. `weekly-review.sh` covers the docs half. |

The daily check **fails closed.** Every probe result is either a real observed
value or `UNKNOWN`. There is no third category — no default that type-checks as
data, no `or []`, no omitted key, no empty string a comparison would read as a
legitimate state. `UNKNOWN` → alert, never "no drift", and `UNKNOWN` never
advances the baseline — including on a first run, which will not write a
baseline it could not fully observe.

This applies to the **sub-probes** too, which is subtler than it sounds. Each
top-level verb's group help and the `--beta` help are probed separately, and a
failure in any of them rejects the **whole** CLI fingerprint. A failed group
`--help` must not mean "that group has no subcommands", and a failed `--beta
--help` must not mean "there is no beta surface" — the beta surface is
*currently empty on a healthy run*, so defaulting a failed beta probe to `[]`
yields byte-for-byte the value a healthy probe yields, and would compare clean
forever. A build with genuinely no `--beta` gate is a real but *different*
observation, so it records as `NO_BETA_FLAG`, not `[]`.

Every probe is also validated against a **positive control** before its result
is trusted — an absence of drift means nothing unless the probe could have found
some:

| Probe | Control |
|---|---|
| `cli_verbs` | must contain the sentinel top-level verbs (`PRIMITIVES_CLI_SENTINELS`) |
| `cli_groups` | each sentinel group (`PRIMITIVES_CLI_GROUP_SENTINELS`, default `auth`) must come back with ≥1 observed subcommand — otherwise a group whose probe is permanently broken reads as an empty group forever |
| `cli_beta_verbs` | must be an observed list or `NO_BETA_FLAG` |
| `spec_hash` | the fetched spec must contain the sentinel path (`/user/v1alpha1/annotation`) |
| `mcp_scopes` | the fetched discovery doc must contain the sentinel scope (`PRIMITIVES_MCP_SENTINEL`, default `openid`) |

This is deliberate: on 2026-07-16 this script missed `record`/`delete` landing in
0.1.37 because its fingerprint was a decorator grep against one file that could
not produce a hit no matter what the CLI did. A scan whose empty result is not
proven meaningful is worse than no scan.

## The alert path (`lib-alert.sh`)

On drift either script posts to the **coord** team bus (`coord-engine tell fulcra
…`). `drift-check.sh` names **what** changed (which verbs appeared/disappeared,
which version, which group) in both the tell and `.primitives-state/DRIFT-ALERT.txt`,
and flags a `record`/`delete` move as the full-rewrite trigger at P1.

**The alert path is part of the probe.** A detector that pages a dead mailbox
reports silence exactly as convincingly as a detector that sees nothing. On
2026-07-16 the daily fired correctly at 16:11:50 with real drift and its P1 went
to `claude-code:Mac:fulcra-primitives-maintainer` — one host's session identity,
hardcoded in a script that ships to every host (both scripts had the same line),
last beat 8 days earlier. `tell` returned 0. The drift was found by hand. So:

| | Rule |
|---|---|
| **Target** | The **role** `fulcra-primitives-maintainer`, never a named agent. Sessions stop; the role outlives them, and whoever holds it is by definition who should act. |
| **Sender** | Resolved at runtime from `PRIMITIVES_AGENT` / `FULCRA_COORD_AGENT` — the same var the engine reads. Unset, we omit `--from` and let **coord-engine's own** resolver derive `coord-reconcile:<hostname>`; a script that mints its own id is a second resolver that agrees with the engine right up until it doesn't. |
| **Reachability** | Verified **every run, including clean ones** — routing rot costs nothing until the day it matters, which is the day you find out. Reachable = `roles status` says HELD/CONTESTED **with a fresh holder** (or, for `PRIMITIVES_TARGET_KIND=agent`, `presence show` says live/idle — the engine's own broadcast reach). |
| **Fail-closed** | A lookup that errors or does not parse is **UNKNOWN → a problem**, never "assume it's fine". "Nobody is listening" and "I could not check" are both loud; neither is a pass. |
| **Delivery** | `tell`'s rc is checked and a nonzero rc (e.g. the slug-prefix collision that returns 1 "already exists") is a **delivery failure**, not a log line. It used to be logged — to a file nothing reads. |
| **Loud locally** | Any of the above failing writes `ALERT-UNDELIVERED.txt`, prints to stderr, and exits **3** on a run that would otherwise have exited 0. A dead mailbox cannot tell you it is dead, so the local side has to. |

A vacant role is *deliberately* a visible local failure rather than a fallback to
some other recipient: silently redirecting a P1 to whoever is around is how a
mailbox becomes wrong without anyone noticing. Fix it by giving the role a holder
(`coord-engine roles claim fulcra fulcra-primitives-maintainer --agent <id>`);
the next run that can deliver clears the marker itself. Fleet-wide, the engine's
own `coord-engine escalate` sweep is what turns a vacant role into a P1 at its
maintainer — these jobs report their own reachability, they don't reimplement it.

### The three markers

The baseline advances after a drift so the same change is not re-*discovered*
daily — but that does not clear the debt, and once the baseline has moved the
alert file is the **only** surviving record of what changed. So there are three
markers in `.primitives-state/`, deliberately separate:

| File | Means | Cleared by |
|---|---|---|
| `DRIFT-ALERT.txt` | Real drift was observed; a `FULCRA-PRIMITIVES.md` rewrite is owed. | A session that did the rewrite, with `rm`. **Nothing in the script ever truncates it.** A second drift *appends*; every run that finds it re-alerts P1 ("OUTSTANDING"). |
| `PROBE-UNKNOWN.txt` | A probe could not answer; the surface was not observed. | The next run whose probes answer — the script removes it itself. |
| `ALERT-UNDELIVERED.txt` | Nobody could be shown to have received this run's alert (target vacant/stale/unverifiable, or the `tell` was dropped). Whatever else the run said, treat it as unheard. | The next run whose target answers — the script removes it itself. |

They are separate because they are different debts, with different owners and
different discharge conditions: fix the doc, fix the probe, fix the routing.
Collapsing them loses data in one direction:
an offline run overwriting `DRIFT-ALERT.txt` with probe-failure text would mean
that once someone fixed the probe and cleared the marker, the rewrite that was
actually owed is gone — silently. An `UNKNOWN` run observed nothing, so it has
nothing to say about a drift an earlier run *did* observe; it points at the open
drift alert instead, and leaves it alone.

A drift the script could not characterize does not advance the baseline at all.

Exit codes: `0` clean, `1` drift or outstanding unactioned alert, `2` UNKNOWN
(probe failure), `3` the alert path could not deliver on a run that was otherwise
clean. 3 never masks 1 or 2 — those already summon a human, and more specifically.

### Env knobs

| Var | Default | Use |
|---|---|---|
| `PRIMITIVES_STATE_DIR` | `<checkout>/.primitives-state` | Point a test run at a scratch state dir. **Always set this when trying either script out** — a live run mutates the real baseline. |
| `PRIMITIVES_COORD_ENGINE` | `coord-engine` on `PATH` | Point at a stub to capture the tell instead of posting it. |
| `PRIMITIVES_AGENT` / `FULCRA_COORD_AGENT` | *(unset — coord-engine derives `coord-reconcile:<hostname>`)* | The identity alerts are sent **as**. Never hardcode one in a script or plist template. |
| `PRIMITIVES_TARGET` | `fulcra-primitives-maintainer` | Who alerts are **for**. A role by default. |
| `PRIMITIVES_TARGET_KIND` | `role` | `role` verifies via `roles status`; `agent` verifies via `presence show` liveness. Changes only *how* reachability is checked, never *whether*. |
| `PRIMITIVES_TEAM` | `fulcra` | Coord team to post to. |
| `PRIMITIVES_SPEC_URL`, `PRIMITIVES_MCP_URL`, `PRIMITIVES_PYPI_URL` | production | Redirect a probe (testing; `file://` works). |
| `PRIMITIVES_SKIP_GH` | `0` | Set `1` on a host with no `gh` auth. Records `cli_head: disabled` — an explicit opt-out on the record, rather than a probe that silently always fails. |
| `PRIMITIVES_CLI_SENTINELS` | `auth,user-info,catalog` | Top-level verbs the CLI parse must contain for its result to be trusted. |
| `PRIMITIVES_CLI_GROUP_SENTINELS` | `auth` | Groups that must come back with ≥1 observed subcommand. Proves the group sub-probe can produce a hit. |
| `PRIMITIVES_CLI_CMD` | *(unset — runs the published package via `uvx`)* | Base command for the CLI probe. Point at a stub CLI to test without PyPI. |
| `PRIMITIVES_MCP_SENTINEL` | `openid` | Scope the MCP discovery doc must contain. Empty disables. |

`drift-check.sh` needs `uvx` on `PATH` (it runs the published package to read
its help). The launchd plist's `PATH` includes `/opt/homebrew/bin`.

Both scripts derive their checkout root from their own location and find
`coord-engine` on `PATH`, so they're portable across machines/clones. All
runtime state (baselines, logs, alert/flag files) lives in `.primitives-state/`
at the checkout root and is gitignored.

## Install (launchd, macOS)

The role pushes the doc **directly to `main`** (doc-only); everything else,
including this tooling, goes through the normal PR + review flow.

1. Clone the repo to a dedicated checkout.
2. **Claim the role the alerts are addressed to**, or nothing this tooling
   detects reaches anyone. This is the install step, not a nicety — the scripts
   check it on every run and exit 3 if it is vacant:
   ```bash
   coord-engine roles claim fulcra fulcra-primitives-maintainer --agent <your-id>
   coord-engine roles status fulcra fulcra-primitives-maintainer   # expect HELD
   ```
   The lease has a 24h SLA, so a session that stops holding it makes the role
   vacant — and the next run says so, loudly, instead of pretending.
3. Copy the plist templates from [`launchd/`](launchd), replacing
   `__CHECKOUT__` with the absolute path of your checkout, into
   `~/Library/LaunchAgents/`, then `launchctl load -w` each. The daily job runs
   ~09:13, the weekly ~Sun 09:27 (off-minute on purpose — see fleet-friendly
   scheduling). launchd does not inherit your shell environment: if you want a
   named sender, set `FULCRA_COORD_AGENT` in the plist (see the note in it).
4. First run writes the baseline; subsequent runs alert only on change.
