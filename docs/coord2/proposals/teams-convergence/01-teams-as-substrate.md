# Teams-as-substrate: coord + continuity as optional layers on top of `fulcra-agent-teams`

**Question:** Can we take the official `fulcra-agent-teams` skill as the foundation and rebuild
coord's + continuity's additional functionality as one or more *optional packages* that run on top of it?

**Verdict: Yes — and it's the better architecture.** It turns two divergent systems into one: the
official OKF-markdown skill as the base tier, and coord's power (typed lifecycle, self-healing views,
roles/leases, review, forge bridge, structured continuity, OS-level wake) as opt-in packages. A user can
run bare teams, or add exactly the layers they want. Strategically this makes the *official* thing the
substrate and demotes coord from "parallel system to keep in sync" to "pro tier of the official skill."

---

## 1. Why it works (the enabling facts)

1. **Shared substrate already.** Both persist to the same **Fulcra File Store** (environment-agnostic,
   versioned). coord writes JSON under `/coordination/`; teams writes markdown under `team/`. Same
   filesystem, same `fulcra-api file {upload,download,list,stat,delete}` transport — only the *record
   format* and the *engine* differ.
2. **Teams' primitives are a strict subset of coord's.** Per-agent inbox, archive lifecycle,
   progress/continuity files, team namespace, consent-gated heartbeat/cron — coord has all of these plus
   more. So coord's features are mostly **additive**, not replacements.
3. **Most of coord's power is format-agnostic.** Reconcile, roles/leases, forge-mirror, and the
   listener/wake automation don't care whether a record is JSON or markdown-frontmatter — they care about
   "a set of records to index, heal, route, or watch." Retargeting them at the `team/` namespace is
   mechanical, not conceptual.
4. **OKF is soft.** Fulcra's own docs say adhere "**when possible**" (v0.1), for *text/markdown*
   readability. JSON sidecars and operational subdirs are already precedented (`inbox/`, `archive/` are
   non-indexed). So structured coord artifacts can live in the namespace without violating the standard.

---

## 2. The concerns / failure modes (and mitigations)

Presented before the design because they shape it.

