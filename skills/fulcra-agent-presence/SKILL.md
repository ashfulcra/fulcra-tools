---
name: fulcra-agent-presence
description: "Add presence + liveness to a fulcra-agent-teams space: agents heartbeat a presence shard, and deterministic folds answer who's live/idle/stale, what each agent is working on, and who a broadcast must reach."
homepage: "https://github.com/ashfulcra/fulcra-tools"
license: "MIT"
user-invocable: true
metadata: { "openclaw": { "emoji": "📡" } }
---

# Fulcra Agent Presence

Enhances [`fulcra-agent-teams`](https://github.com/fulcradynamics/agent-skills). Teams knows who its
*members* are (prose in `index.md`) but not who is **alive right now**. This skill adds a presence
heartbeat + deterministic liveness folds — the roster that directives' broadcast semantics, the operator
digest, and role-vacancy escalation all build on. Optional: without it, everything else still works;
broadcasts just degrade to "acked-by-me hides it for me".

## Where to start — the re-entrancy probes

Before beating or reading the roster, probe how far this session already got. Enter at the **first probe
that fails** (per the repo's skill-quality pattern, `docs/skill-quality-pattern.md`); every step here is
safely re-runnable (a beat is a single-file overwrite, a claim is a refresh):

| Probe (run in order) | Command | Passes when | If it fails, enter at |
|---|---|---|---|
| Engine usable? | `uv tool run coord-engine doctor <team>` | exits 0 and the last line is exactly `doctor: healthy` | fix engine/auth first (see fulcra-agent-reconcile) — do NOT beat against a broken engine |
| Own shard live? | `uv tool run coord-engine presence show <team>` | a row for your agent id shows liveness `[live ]` (the `<1h` bucket) | **Beat** — run `presence beat <team>` (see Usage) to write/refresh your shard |
| If identity=role, lease held? | `uv tool run coord-engine roles status <team> <role>` | prints `role <role> in team/<team>: HELD` for the role you act as | **Claim** — run `roles claim <team> <role>` (skip this probe entirely if you are not acting under a role identity) |

All probes pass → you are present and (if role-scoped) holding your lease; just keep beating on your
cadence. A brand-new session fails the first or second probe and enters at Beat.

## How it works
- **Beat** (single-file write, safe as a command): `presence beat` writes/refreshes your shard
  `team/<team>/presence/<agent-key>.md` (collision-safe key) (OKF `type: Presence`: agent, workstreams, summary, timestamp).
  Beat whenever you start work, on heartbeat/cron ticks, and when your focus changes.
- **Folds** (deterministic, engine-side — never eyeball timestamps):
  - `presence show` — roster with `live` (<1h) / `idle` (<24h) / `stale` per agent.
  - `agents` — cross-agent digest: each agent's liveness, summary, and open work by status
    (union of presence ∪ task owners/assignees from the reconcile aggregate).
  - Roles: `roles claim <team> <role>` writes/refreshes your lease shard; `roles release` deletes it;
    `roles status` folds HELD/VACANT/CONTESTED (see fulcra-agent-roles).

## Usage
```bash
uv tool run coord-engine presence beat <team> [--agent X] [-w workstream]... [-s "one-liner"]
uv tool run coord-engine presence show <team> [--json]
uv tool run coord-engine agents <team> [--json]
uv tool run coord-engine roles claim <team> <role> [--agent X]     # refresh = re-run
uv tool run coord-engine roles release <team> <role> [--agent X]
```
`--agent` defaults to `$FULCRA_COORD_AGENT` (or a derived host id). Stale shards drop out of the
presence fold's `[live]` view by age; the shard FILES are not currently garbage-collected (reconcile's
GC covers ack and health shards only). A stale agent reappears by simply beating again.

## Pick your identity by ROLE, not by folder

Set `FULCRA_COORD_AGENT` to the role you are acting as (`coord-maintainer`, `prefs-maintainer`,
`release-reviewer`), not a host/cwd-derived string. Folder-derived ids collide the moment two sessions
share a directory (shared inbox, clobbered presence, ambiguous acks) and rot when a hostname or checkout
path changes; a role-based id survives both and is what teammates actually want to address. Two rules
make it safe:

1. **Claim the role's lease while you act as it** (`roles claim <team> <role>`; see
   fulcra-agent-roles). An `exclusive` role turns two sessions acting as the same role under
   DIFFERENT ids into a visible CONTESTED state. It cannot see two sessions sharing one id string
   (same lease shard, last write wins) — see the roles skill's "Role-as-identity" guard matrix for
   the procedural check that covers that case.
2. **Session/host details are metadata, not address.** Put them in the presence `-s` summary or the
   lease body if useful; never in the agent id.

The derived host id remains only as a fallback for throwaway/anonymous sessions that never take
assignments — and note it is per-HOST, not per-session (`coord-reconcile:<hostname>`), so two env-less
sessions on one machine still share an id and clobber each other's shards. Any session that acts on the
bus should set an explicit role id.
