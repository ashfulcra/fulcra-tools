---
name: fulcra-agent-review
description: "Add a review handshake to a fulcra-agent-teams space: request review of an artifact (PR, doc, plan), reviewers leave verdicts, and the overall APPROVED/CHANGES/PENDING state is computed deterministically — including required-reviewer gating."
homepage: "https://github.com/ashfulcra/fulcra-tools"
license: "MIT"
user-invocable: true
metadata: { "openclaw": { "emoji": "🔎" } }
---

# Fulcra Agent Review

Enhances the [`fulcra-agent-teams`](https://github.com/fulcradynamics/agent-skills) skill with a
lightweight **review handshake**: an author requests review of an artifact, one or more reviewers leave
verdicts, and the overall state is folded deterministically. The single-file actions (request, verdict)
are prose over `fulcra-api file` + the teams inbox; the **verdict tally** is a `coord-engine` command
(folding multiple reviewers is a derived state — code, not eyeballing).

## Where to start — the re-entrancy probes

Before requesting a review or leaving a verdict, probe where the handshake already stands. Enter at the
**first probe that fails** (per the repo's skill-quality pattern, `docs/skill-quality-pattern.md`);
requesting is a single-file write and a verdict is an overwrite (re-uploading your verdict file just
supersedes it), so re-entry never corrupts the tally:

| Probe (run in order) | Command | Passes when | If it fails, enter at |
|---|---|---|---|
| Engine + auth usable? | `uv tool run coord-engine doctor <team>` | exits 0 and the last line is exactly `doctor: healthy` | fix engine/auth first (see fulcra-agent-reconcile) — do NOT tally against a broken engine |
| Any reviews owed me? | `uv tool run coord-engine needs-me <team> --agent <me>` | NO `[REVIEW] pending verdict:` row prints for you — no `pending_required` entry names you (NON-mutating read) | **Leave a verdict** — each printed `[REVIEW] pending verdict: <slug> (required: …)` row is an open obligation on you; write your verdict at the echoed path `team/<team>/review/<slug>/verdicts/<me>.md`, then verify + ack per [Lifecycle](#lifecycle) step 2 |
| Known artifact's handshake state settled? | `uv tool run coord-engine review status <team> <slug>` | prints a line beginning `review <slug> in team/<team>:` ending in `APPROVED` or `CHANGES` (deterministic fold — never tally by hand) | if it prints `PENDING`, the review is not settled — chase the `awaiting required:` reviewers per [Lifecycle](#lifecycle) step 3 |

All probes clean → nothing is blocked on your verdict and any artifact you name is at its folded state;
proceed to request a new review or advance an existing one below.

## Layout (under `team/<team>/review/<slug>/`)
- **`review/<slug>.md`** — the review request, written by `review request` (below). OKF `type: Review`.
  `<slug>` is a short id for the artifact (e.g. `pr-42`). The `required` list is what the tally gates on
  (roles preferred — resolved to fresh lease holders):
  ```yaml
  ---
  type: Review
  schema: review-request/v1
  requested_by: ash
  of: https://github.com/org/repo/pull/42
  required: [reviewer, security]   # all must approve for APPROVED (string "a, b" also accepted)
  ts: 2026-07-08T12:00:00Z
  ---
  Review requested: <artifact>
  ```
- **`review/<slug>/verdicts/<reviewer>.md`** — one verdict per reviewer. OKF `type: Verdict`:
  ```yaml
  ---
  type: Verdict
  reviewer: alice
  verdict: approve            # approve | changes
  ---
  Notes / requested changes.
  ```

## Lifecycle
1. **Request** (author) — one command, never a hand-written doc and never a bare `tell`:
   ```bash
   uv tool run coord-engine review request <team> <slug-or-title> \
       --of <artifact> --reviewer <role> [--reviewer <role> …] [--from <me>]
   ```
   `<slug-or-title>` slugs exactly the way a `tell` title does (an already-slug-like arg round-trips
   unchanged); name **roles**, not identities, so `needs-me` resolves the fresh lease holders
   (role-routing doctrine). The command writes `review/<slug>.md` at the exact path the tally reads and
   echoes, per required reviewer, the verdict path to fill:
   ```
   review <slug> requested (required: reviewer, security)
     reviewer reviewer -> file verdict at team/<team>/review/<slug>/verdicts/reviewer.md
   ```
   Requesting the same slug twice is refused (exit 1, `already exists`) rather than clobbering.

   **Why the verb, not a `tell`:** the request doc itself IS the obligation. It lands in every required
   reviewer's `needs-me` as a `pending_required` marker and persists there until that reviewer's verdict
   file exists at the echoed path — the tally folds presence-of-file, so the duty survives sessions,
   hosts, and compaction with no one having to remember it. A bare `tell` is the failure mode this
   replaces: an acked directive leaves **no** durable marker, so a dropped or forgotten review vanishes
   silently and the merge gates on nothing. Never request reviews via `tell`.
2. **Verdict** (reviewer): write `review/<slug>/verdicts/<you>.md` — **slug-exact**, named after **you**
   (the filename is the identity the tally uses) — with `verdict: approve|changes` and notes. Then
   **verify** the fold reflects it (`coord-engine review status <team> <slug>` — you must no longer be in
   `pending_required`) and **only then ack** the request in your inbox
   (`coord-engine inbox <team> --agent <you> --ack <slug>`). Never satisfy a review by acking without a
   verdict file, or against a different slug's status. To change your mind, re-upload your verdict file
   (overwrites; the File Store keeps the history). **Fail-closed:** a `changes` verdict keeps blocking
   until *that reviewer* re-uploads `approve` — pushing a fix does **not** clear it; the reviewer must
   re-affirm.
3. **Check state** (anyone) — deterministic fold, do not tally by hand:
   ```bash
   uv tool run coord-engine review status <team> <slug> --json
   # -> {state: APPROVED|CHANGES|PENDING, approvals, changes, required, pending_required}
   ```
   **CHANGES** if any reviewer requests changes; **APPROVED** if there's an approval, no outstanding
   changes, and all `required` reviewers approved; **PENDING** otherwise.

## When to use
- Gating a merge/land on review in a multi-agent team.
- Any "N reviewers must sign off" flow where you need an unambiguous, non-drifting verdict state.

See [`references/review-cli.md`](references/review-cli.md) for exact commands.
