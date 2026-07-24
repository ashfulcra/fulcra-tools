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
| Engine + auth usable? | `coord-engine doctor <team>` | exits 0 and the last line is exactly `doctor: healthy` | fix engine/auth first (see fulcra-agent-reconcile) — do NOT tally against a broken engine |
| Any reviews owed me? | `coord-engine needs-me <team> --agent <me>` | NO `[REVIEW] pending verdict:` row prints for you — no `pending_required` entry names you (NON-mutating read) | **Leave a verdict** — each printed `[REVIEW] pending verdict: <slug> (required: …)` row is an open obligation on you; use the exact verdict path echoed by `review request` (head-keyed PR rounds use `verdicts/<head>--<required-token>.md`; legacy/non-code reviews use `verdicts/<required-token>.md`), then verify + ack per [Lifecycle](#lifecycle) step 2 |
| Known artifact's handshake state settled? | `coord-engine review status <team> <slug>` | prints a line beginning `review <slug> in team/<team>:` ending in `APPROVED` or `CHANGES` (deterministic fold — never tally by hand) | if it prints `PENDING`, the review is not settled — chase the `awaiting required:` reviewers per [Lifecycle](#lifecycle) step 3 |

All probes clean → nothing is blocked on your verdict and any artifact you name is at its folded state;
proceed to request a new review or advance an existing one below.

## Layout (under `team/<team>/review/<slug>/`)
- **`review/<slug>.md`** — the review request, written by `review request` (below). OKF `type: Review`.
  `<slug>` is a stable id for the artifact. For a PR it is always `pr-N`, reused
  across pushes; `head` and `round` identify the active exact-head round. The
  `required` list is what the tally gates on (roles preferred — resolved to fresh
  lease holders):
  ```yaml
  ---
  type: Review
  schema: review-request/v2
  requested_by: ash
  of: https://github.com/org/repo/pull/42
  required: [reviewer, security]   # all must approve for APPROVED (string "a, b" also accepted)
  head: 0123456789abcdef0123456789abcdef01234567
  round: 2
  ts: 2026-07-08T12:00:00Z
  ---
  Review requested: <artifact>
  ```
- **`review/<slug>/verdicts/<head>--<required-token>.md`** — one append-only
  verdict per requirement and exact PR head. The suffix after `<head>--` is the
  tally key and must equal a `required` token (the role, or direct agent name),
  not the holder's own name. The frontmatter repeats the exact head independently.
  Legacy/non-code reviews without `--head` retain
  `verdicts/<required-token>.md`. OKF `type: Verdict`:
  ```yaml
  ---
  type: Verdict
  reviewer: alice             # who signed off (informational — the FILENAME drives the tally)
  head: 0123456789abcdef0123456789abcdef01234567
  verdict: approve            # approve | changes
  ---
  Notes / requested changes.
  ```

## Lifecycle
1. **Request** (author) — one command, never a hand-written doc and never a bare `tell`:
   ```bash
   coord-engine review request <team> <slug-or-title> \
       --of <artifact> [--head <exact-sha>] \
       --reviewer <role> [--reviewer <role> …] [--from <me>]
   ```
   For a PR, use one stable slug (`pr-N`), the PR URL as `--of`, and its full
   40- or 64-hex commit id as `--head`. Re-requesting the same slug/PR/requester/
   required-set with a new head advances the same review doc to the next round;
   prior verdicts remain append-only and only the active head tallies. An identical
   head is idempotent recovery. Name **roles**, not identities, so `needs-me`
   resolves fresh lease holders. The command writes `review/<slug>.md` and echoes
   each exact verdict path:
   ```
   review <slug> requested (required: reviewer, security)
     reviewer reviewer -> file verdict at team/<team>/review/<slug>/verdicts/<head>--reviewer.md
   ```
   An identical re-request is idempotent recovery. For a head-keyed PR, a new
   exact head advances the same slug; a different artifact/requester/required set
   is refused rather than clobbering the existing review.

   **Why the verb, not a `tell`:** the request doc itself IS the obligation. It lands in every required
   reviewer's `needs-me` as a `pending_required` marker and persists there until that reviewer's verdict
   file exists at the echoed path — the tally folds presence-of-file, so the duty survives sessions,
   hosts, and compaction with no one having to remember it. A bare `tell` is the failure mode this
   replaces: an acked directive leaves **no** durable marker, so a dropped or forgotten review vanishes
   silently and the merge gates on nothing. Never request reviews via `tell`.
2. **Verdict** (reviewer): write the verdict file at the **exact path `review request` echoed** for you —
   **slug-exact**, with the required token encoded after `<head>--` (or as the
   whole legacy filename), not your own name. That path token is what the tally
   matches, not the frontmatter `reviewer:` field:
   - **role requirement** (`required: reviewer`) →
     `review/<review-slug>/verdicts/<head>--reviewer.md`, whoever holds the role.
   - **direct requirement** (`required: alice`) →
     `review/<review-slug>/verdicts/<head>--alice.md`.
   Include the same exact `head:` in the verdict frontmatter. A mismatched or
   missing head cannot discharge a head-keyed round.

   Write it with `verdict: approve|changes` and notes. The **verdict file is what discharges the
   obligation** (the tally folds presence-of-file). Then **verify** the fold reflects it (`coord-engine
   review status <team> <review-slug>` — that requirement must no longer be in `pending_required`) and
   **only then ack** the accompanying directive as inbox hygiene — using the **directive** id, NOT the
   `<review-slug>`: the review-request directive has its own slug `review-request-<review-slug>-<hash>`, so
   ack that (read it from `coord-engine inbox <team> --agent <you> --json` — the `name` of the `REVIEW
   REQUEST: <review-slug>` row), never `--ack <review-slug>` (which the directive would never match,
   leaving it re-notifying). Never satisfy a review by acking without a verdict file, or against a
   different review's status. To change your mind, re-upload the same file (overwrites; the File Store
   keeps the history). **Fail-closed:** a `changes` verdict keeps blocking until that same file is
   re-uploaded as `approve` — pushing a fix does **not** clear it; the requirement must be re-affirmed.
