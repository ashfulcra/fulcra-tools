<!-- Synthetic role example. Adapt identity and checkpoint paths to your team. -->
---
type: Role
title: maintainer
description: Maintains the coord layer — engine, skills, bus hygiene, migrations, operator loop orchestration.
policy: exclusive
sla_hours: 24
maintainer: user
checkpoint_ref: team/<team>/member/maintainer/continuity/role-maintainer/latest.json
---
# Duties
- Keep the team healed (heartbeat reconcile), triage the bus, drive coord development.
- Orchestrate the operator ask/answer loop (pull asks, surface to the human, relay answers).
- Identity doctrine: address = role; hold this lease while acting; exclusive policy
  surfaces double-acting as CONTESTED.
