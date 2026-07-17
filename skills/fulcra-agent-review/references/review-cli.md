---
name: fulcra-agent-review-cli
description: "Exact commands for the review handshake: the request verb, the verdict file, and the coord-engine tally."
---

# Fulcra Agent Review — CLI reference

The whole handshake is `coord-engine`. **Requesting a review is ONE verb** —
`coord-engine review request` — never a hand-written doc and never a bare `tell`.
Leaving a verdict is a single file at the path the request echoes; the tally
(`review status`) is deterministic. See [`SKILL.md`](../SKILL.md) for when/why;
this file is the exact commands.

## Request review (author) — one command
```bash
coord-engine review request <team> <slug-or-title> \
    --of <artifact> --reviewer <role> [--reviewer <role> …] [--from <me>]
```
- `<slug-or-title>` slugs exactly as a `tell` title does (an already-slug-like arg
  round-trips unchanged) — the result is the **`<review-slug>`** you use in
  `review status` and the verdict path. `<artifact>` is an opaque ref —
  PR#/branch/commit SHA/URL or a non-code deliverable — so the handshake works with
  any forge or none.
- Name **roles**, not identities (`--reviewer reviewer`, not a session id), so
  `needs-me`/`briefing` resolve the fresh lease holders (role-routing doctrine).

**One command; durable-first; idempotently recoverable — not a single atomic
write.** The verb does two things in order: (1) it writes the durable review doc
`review/<review-slug>.md` FIRST — that doc IS the obligation, it lands in every
required reviewer's `needs-me` as a `pending_required` marker and persists there
until that reviewer's verdict file exists, and the tally reads it; then (2) it
delivers one directive per required reviewer through the canonical hash-slug inbox
path, so a verb-opened review fires each reviewer's `inbox`/`listen`. The two are
**not** one atomic operation: a partial notification failure is an expected,
loudly-reported **rc 1** that idempotent re-request recovers (below). The doc
landing first is exactly what makes recovery safe — the obligation exists the
moment the doc lands, whether or not every directive got out. Never hand-write the
doc, and never notify a reviewer with a bare `tell` — an acked directive leaves no
durable marker, so a dropped review vanishes silently and the merge gates on
nothing (the exact failure this verb was built to kill).

The command echoes, per required reviewer, the verdict path to fill (it prints the
`<review-slug>` and verdict paths — NOT the inbox directive id; for that, see the
ack step below):
```
review <review-slug> requested (required: reviewer, security)
  reviewer reviewer -> file verdict at team/<team>/review/<review-slug>/verdicts/reviewer.md
await verdicts: coord-engine listen <team> --agent <me>
```

### Recovery semantics (idempotent, fail-closed)
- **Partial notify** — if the doc lands but a reviewer directive fails to deliver,
  the command reports **rc 1** naming exactly which reviewers were and were not
  notified. **Re-run the SAME request** (same `--of` / `--reviewer` set / `--from`):
  it is idempotent recovery — the doc is left byte-unchanged, already-delivered
  directives dedupe (rc 0), and only the dropped reviewers are re-notified. No
  reviewer is stranded by the exists-guard.
- **Conflicting re-request** — re-running with a *different* `--of`, required set,
  or requester is a loud **rc 1 conflict**; it never clobbers the existing doc. A
  changed required set re-opens only via a **new slug**.
- **Unreadable doc** — a present-but-unreadable request doc fails closed (rc 1,
  never overwritten).

## Leave a verdict (reviewer) — the verdict FILE discharges it; ack is inbox hygiene
**The verdict file is what clears the obligation** — the tally folds
presence-of-file, so writing it (and only it) is what removes you from
`pending_required`. Acking is separate: it just clears the directive from your
`inbox`/re-notify, and it does **not** substitute for the verdict.