| # | Failure mode | Mitigation / design constraint |
|---|---|---|
| C1 | **Format impedance** — coord's engine speaks JSON; teams records are markdown+YAML frontmatter, hand-editable (a *feature* of teams). Frontmatter as a typed store is fragile (YAML edge cases, humans mangling it). | Layers must treat markdown as **source of truth** and parse defensively (never-raise, degrade on bad frontmatter — coord already has this posture). Typed fields live in OKF frontmatter (extensible YAML); body stays human prose. |
| C2 | **Index-ownership governance flip.** Teams says "agents hand-maintain `index.md`/`log.md`." coord says "the engine owns derived views." You cannot have both writing `index.md`. | Once Layer 1 is present, **`index.md` / `task/index.md` become engine-owned** (agents stop hand-editing them; they hand-edit *content* files, reconcile rebuilds the indexes). This is the one non-additive behavior change — call it out to adopters. |
| C3 | **Performance floor.** Same slow CLI-subprocess-per-file transport (~1s/op, ~15–18 concurrent). A reconcile that scans N task files + M inbox files is N+M downloads — *worse* than coord's aggregate-of-one-download reads. | Layer 1 re-introduces coord's **`summaries.json` aggregate** as a sidecar in the team namespace, so reads stay O(1). You don't get teams' simplicity *and* coord's read speed for free — the sidecar is the price. |
| C4 | **Single-source-of-truth discipline.** If the coord layer keeps its own shadow JSON store, you recreate "two systems bridged" — the very thing we're removing. | **Hard rule:** the coord layers operate *on the teams files in place* (parse→modify→write). No shadow authoritative store. Derived artifacts only. |
| C5 | **Alpha churn.** teams is alpha; conventions have already shifted (artifact path pluralization, PRs #61–#116). Building on a moving base means chasing breaks. | Pin a **teams-convention version** the layers target; co-evolve upstream. Treat the layer boundary as a versioned contract. |
| C6 | **Mixed fleets.** Some agents run bare teams (write markdown directly), some run coord layers. | Works **iff** layers tolerate base-teams-shaped files (backfill missing frontmatter with defaults) and only the reconcile host owns the indexes (C2). A bare-teams hand-edit is fine — reconcile re-parses it next pass. |

Net: no blocker. C2 (index ownership) is the one genuine behavior change; the rest are engineering
discipline coord already practices.

---

## 3. The layered package architecture

Layer 0 is the official skill, unchanged. Each layer above is an **optional package**; adopters stop
wherever they want. Paths are all under `team/<team>/` unless noted.

### Layer 0 — `fulcra-agent-teams` (base, unchanged)
OKF markdown substrate: `{index,log,role,progress,completed}.md`, `artifact/`, `session/`,
`task/<name>.md` + `task/index.md`, `knowledge/`, `member/<agent>/{role,progress}.md`, `inbox/`, `archive/`.

### Layer 1 — `coord-reconcile` (the linchpin — self-healing views + queries)  ← *the "OKF projection", inverted*
- **Reads:** `task/*.md` + `member/*/inbox/*.md`, parsing OKF frontmatter (`type:` + coord fields).
- **Rebuilds (engine-owned):** `task/index.md` and top-level `index.md` — OKF-compliant human indexes,
  healed each pass (missing entries added, stale/orphaned entries pruned — the exact discipline that
  fixed coord's summaries-orphan leak).
- **Emits (sidecar):** `_coord/summaries.json` — a fast-path aggregate so query verbs are one download,
  not N. Lives in a clearly-marked non-OKF operational subtree (precedent: `inbox/`/`archive/`).
- **Provides verbs:** `status` / `board` / `needs-me` / `search` reading the aggregate.
- **Concurrency:** LWW single-writer on the indexes + aggregate (reconcile host owns them).
- *Purely additive except C2 (index ownership).* This is the single highest-value package — it gives
  teams queryability + consistency it structurally lacks.

### Layer 2 — `coord-tasks` (typed lifecycle)  — depends on L1
- Typed fields in task frontmatter: `id`, `status` (`proposed|active|waiting|blocked|done|abandoned`),
  `priority` (P0–P3), `assignee`, `owner`, `updated_at`, `blocked_on`, `due`, `not_before`.
- Verbs `start/update/block/pause/done/abandon` parse→modify→write the task md and append a state-change
  line to its body (teams already appends task updates). `done` requires evidence + verification level.
- Enforces the status machine. Backfills defaults on base-teams tasks that lack the fields (C6).

### Layer 3 — `coord-directives` (structured inbox: priority + ack + re-notify)  — depends on L1
- Priority in inbox-message frontmatter; **ack = the existing archive-move** (teams already archives!),
  optionally an append-only `member/<a>/inbox/_acks/` shard for board rollups.
- `board` / `needs-me` across members; re-notify surfaces unacked high-priority items.
- Maps almost 1:1 onto teams' inbox — the smallest lift for real value.

### Layer 4 — `coord-roles` (roles + leases + vacancy escalation)  — additive, independent
- `roles/<name>.md` (OKF registry: policy `shared|exclusive`, `sla_hours`, `maintainer`, instructions)
  + `roles/<name>/leases/<agent>.json` (append-only shards).
- HELD/VACANT/CONTESTED fold from member `progress.md`/presence freshness; SLA vacancy → a message into
  the maintainer's inbox. Invisible to bare-teams users.

### Layer 5 — `coord-review` + `coord-forge` (review handshake + VCS bridge)  — additive, independent
- Review: `request-review` drops a review message in the reviewer's inbox; `review-done --verdict`
  (`approve|changes`) drops a P1 verdict message back to the author's inbox. Pure convention + tracker.
- Forge: watch GitHub (merges, verdict comments), append evidence to the review task/thread.

### Layer 6 — `coord-continuity` (structured resumable snapshots)  — additive, independent
- Keep the separate **`fulcra-continuity`** engine; point its `checkpoint_ref` at
  `member/<agent>/continuity/<task>/latest.json` (structured JSON) in the team namespace. `resume` /
  `briefing` / `handoff` consume it. Richer than teams' freeform `progress.md`, and opt-in.

### Layer 7 — `coord-automation` (heartbeat / listener / wake / digest)  — additive, mostly reused
- The launchd/cron installers, retargeted: heartbeat runs **L1 reconcile on the team namespace**;
  listener runs "check my team inbox" + the wake chain (headless `claude -p`); digest reads the L1
  aggregate. Substrate-agnostic — the biggest reuse-as-is.

**Dependency graph:** L1 is the foundation for L2/L3 queries. L4/L5/L6/L7 are independent add-ons.
Additive (invisible to bare-teams users): L4, L5, L6, L7, and L1's sidecar. Behavior-changing (adopter
must accept): L1 index ownership (C2), L2 typed frontmatter, L3 formalized inbox semantics.

---

## 4. Adoption path (incremental, reversible)
1. **Bare teams** — official skill, nothing added.
2. **+ L1** — instantly queryable + self-healing indexes (biggest single win; the drift teams will
   otherwise accrue at scale, healed).
3. **+ L3** — priority/ack/board on the inbox (small lift).
4. **+ L2** — typed task lifecycle + status machine.
5. **+ L4/L5/L6/L7** — roles, review+forge, structured continuity, OS-level automation, à la carte.

Each layer is removable: delete the package + its sidecars/subdirs; the base markdown remains a valid
teams space.

---

## 5. What to validate before building
- **OKF v0.1 exact frontmatter rules** (read `GoogleCloudPlatform/knowledge-catalog/okf/SPEC.md`): confirm
  extra frontmatter keys (`status`,`priority`,`assignee`) and a `_coord/` operational subtree are
  compliant / tolerated. (Soft standard suggests yes; verify.)
- **`fulcra-api file` semantics** under concurrent writers: does it give atomic-ish uploads + reliable
  `stat`-after-write? coord's NO-CAS append-only shard model assumes per-file last-writer-wins with no
  partial reads — confirm the File Store honors that for the shard subdirs.
- **teams-convention pinning:** choose the upstream commit/version the layers target; decide fork vs
  contribute-upstream.
- **fulcra-continuity integration point:** confirm `checkpoint_ref` can be an arbitrary team-namespace
  path and the continuity engine round-trips it.

## 6. The strategic payoff
coord stops being a separate system to keep version-synced across a fleet (the exact pain of this
maintainer workstream — stale hosts, version skew, the orphan leak, the wake-auth drift). It becomes the
**pro tier of the official skill**: bare-teams agents interoperate by reading the same markdown; power
agents get typed lifecycle, self-healing, roles, review, and durable wake — all opt-in, all on the one
official substrate.
