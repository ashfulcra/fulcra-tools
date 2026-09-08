# Migration — moving off `fulcra-tools-coord` onto coord

> **📎 HISTORICAL — design provenance, not current instructions.** Part of the
> [teams-convergence proposal set](README.md), superseded by what shipped
> (`packages/coord-engine` + `skills/fulcra-agent-*`). Read it for the reasoning,
> not for runnable commands; current setup is in
> [`docs/coord/GET-ON-THE-BUS.md`](../../GET-ON-THE-BUS.md).

**Goal:** migrate real coordination from the incumbent (`/coordination/` JSON bus, fulcra-coord v0.15.16,
launchd fleet) to coord (`team/<t>/` markdown + coord-engine), and test on a real team.

## Constraints that shape the choice
- **Hosts migrate independently.** A deployment may include desktop and cloud
  agents with different availability. Avoid requiring simultaneous fleet cutover.
- **Same physical store.** Both systems live on the one Fulcra File Store (`/coordination/` vs `team/`),
  so "migration" is a data mapping + habit change, not a platform move.
- **Doc 01's C4 rule:** no long-lived shadow store / dual-truth. Any bridge must be short-lived or absent.
- Preserve incumbent task, event, audit, and review history.

## Approaches compared

### A — Hard cutover
Freeze the incumbent (broadcast), export open tasks → coord team, retire incumbent automation, point
everyone at coord.
- **+** one truth immediately; no bridge code; cleanest end state.
- **−** requires the whole fleet at once (machine-gated → impossible today); breaks the live Codex review
  workbook flow mid-flight; no rollback once incumbent automation is retired; big-bang risk on a
  system that coordinates the very agents doing the migration.

### B — Gradual bridge (dual-run + mirror)
coord primary for new work; a bridge job mirrors incumbent directives/inbox into the coord team (and/or
back) while hosts migrate one by one.
- **+** nothing goes dark; per-host migration.
- **−** exactly the "two systems bridged" failure doc 01 forbids: dual truth, drift, ack/status divergence
  between mirrored copies, and the bridge is throwaway code needing its own reviews. The incumbent's own
  history (summaries-orphan leak) shows what mirrored derived state does over time.