1. **Write your verdict** at the **slug-exact** path the request echoed. The verdict
   FILENAME STEM must equal the `required` token the request named — that stem is what
   the tally matches, NOT the frontmatter `reviewer:` field — so name the file after
   the requirement, not yourself:
   - **role requirement** (`required: reviewer`) → `verdicts/reviewer.md`, whoever
     holds the role (`verdicts/alice.md` records an approval the tally can't credit —
     `reviewer` stays in `pending_required` forever).
   - **direct requirement** (`required: alice`) → `verdicts/alice.md`.
   ```bash
   # filename = the required token, e.g. reviewer.md — type: Verdict, verdict: approve|changes
   fulcra-api file upload /tmp/verdict.md \
     "team/<team>/review/<review-slug>/verdicts/reviewer.md"
   ```
   ```yaml
   ---
   type: Verdict
   reviewer: <you>             # who signed off (informational — the FILENAME drives the tally)
   verdict: approve            # approve | changes
   ---
   Notes / requested changes.
   ```
2. **Verify** the fold reflects it — you must no longer be in `pending_required`:
   ```bash
   coord-engine review status <team> <review-slug>
   ```
3. **Ack the DIRECTIVE (inbox hygiene) — using the directive id, NOT the
   `<review-slug>`.** The review-request directive has its own slug,
   **`review-request-<review-slug>-<hash>`** (`<directive-slug>`), distinct from
   `<review-slug>`. Acking `<review-slug>` writes an ack shard the directive never
   matches, so the directive keeps re-notifying and looks unpicked-up. Read the
   exact id from your inbox — the text line shows the title `REVIEW REQUEST:
   <review-slug>`, so use `--json` to copy the row's `name`:
   ```bash
   coord-engine inbox <team> --agent <you> --json     # find the REVIEW REQUEST row; copy its "name"
   coord-engine inbox <team> --agent <you> --ack <directive-slug>   # e.g. review-request-pr-42-1a2b3c4d
   ```

Never ack without a verdict file, or against a different review's status. To change
your mind, re-upload your verdict file (last wins; the File Store keeps history).
**Fail-closed:** a `changes` verdict keeps blocking until *that reviewer* re-uploads
`approve` — pushing a fix does not clear it.

## Check state (deterministic — do not tally by hand)
```bash
coord-engine review status <team> <review-slug> --json
# {state: APPROVED|CHANGES|PENDING, approvals:[...], changes:[...], required:[...], pending_required:[...]}
```
- **CHANGES** — any reviewer requested changes (a single blocker dominates).
- **APPROVED** — ≥1 approval, no outstanding changes, and every `required` reviewer approved.
- **PENDING** — otherwise (no verdicts yet, or required reviewers haven't voted).

Verdict synonyms accepted: `approve|approved|lgtm` and `changes|request-changes|reject`.

A review that reaches APPROVED with every `required` verdict in is *settled* — the
fold caches `verdicts/.settled` so the fan-out folds (`briefing`/`needs-me`) skip
it. Settled reviews are immutable; re-opening under a changed `required` list is a
**new slug**. `review status` never trusts the marker — it recomputes the full
tally every call, so a stale marker self-heals on direct query.

### The rc-1 register a watcher parses
`review status` **exits 1** rather than ever printing a partial state:
- `... unreadable (missing slug or degraded transport) — tally unknown, retry` —
  the doc, the verdicts *listing*, or a verdict shard couldn't be read. UNKNOWN,
  **retryable**: read it as *transport down, retry*, never as a state (without the
  `required` list a lone approval would tally as a clean APPROVED and durably hide
  a pending review).
- `... tombstone (archived/deleted review) — no doc, no verdicts` — the slug is a
  soft-delete tombstone (a `<review-slug>/` dir with no `<review-slug>.md`).
  **Terminal**: a retry never resurrects it.

The `, retry` suffix means retryable; a `tombstone` mention means terminal — the
convention is load-bearing, so match on it, not on the whole string.

**Nudge only against a live obligation.** Before nudging a reviewer, re-run
`review status <team> <review-slug> --json` on the exact review slug and nudge only if
`pending_required` still names them — a verdict may have landed since you looked,
and a stale nudge trains reviewers to ignore the real ones. rc 1 is *retry*, not
"no longer pending" — never suppress a legitimate nudge on an unreadable tally.
