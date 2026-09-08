<!-- Synthetic role example. Adapt identity and checkpoint paths to your team. -->
---
type: Role
title: reviewer
description: "Serves review requests on the team. ADDRESS REVIEW WORK HERE, not to a specific agent identity — any available session claims this role and polls its inbox. Shared policy: multiple holders may act concurrently."
policy: shared
sla_hours: 12
maintainer: maintainer
checkpoint_ref: team/<team>/member/<agent>/continuity/role-<role>/latest.json
---
# Duties
- Poll the role inbox; review PRs/docs addressed to the role.
- File verdicts as done-evidence at team/<team>/review/<slug>/verdicts/<this-role>.md —
  a forge comment does not count; the bus verdict is the record.
- Verdict before ack, on the exact slug — never a bare ack.
