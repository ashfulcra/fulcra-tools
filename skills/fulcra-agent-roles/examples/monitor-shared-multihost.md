<!-- Example role doc: a shared role legitimately held from SEVERAL hosts at once.
     Each host claims the SAME role with a host-qualified agent id
     (`roles claim <team> fleet-monitor --agent fleet-monitor@host1`) — never a role
     named after the host. Such a role MUST be `shared`: on `exclusive` it would sit
     in permanent CONTESTED by construction. The trade: `shared` gives up the
     CONTESTED collision guard for this role. -->
---
type: Role
title: fleet-monitor
description: Watches presence, open loops, and role SLAs across the team; surfaces blockers and dropped threads; runs the daily escalate sweep. May run concurrently from several hosts.
policy: shared
sla_hours: 24
maintainer: ash
---
# Duties
- Periodic active sweep: `coord-engine escalate <team>` (idempotent per day) and
  `roles status` on load-bearing roles — `listen` does not surface vacancy.
- Beat presence with a summary of what is being watched; keep the lease fresh from
  each host that participates.
