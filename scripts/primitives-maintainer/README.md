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

The daily check **fails closed.** Every probe is validated against a positive
control (the parsed verb list must contain known-stable sentinel verbs; the
fetched spec must contain a sentinel path) before any result is trusted. A probe
that returns empty or unparseable is `UNKNOWN` → alert, never "no drift", and
`UNKNOWN` never advances the baseline — including on a first run, which will not
write a baseline it could not fully observe. This is deliberate: on 2026-07-16
this script missed `record`/`delete` landing in 0.1.37 because its fingerprint
was a decorator grep against one file that could not produce a hit no matter
what the CLI did. A scan whose empty result is not proven meaningful is worse
than no scan.

On drift either script posts an alert to the **coord** team bus (`coord-engine
tell fulcra …`) into the `claude-code:Mac:fulcra-primitives-maintainer` inbox —
the role that acts on it. `drift-check.sh` names **what** changed (which verbs
appeared/disappeared, which version, which group) in both the tell and
`.primitives-state/DRIFT-ALERT.txt`, and flags a `record`/`delete` move as the
full-rewrite trigger at P1.

The baseline advances after a drift so the same change is not re-*discovered*
daily — but that does not clear the debt. `DRIFT-ALERT.txt` stays until a
session **rm**s it, and every run that finds it still there re-alerts at P1
("OUTSTANDING"). Clear it when the `FULCRA-PRIMITIVES.md` rewrite is done, not
before. A drift the script could not characterize does not advance the baseline
at all.

Exit codes: `0` clean, `1` drift or outstanding unactioned alert, `2` UNKNOWN
(probe failure).

### Env knobs

| Var | Default | Use |
|---|---|---|
| `PRIMITIVES_STATE_DIR` | `<checkout>/.primitives-state` | Point a test run at a scratch state dir. **Always set this when trying the script out** — a live run mutates the real baseline. |
| `PRIMITIVES_COORD_ENGINE` | `coord-engine` on `PATH` | Point at a stub to capture the tell instead of posting it. |
| `PRIMITIVES_SPEC_URL`, `PRIMITIVES_MCP_URL`, `PRIMITIVES_PYPI_URL` | production | Redirect a probe (testing; `file://` works). |
| `PRIMITIVES_SKIP_GH` | `0` | Set `1` on a host with no `gh` auth. Records `cli_head: disabled` — an explicit opt-out on the record, rather than a probe that silently always fails. |
| `PRIMITIVES_CLI_SENTINELS` | `auth,user-info,catalog` | Verbs the CLI parse must contain for its result to be trusted. |

`drift-check.sh` needs `uvx` on `PATH` (it runs the published package to read
its help). The launchd plist's `PATH` includes `/opt/homebrew/bin`.

Both scripts derive their checkout root from their own location and find
`coord-engine` on `PATH`, so they're portable across machines/clones. All
runtime state (baselines, logs, alert/flag files) lives in `.primitives-state/`
at the checkout root and is gitignored.

## Install (launchd, macOS)

The role pushes the doc **directly to `main`** (doc-only); everything else,
including this tooling, goes through the normal PR + review flow.

1. Clone the repo to a dedicated checkout. coord takes identity per-command via
   `--agent`/`--from` (no persisted identity to set); announce presence once:
   ```bash
   coord-engine presence beat fulcra --agent claude-code:<host>:fulcra-primitives-maintainer \
     --workstream fulcra-primitives
   ```
2. Copy the plist templates from [`launchd/`](launchd), replacing
   `__CHECKOUT__` with the absolute path of your checkout, into
   `~/Library/LaunchAgents/`, then `launchctl load -w` each. The daily job runs
   ~09:13, the weekly ~Sun 09:27 (off-minute on purpose — see fleet-friendly
   scheduling).
3. First run writes the baseline; subsequent runs alert only on change.
