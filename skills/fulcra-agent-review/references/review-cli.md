---
name: fulcra-agent-review-cli
description: "Exact commands for the review handshake: request, verdict, and the coord-engine tally."
---

# Fulcra Agent Review — CLI reference

Requesting review and leaving a verdict are `fulcra-api file` uploads (needs `fulcra-api auth login`);
the tally is `coord-engine`.

## Request review (author)
```bash
# 1. the review doc (type: Review; optional `required:` reviewers in frontmatter)
uv tool run fulcra-api file upload /tmp/review.md "team/<team>/review/pr-42.md"
# 2. notify each reviewer via the teams inbox lifecycle
uv tool run fulcra-api file upload /tmp/notice.md \
  "team/<team>/member/<reviewer>/inbox/$(date -u +%Y%m%d-%H%M%S)_<author>_review-pr-42.md"
```

## Leave a verdict (reviewer)
```bash
# type: Verdict, reviewer: <you>, verdict: approve|changes
uv tool run fulcra-api file upload /tmp/verdict.md \
  "team/<team>/review/pr-42/verdicts/<you>.md"
# then notify the author's inbox (same lifecycle). Re-upload to change your verdict (last wins).
```

## Check state (deterministic — do not tally by hand)
```bash
coord-engine review status <team> pr-42 --json
# {state: APPROVED|CHANGES|PENDING, approvals:[...], changes:[...], required:[...], pending_required:[...]}
```
- **CHANGES** — any reviewer requested changes (a single blocker dominates).
- **APPROVED** — ≥1 approval, no outstanding changes, and every `required` reviewer approved.
- **PENDING** — otherwise (no verdicts yet, or required reviewers haven't voted).

Verdict synonyms accepted: `approve|approved|lgtm` and `changes|request-changes|reject`.
