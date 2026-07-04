---
name: fulcra-agent-review
description: "Add a review handshake to a fulcra-agent-teams space: request review of an artifact (PR, doc, plan), reviewers leave verdicts, and the overall APPROVED/CHANGES/PENDING state is computed deterministically — including required-reviewer gating."
homepage: "https://github.com/ashfulcra/coord2"
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

## Layout (under `team/<team>/review/<slug>/`)
- **`review/<slug>.md`** — the review request. OKF `type: Review`. `<slug>` is a short id for the
  artifact (e.g. `pr-42`). Frontmatter may name required reviewers:
  ```yaml
  ---
  type: Review
  title: Review PR #42 — widget fix
  artifact: https://github.com/org/repo/pull/42
  author: ash
  required: alice, bob        # optional; all must approve for APPROVED
  ---
  What to look at, context, links.
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
1. **Request** (author): write `review/<slug>.md`, then drop a short message into each reviewer's inbox
   (`team/<team>/member/<reviewer>/inbox/<YYYYMMDD-HHMMSS>_<author>_review-<slug>.md`) per the teams
   inbox lifecycle, pointing at the artifact + the review doc.
2. **Verdict** (reviewer): write `review/<slug>/verdicts/<you>.md` (named after **you** — the filename
   is the identity the tally uses) with `verdict: approve|changes` and notes, then drop a message into the
   author's inbox. To change your mind, re-upload your verdict file (overwrites; the File Store keeps the
   history). **Fail-closed:** a `changes` verdict keeps blocking until *that reviewer* re-uploads
   `approve` — pushing a fix does **not** clear it; the reviewer must re-affirm.
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
