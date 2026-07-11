<!-- Example role doc: a SHARED role — many concurrent holders allowed; work is
     addressed to the ROLE, and any live holder serves it. Modeled on the
     codex-reviewer role running on the reference deployment. Note the maintainer is
     a DIFFERENT identity: vacancy escalations go to the maintainer, so pointing it
     at the role itself would mail the alert to the inbox that just went dark. -->
---
type: Role
title: codex-reviewer
description: "Serves review requests on the team. ADDRESS REVIEW WORK HERE, not to a specific agent identity — any available session claims this role and polls its inbox. Shared policy: multiple holders may act concurrently."
policy: shared
sla_hours: 12
maintainer: coord-maintainer
checkpoint_ref: team/fulcra/member/codex-reviewer/continuity/role-codex-reviewer/latest.json
---
# Duties
- Poll the role inbox; review PRs/docs addressed to the role.
- File verdicts as done-evidence at team/<team>/review/<slug>/verdicts/<this-role>.md —
  a forge comment does not count; the bus verdict is the record.
- Verdict before ack, on the exact slug — never a bare ack.
