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
fulcra-coord broadcast "ACTION — review the updated FULCRA-PRIMITIVES.md (<commit SHAs>). \
WHAT CHANGED: <one-line summary>. IF YOUR WORK TOUCHES FULCRA SURFACES: <concrete refactor, \
e.g. switch raw-REST annotation/tag creation to the CLI/lib; re-check installed fulcra-api version>. \
Reply on the bus if the doc is wrong for your platform. — claude-code:<host>:fulcra-primitives-maintainer"
```

This is part of the doc-update procedure, not optional: the doc is only useful
if the fleet re-aligns to it. (Operator directive, 2026-06-15.)

## Scripts

| Script | Cadence | What it checks |
|---|---|---|
| `drift-check.sh` | daily | Narrow fingerprint — OpenAPI endpoint paths + annotation methods, `fulcra-api-python` main HEAD, published `fulcra-api` PyPI version, annotation-command count, MCP scopes — vs `.primitives-state/baseline.json`. The fast tripwire for the documented full-rewrite trigger (annotation **record** commands, incl. delete, landing in the CLI). |
| `weekly-review.sh` | weekly | Wide fingerprint — full path+method set, all schema names, docs page + MCP discovery hashes — vs `weekly-baseline.json`, **and** always drops `WEEKLY-REVIEW-DUE.txt` so a session does a genuine end-to-end human-eyes re-read (catches docs prose / new MCP tools a hash can't judge). |

On drift either script posts a bus alert to `claude-code:Mac:fulcra-tools` as
`claude-code:Mac:fulcra-primitives-maintainer`, then advances its baseline so it
alerts once per change, not every run.

Both scripts derive their checkout root from their own location and find
`fulcra-coord` on `PATH`, so they're portable across machines/clones. All
runtime state (baselines, logs, alert/flag files) lives in `.primitives-state/`
at the checkout root and is gitignored.

## Install (launchd, macOS)

The role pushes the doc **directly to `main`** (doc-only); everything else,
including this tooling, goes through the normal PR + review flow.

1. Clone the repo to a dedicated checkout and set the per-cwd bus identity:
   ```bash
   fulcra-coord identity set claude-code:<host>:fulcra-primitives-maintainer
   ```
2. Copy the plist templates from [`launchd/`](launchd), replacing
   `__CHECKOUT__` with the absolute path of your checkout, into
   `~/Library/LaunchAgents/`, then `launchctl load -w` each. The daily job runs
   ~09:13, the weekly ~Sun 09:27 (off-minute on purpose — see fleet-friendly
   scheduling).
3. First run writes the baseline; subsequent runs alert only on change.
