---
name: fulcra-agent-forge
description: "Bridge GitHub into a fulcra-agent-teams review flow: mirror PR state onto the team store as evidence, and auto-approve a review when its PR merges — so review status reflects the forge even when no human files a verdict."
homepage: "https://github.com/ashfulcra/fulcra-tools"
license: "MIT"
user-invocable: true
metadata: { "openclaw": { "emoji": "🔗" } }
---

# Fulcra Agent Forge

Enhances [`fulcra-agent-review`](../fulcra-agent-review/SKILL.md). When a review's `artifact:` is a
GitHub PR URL, the forge is the ground truth — this skill mirrors it onto the team store so
`coord-engine review status` reflects reality:

- **Evidence**: one idempotent shard per PR state transition
  (`_coord/evidence/<slug>/state-<OPEN|MERGED|CLOSED>.md`).
- **Auto-verdict**: when the PR merges, a `verdicts/forge.md` approval is written (reviewer `forge`) —
  the review tally then folds it like any reviewer.

## Where to start — the re-entrancy probes

The forge is **stateless** — `forge mirror` is a full idempotent pass, so there is no mid-journey
resume; the probes are a preflight confirming you *can* mirror. Enter at the **first probe that
fails** (per `docs/skill-quality-pattern.md`); both probes are non-mutating (the mirror pass itself
is the only write, and it's idempotent):

| Probe (run in order) | Command | Passes when | If it fails, enter at |
|---|---|---|---|
| Engine + auth usable? | `uv tool run coord-engine doctor <team>` | exits 0 and the last line is exactly `doctor: healthy` | fix engine/auth first (see fulcra-agent-reconcile) — do NOT mirror against a broken engine |
| GitHub reachable? | `gh auth status` | exits 0 (an authenticated `gh` is installed) | authenticate the GitHub CLI: `gh auth login` — without it `forge mirror` is a clean no-op (exit 0) and no evidence is written, so review status stays stale |

Both probes clean → run one mirror pass to fold current PR state into review evidence (see
[Usage](#usage)); re-run on your heartbeat alongside `reconcile`.

## Usage
```bash
uv tool run coord-engine forge mirror <team>   # one pass over all PR-backed reviews
```
Run it ad hoc, or on the heartbeat alongside `reconcile`. **Requires the GitHub CLI (`gh`) authenticated**;
without it the command is a clear no-op (exit 0) — the skill degrades, nothing breaks.

## Notes
- Only `github.com/<owner>/<repo>/pull/<n>` artifacts are mirrored; other artifacts are skipped silently.
- A failing `gh` call leaves the review untouched (never fabricates state).
- The forge approval doesn't override human verdicts — a human `changes` still blocks (CHANGES dominates).
