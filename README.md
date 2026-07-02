# coord2

**Agent coordination as optional layers on top of [`fulcra-agent-teams`](https://github.com/fulcradynamics/agent-skills).**

coord2 is a ground-up rebuild of `fulcra-coord` + `fulcra-continuity` as a family of **sibling
`fulcra-agent-*` skills** that enhance Fulcra's official (alpha) `fulcra-agent-teams` skill — each a
pure-prose SKILL backed by **one shared tool, `coord-engine`**, for the deterministic parts (the
consistency-critical folds that prose would drift on). Not a parallel system; not pip packages to
version-sync across a fleet.

Bare `fulcra-agent-teams` is a lightweight OKF-markdown convention over the Fulcra File Store
(per-agent inboxes, `progress.md` continuity, consent-gated automation). coord2 keeps that as the
**base tier** and adds, à la carte, the machinery that a convention structurally lacks: typed task
lifecycle, self-healing queryable views, roles + leases, a review handshake, a VCS bridge, structured
resumable continuity, and OS-level wake automation.

## Why
The incumbent [`fulcra-tools-coord`](https://github.com/ashfulcra/fulcra-tools) (v0.15.x) is a powerful
but heavyweight parallel system — a dedicated CLI + JSON store + reconcile engine that must be kept
version-synced across a fleet. coord2's thesis: make the **official skill the substrate** and coord the
opt-in **"pro tier."** Bare-teams agents interoperate by reading the same markdown; power agents opt into
the layers they want. OKF v0.1 explicitly permits the typed frontmatter and synthesized views this needs.

## Architecture
Each capability is a **skill** (agent-facing interface + genuinely-conventional prose); the
consistency-critical folds live in the shared **`coord-engine`** tool the skills invoke via
`uv tool run coord-engine …` (the same way skills already invoke `fulcra-api`). Decision + rationale:
[`docs/proposals/teams-convergence/03-packaging-decision.md`](docs/proposals/teams-convergence/03-packaging-decision.md).

| Layer | Skill | Engine command(s) | Adds |
|---|---|---|---|
| L0 | `fulcra-agent-teams` (external, base) | — | OKF-markdown team spaces, inboxes, continuity files |
| **L1 ✅** | **`fulcra-agent-reconcile`** | `reconcile`/`status`/`board`/`needs-me`/`search` | self-healing `task/index.md`+`log.md`, queryable views |
| **L4 ✅** | **`fulcra-agent-roles`** | `roles status` | roles + leases + HELD/VACANT/CONTESTED fold + SLA escalation-due |
| **L2 ✅** | **`fulcra-agent-tasks`** | `task start/update/done` | typed status/priority/assignee lifecycle + validated state machine |
| **L5 ✅** | **`fulcra-agent-review`** | `review status` | review handshake + APPROVED/CHANGES/PENDING verdict tally |
| **L6 ✅** | **`fulcra-agent-continuity`** | `continuity snapshot/resume` | structured resumable snapshots + deterministic resume brief |
| **L7 ✅** | **`fulcra-agent-automation`** | (schedules `reconcile`) | heartbeat scheduler + resume-on-wake loop |

Reconcile is the linchpin (queryability + self-healing). The roles fold and reconcile share the engine's
OKF parser + transport — one implementation, no drift.

## Install (standalone)

coord2 installs from a checkout — the engine and skills come from the same checkout, so they're always
version-matched. Matches how [`fulcra-agent-teams`](https://github.com/fulcradynamics/agent-skills) is
installed (copy the skill folders into your agent's skills dir).

```bash
git clone https://github.com/ashfulcra/coord2 && cd coord2
scripts/coord2-setup.sh              # installs coord-engine (from ./engine) + copies the 6 skills into ~/.claude/skills/
# options: --symlink (dev; git pull updates in place) · --skills-dir DIR (other agents) · --engine-only/--skills-only · --uninstall
```
Prereqs: `uv`, and an authenticated `fulcra-api` (`uv tool install fulcra-api && fulcra-api auth login`) —
the setup self-test checks for both. Engine-only, no checkout:
`uv tool install "git+https://github.com/ashfulcra/coord2.git@v0.4.0#subdirectory=engine"`.

Publishing to PyPI / a plugin registry is deferred to the upstream phase (see
`docs/proposals/teams-convergence/04-standalone-packaging.md`).

## Status
**`coord-engine` v0.4.0 + **all six skills built** — reconcile, roles, tasks, review, continuity, automation. **105 engine tests** + a live end-to-end run of reconcile/queries against the real
Fulcra File Store. Packaging decided (approach C) after independent + bus (Codex) review — both reviewers
independently flagged the roles-fold-as-prose defect, now fixed; open decisions resolved in doc `03`.
The full stack is in place.

**Foundations validated (2026-07-01):** `fulcra-api file` is last-writer-wins + versions every upload
(`stat` → version UUID + history; `restore` rolls back live files), `list` timestamps are minute-granular
(incremental uses a conservative compare), `file` output is text (line-parsed), `delete` isn't
CLI-undoable (archival = move-not-delete). The upstream skills model uses **no pip/wheels** — skills
invoke shared tools as external commands (`uv tool run fulcra-api`), which is why `coord-engine` ships the
same way.

## Layout
- `engine/` — **`coord-engine`**, the shared stdlib-only tool (OKF parse/render, transport, reconcile,
  role fold, queries). Tested. `cd engine && uv run --extra dev pytest`.
- `skills/fulcra-agent-*/` — the pure-prose skills (`SKILL.md` + `references/`) that invoke `coord-engine`.
- `docs/proposals/teams-convergence/` — the design set (start at its `README.md`).

## Conventions
Python, **stdlib-only** in `coord-engine` (transport is a CLI-subprocess call to `fulcra-api`, so no heavy
deps). Best-effort / never-raise on the reconcile path. Skills are prose + references, no bundled code.
TDD; every stateful fold is engine-side + tested, never prose the agent eyeballs.

## License
MIT © 2026 Fulcra Dynamics