### C — Phased adoption with a one-shot exporter (RECOMMENDED)
No mirror. Three phases, each independently safe:
1. **Adopt (test on a real team):** create the real team space (`team/<team>/`), migrate **open** tasks
   once via a deterministic exporter (`coord-engine migrate` — incumbent JSON → coord task docs,
   idempotent, `--dry-run` first), install coord heartbeat+listener on THIS host, and run real work on
   it (this epic's own follow-ups live there). The incumbent keeps running untouched for the fleet.
2. **Per-host adoption:** as each fleet machine is touched (operator-gated), run `coord-setup.sh`,
   `install-heartbeat`/`install-listener` for the team, and retire that host's incumbent launchd jobs.
   New work goes to coord; an agent still on the incumbent simply isn't reachable by coord directives
   yet (visible in `presence`/`agents` — no silent loss).
3. **Retire:** when `coord-engine health` shows every active host reconciling coord and the incumbent
   board is empty of open work, freeze the incumbent (final broadcast + read-only), keep `/coordination/`
   as cold history (no data deleted), remove remaining launchd jobs.
- **+** no bridge code, no dual-truth window per task (a task lives in exactly one system: unmigrated =
  incumbent, migrated = coord; the exporter marks migrated tasks on the incumbent side), rollback at
  every phase, fleet migrates at its own pace, real-world test is phase 1 itself.
- **−** during phase 2 the OPERATOR watches two digests (bounded, explicit); incumbent history is not
  ported (deliberate — it stays queryable read-only forever; coord starts with open work only).

## The exporter (`coord-engine migrate`) — deterministic mapping
- Source: `/coordination/tasks/*.json` via the same transport. Filter: non-terminal only (default).
- Field map: `title→title`, `status→status` (identical vocab), `priority→priority`, `workstream→tags
  workstream:<ws>` , `kind→tags kind:<k>`, `owner_agent→owner`, `assignee→assignee` (incl `*`/@backlog),
  `current_summary→description`, `next_action→next_action`, `blocked_on→blocked_on`,
  `not_before/due→same`, `updated_at→timestamp`, `id→body provenance line` (slug from title; original id
  preserved in frontmatter `migrated_from`).
- **Idempotent:** skip if a doc with the same `migrated_from` already exists in the team (or same slug).
- **One-way + marked:** after a successful verified write, append a `migrated` event/tag on the incumbent
  task (`tags += migrated:coord`) so incumbent boards/digests can filter them out — the task now lives in
  exactly one active system. `--dry-run` prints the plan without writing. `--no-mark` for rehearsal.
- Never deletes anything on the incumbent.

## Test plan (phase 1 acceptance)
On a synthetic test team with migrated tasks: reconcile heals the index and aggregate
at representative scale using full and incremental passes; board/needs-me/digest match the incumbent's
view for the migrated set (spot-check N=10); directives round-trip (tell→inbox→ack→respond); briefing +
park/checkpoint; health fresh; heartbeat + listener installed and self-tested on this host. Rollback
rehearsal: `--dry-run` + `--no-mark` first on a scratch team.

---

## Resolution (opus plan review, ENDORSE-WITH-CHANGES — 2026-07-02)

1. **The tag was decorative — the invariant is a TERMINAL TRANSITION.** The incumbent has no tag-based
   board exclusion (verified in its query.py), so a tagged-but-open task stays live on every incumbent
   host (dual execution). The exporter now, on verified coord write: sets the incumbent task
   `status: abandoned`, appends an `abandoned` event (`by: coord-migrate`, summary pointing at the
   coord doc), bumps `updated_at`, AND adds the `migrated:coord` tag (metadata). Incumbent
   OPEN_STATUSES then naturally hides it fleet-wide with zero incumbent code changes.
2. **Repair pass built in:** a task already migrated (coord twin exists via `migrated_from`) but still
   open on the incumbent gets its terminal transition finished on the next run — partial failures
   (write-ok, mark-fail) self-heal instead of silently double-listing.
3. **Open review loops are NOT migration-eligible.** Tasks carrying a `pr` field or review-verdict kinds
   stay on the incumbent until their loop closes (the Codex workbook produces/consumes verdicts there);
   this confines each verdict flow to one system with no bridge. Reported as `skipped_review`.
4. **Identity policy: ids are IDENTICAL across systems** (nothing in coord forces different agent ids —
   keep `claude-code:<Host>:<ws>` verbatim), so inbox/assignee folds match without translation. An
   optional `--map old=new` handles exceptions.
5. **Role registry is seeded MANUALLY during phase-1 acceptance** (registry docs only — leases NEVER
   migrate, they re-establish); `--roles`/`--map` flags deferred (ids are identical, so no map needed).
   `checkpoint_ref` migrates verbatim (opaque pointer, shared store). PREFLIGHT: assert no fleet host
   runs `FULCRA_COORD_READ_SOURCE=events` (such a host would keep migrated tasks live — the exporter
   writes only the file store, not the event store).
6. **Acceptance additions:** identity-inbox assertion; dual-listing NEGATIVE test on the incumbent board;
   partial-failure recovery (kill between write and mark → re-run → no dual-truth); shard-GC clean at
   a representative synthetic corpus; exporter is sequential (no concurrency-ceiling collision).
7. **Phase-3 gate additions:** no open review loop on the incumbent; forge/verdict pollers stopped;
   "active host" freshness window > slowest machine-gated host cadence.

---

## Phase 1 — rehearsal

Use a synthetic team to verify field fidelity, stable task IDs, migration marking,
and recovery from partial failure. Confirm status counts and review-loop handling
before planning a deployment migration. Keep live run logs outside this repository.

## Phase 2 — per-host adoption checklist

For each host, use the current [`setup guide`](../../GET-ON-THE-BUS.md), verify
`coord-engine doctor <team>`, and register presence under its configured identity.
Retire incumbent jobs only once the host's open review loops have closed. Migrate
the review host last if it is still draining legacy review work.

## Phase 3 — retirement gate (from Resolution §7)
All active hosts fresh in `coord-engine health <team>`; incumbent board has NO open work AND no open
review loops; forge/verdict pollers stopped; final incumbent broadcast + freeze; /coordination/ kept as
cold read-only history.
