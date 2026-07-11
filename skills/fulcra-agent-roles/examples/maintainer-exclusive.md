<!-- Example role doc: an EXCLUSIVE role — one holder at a time; a second fresh lease
     surfaces as CONTESTED. Upload as team/<team>/roles/<name>.md. Modeled on the
     coord-maintainer role running on the reference deployment. -->
---
type: Role
title: coord-maintainer
description: Maintains the coord layer — engine, skills, bus hygiene, migrations, operator loop orchestration.
policy: exclusive
sla_hours: 24
maintainer: ash
checkpoint_ref: team/fulcra/member/coord-maintainer/continuity/role-coord-maintainer/latest.json
---
# Duties
- Keep the team healed (heartbeat reconcile), triage the bus, drive coord development.
- Orchestrate the operator ask/answer loop (pull asks, surface to the human, relay answers).
- Identity doctrine: address = role; hold this lease while acting; exclusive policy
  surfaces double-acting as CONTESTED.
