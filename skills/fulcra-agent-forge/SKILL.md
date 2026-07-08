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
| Watching my authored PRs? | `uv tool run coord-engine needs-me <team> --agent <me>` | a PR you authored or requested review on surfaces as a `[FORGE]` item (feedback is reaching you) | it isn't a swept target — register it: `forge watch <team> <pr-url> --agent <me>`, then sweep (see [PR feedback lives on three surfaces](#pr-feedback-lives-on-three-surfaces)) |
| Feedback swept? | `uv tool run coord-engine forge feedback <team>` | exits 0 — a sweep pass over all three surfaces completed (new shards, if any, written) | authenticate `gh` first (the row above); an unauthenticated sweep is a clean no-op |

All probes clean → run one mirror pass to fold current PR state into review evidence and sweep feedback (see
[Usage](#usage)); re-run on your heartbeat alongside `reconcile`.

## Usage
```bash
uv tool run coord-engine forge mirror <team>   # one pass over all PR-backed reviews
```
Run it ad hoc, or on the heartbeat alongside `reconcile`. **Requires the GitHub CLI (`gh`) authenticated**;
without it the command is a clear no-op (exit 0) — the skill degrades, nothing breaks.

## PR feedback lives on three surfaces
A PR's feedback is scattered across three GitHub API surfaces, and a poll that reads only one misses the rest:
- **Formal reviews** — `gh pr view <url> --json reviews` (APPROVE / CHANGES / COMMENTED verdicts).
- **Inline review comments** — `gh api repos/<owner>/<repo>/pulls/<n>/comments` (diff-anchored threads; a distinct REST shape).
- **Conversation comments** — `gh pr view <url> --json comments` (the PR timeline).

The motivating failure mode: a watch prompt that polled conversation comments alone saw an empty timeline
and reported "no feedback" while a formal CHANGES review and inline comments sat unread — a real review
went unseen this way. `forge feedback <team>` (sweep-only) and `forge mirror <team>` (which now sweeps too)
hit **all three** surfaces every pass, mirroring each item to an idempotent shard keyed by its GitHub node
id, so a re-run converges rather than duplicating. Items authored by the PR author are skipped as
self-comments — and when the PR author can't be resolved (the reviews call failed), self-skip can't apply,
so the sweep notes it on stderr.

**Watch PRs the review flow doesn't already cover.** A PR that backs a review artifact is swept
automatically; an authored or upstream PR with no review doc is not, until you register it:
```bash
uv tool run coord-engine forge watch   <team> <pr-url> [--agent <responsible>]  # register; default responsible = caller
uv tool run coord-engine forge unwatch <team> <pr-url>                          # deregister
uv tool run coord-engine forge feedback <team>                                  # sweep all three surfaces now
```
Discovery reads the review doc's `of:` first, falling back to `artifact:`; a watched PR joins that same
target set.

**The needs-me guarantee.** Swept feedback on a PR you're responsible for surfaces as a `[FORGE]` item in
`needs-me` and `briefing` for that agent, and **persists until you ack it** — it does not age out. Acking is
two steps (non-obvious, so spell it out):
1. `uv tool run coord-engine needs-me <team> --agent <me> --json` — each `forge-feedback` row carries an
   `items` array; those stems are the ack ids.
2. `uv tool run coord-engine inbox <team> --ack <item-id> --agent <me>` — ack each stem. A later sweep that finds a
   *new* node id writes a new shard, which re-surfaces — you silence only what you've already seen.

## Notes
- Only `github.com/<owner>/<repo>/pull/<n>` artifacts are mirrored; other artifacts are skipped silently.
- A failing `gh` call leaves the review untouched (never fabricates state).
- The forge approval doesn't override human verdicts — a human `changes` still blocks (CHANGES dominates).
