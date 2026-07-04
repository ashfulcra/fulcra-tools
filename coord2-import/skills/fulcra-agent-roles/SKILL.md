---
name: fulcra-agent-roles
description: "Add durable roles to a fulcra-agent-teams space: agents claim leases on named roles (reviewer, maintainer, on-call), liveness is tracked, and a role left vacant past its SLA escalates to its maintainer."
homepage: "https://github.com/ashfulcra/coord2"
license: "MIT"
user-invocable: true
metadata: { "openclaw": { "emoji": "🎓" } }
---

# Fulcra Agent Roles

Enhances the [`fulcra-agent-teams`](https://github.com/fulcradynamics/agent-skills) skill. A team's
`member/<agent>/role.md` says what a *member* does, but teams has no notion of a **durable role** that
outlives any one session — "who is the reviewer right now?", "is anyone on-call?", "this role has been
unattended too long." This skill adds that, as a pure OKF-markdown convention over the team namespace
(lease mechanics via `coord-engine roles` verbs; everything else plain `fulcra-api file` + the OKF
standard).

## Concepts
- **Role** — a named, durable function in the team (e.g. `reviewer`, `maintainer`, `on-call`). Defined
  once; sessions come and go.
- **Lease** — an agent's claim on a role, refreshed to prove liveness. A role is *held* while a fresh
  lease exists.
- **Policy** — `shared` (many holders allowed) or `exclusive` (one holder; a second fresh lease is a
  contention signal).
- **SLA / escalation** — if a role sits vacant longer than `sla_hours`, its `maintainer` is notified.

## Layout (under `team/<team>/roles/`)
- **`roles/<name>.md`** — the role registry doc. OKF `type: Role`. Created once when the role is
  established. Frontmatter carries the policy and SLA:
  ```yaml
  ---
  type: Role
  title: Reviewer
  description: Adversarial code/plan review for the team's PRs.
  policy: shared            # shared | exclusive
  sla_hours: 24             # vacancy longer than this escalates
  maintainer: ash           # who gets the escalation (an agent or member name)
  ---
  # Duties
  - Pick up review requests from the team inbox…
  ```
- **`roles/<name>/leases/<slug>-<hash6>.md`** — one lease per holder, named by the engine from the
  holder's id (`agent_key`); never hand-name lease files. OKF `type: Lease`. The `timestamp` is the
  liveness signal — **refresh it** (re-claim) each time you act in the role:
  ```yaml
  ---
  type: Lease
  title: reviewer lease — treecle
  agent: treecle
  timestamp: 2026-07-01T18:00:00Z
  ---
  Holding the reviewer role. Next: drain the review inbox.
  ```
- **`roles/<name>/escalations/<YYYY-MM-DD>.md`** — a first-writer-wins daily marker so a vacant role
  escalates at most once per day (avoids spamming the maintainer).

## Lifecycle

### Establish a role (once)
Write `roles/<name>.md` with `type: Role` + policy/SLA/maintainer, and list it in the team `roles/index.md`.

### Claim / hold
`uv tool run coord-engine roles claim <team> <name>` writes your lease shard (engine-named `<slug>-<hash6>.md`;
the command echoes the filename). **Re-run it** whenever you do work in the role — the refreshed
`timestamp` is what keeps the role "held". Never hand-upload a lease file: a hand-named shard makes a
SECOND lease for your id (spurious CONTESTED on exclusive roles). The Fulcra File Store versions every
write, so the lease's history is an audit trail of your tenure.

### Release
`uv tool run coord-engine roles release <team> <name>` deletes your engine-named shard. (Deletion is intentional
and not undoable — correct for releasing.)

### Determine role status (the fold) — **use the engine, do not eyeball timestamps**
Classifying a role from many lease files is a *fold* over derived state: two agents must AGREE on
whether a role is vacant before one escalates. Eyeballing timestamps drifts (the exact failure coord
exists to prevent), so this is a deterministic **`coord-engine`** command, not a prose instruction:
```bash
uv tool run coord-engine roles status <team> <role> --json
```
It reads the role's `policy`/`sla_hours`, folds the leases, and returns:
- `status` — **HELD** (≥1 fresh lease) / **VACANT** (none) / **CONTESTED** (`exclusive` + ≥2 fresh) / **UNKNOWN** (unreadable),
- `fresh_holders`, and `escalation_due` (true iff vacant past SLA and today's marker isn't present).

For **CONTESTED**, resolve by having all but one holder release.

### Role-as-identity (recommended)
When a session exists to serve one role, use the role name AS its agent identity
(`FULCRA_COORD_AGENT=coord-maintainer`) — see fulcra-agent-presence's "Pick your identity by ROLE"
section. Claim the role's lease while you act as it. Know what each guard does and does not catch:

- **Different ids claiming an exclusive role** (e.g. `coord-maintainer` and a stray
  `claude-code:host:repo`): two FRESH lease shards (within `sla_hours`) → `roles status` reports
  **CONTESTED**. A stale stray shard yields HELD, not CONTESTED. Detected.
- **Two sessions under the SAME id string**: they write the SAME lease shard (shard names derive from
  the id), so leases alone CANNOT see this — last write silently wins. Since the
  session-nonce verify was shipped, the engine detects this automatically: every `roles claim`
  writes a session nonce into the lease and compares on refresh — a foreign nonce prints a loud
  stderr WARNING ("nonce mismatch ... same-id double-acting"), and claiming with no local state over
  an existing shard prints a takeover note. Heed those. The manual fallback, in this order at the start
  of every work burst: (1) `roles status <team> <role> --json` — proceed only if VACANT or the sole
  holder is your id; (2) read your lease shard raw (`fulcra-api file download
  team/<team>/roles/<role>/leases/<agent-key>.md` — learn your `<agent-key>` by listing the leases
  dir, or from `presence beat` output, which prints the same key) and compare its `timestamp` to when YOU last
  claimed — a fresher timestamp you did not write means another session is acting under your id;
  (3) only then re-claim to refresh. Re-claiming FIRST destroys that evidence.

Multi-host variants (`coord-maintainer@host1`, `@host2`) are acceptable when one role legitimately
runs in several places — each host claims the SAME role (`roles claim <team> coord-maintainer --agent
coord-maintainer@host1`), never a role named after the variant. Such a role needs `policy: shared`:
on `exclusive` it would sit in permanent CONTESTED by construction — and note `shared` trades away
the CONTESTED collision guard for that role. Keep the role doc's `maintainer:` field
a distinct SUPERVISING identity (e.g. `maintainer: ash`): vacancy escalations are assigned to that
field, so pointing it at the role itself mails the alert to the very inbox that just went dark.

### Escalate a vacancy — engine decides, you act
The engine already computed `escalation_due` above. When it is **true**, perform the single-file actions
(these are reliable as prose):
1. Write today's dedupe marker `roles/<name>/escalations/<date>.md` (first-writer-wins).
2. Drop a message into the maintainer's inbox
   (`team/<team>/member/<maintainer>/inbox/<YYYYMMDD-HHMMSS>_<you>_role-vacant-<name>.md`) per the
   `fulcra-agent-teams` inbox lifecycle, stating which role is vacant and for how long.

## When to use
- Establishing "someone owns X" in a team without pinning it to one session.
- Routing work by role ("the reviewer") instead of by name.
- Making sure a critical function (on-call, maintainer) is never silently unattended.

## Efficiency (per the teams OKF directive)
List roles in `roles/index.md`, but do **not** index every lease or escalation marker — describe the
`leases/` and `escalations/` directories as a whole. Keep the team `log.md` for role *creation* and
*handoff* milestones, not every lease refresh.

See [`references/roles-cli.md`](references/roles-cli.md) for exact commands.