3. **Check state** (anyone) — deterministic fold, do not tally by hand:
   ```bash
   coord-engine review status <team> <slug> --json
   # -> {state: APPROVED|CHANGES|PENDING, approvals, changes, required, pending_required}
   ```
   **CHANGES** if any reviewer requests changes; **APPROVED** if there's an approval, no outstanding
   changes, and all `required` reviewers approved; **PENDING** otherwise.

   A review round that reaches **APPROVED** with every `required` verdict in is *settled*: the fold caches it
   at `verdicts/.settled` so the fan-out folds (`briefing`/`needs-me`) skip it. Settled reviews are
   immutable at that head; a new exact head clears the cache and advances the same
   PR slug, while a changed artifact/requester/required list needs a **new slug**.
   `review status` never trusts the marker: it recomputes the active-head tally on
   every call, so a stale or wrong marker self-heals on direct query.

   `review status` **exits 1** with `... unreadable (missing slug or degraded transport) — tally unknown,
   retry` when the review doc can't be read — a transport failure or a nonexistent slug, indistinguishable
   and both UNKNOWN (without the `required` list a lone approval would tally as a clean APPROVED and
   durably hide a pending review). A watcher must read rc 1 as *transport down, retry*, never as a state.

   **Nudge only against a live obligation.** Before nudging a reviewer about a pending review, run
   `review status <team> <slug> --json` on the *exact* slug and nudge only if `pending_required` still
   names that assignee — a verdict may have landed since you last looked, and a stale nudge is noise that
   trains reviewers to ignore the real ones. rc 1 is *transport down, retry* (not a settled state), so
   never take an unreadable tally as "no longer pending" and suppress a legitimate nudge on it.

## When to use
- Gating a merge/land on review in a multi-agent team.
- Any "N reviewers must sign off" flow where you need an unambiguous, non-drifting verdict state.

See [`references/review-cli.md`](references/review-cli.md) for exact commands.
