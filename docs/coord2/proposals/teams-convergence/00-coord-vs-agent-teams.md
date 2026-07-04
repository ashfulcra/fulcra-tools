# fulcra-coord + continuity (as-built) vs. official `fulcra-agent-teams` (alpha)

> Grounding: coord side reversed from `/Users/ashkalb/Developer/fulcra-tools-coord/packages/fulcra-coord`
> (v0.15.16) as it runs today. Official side from a fresh clone of `fulcradynamics/agent-skills`
> (`skills/fulcra-agent-teams/SKILL.md` + `references/fulcra-agent-teams-cli.md` + README).

---

## PART 1 — Reversed functional spec: coord + continuity (as-built)

### 1.1 Purpose
A coordination layer that lets independent agents (Claude Code, Codex, OpenClaw, CI) run durable,
shared work over **Fulcra Files** as a bus — no shared memory, no direct calls. Read/write are
separated: authoritative **task files** are the source of truth; a **reconcile engine** rebuilds
derived **views** that all reads hit.

### 1.2 Substrate & transport
- Remote root `/coordination/` on Fulcra Files. Layout:
  - `tasks/<TASK-id>.json` — authoritative task bodies
  - `views/{index,search-index,next,summaries,presence,roles,health,operator_digest}.json` — derived read models
  - `continuity/<workstream>/<agent>/<task>/latest.json` — continuity snapshots
  - `events/…`, `retention/…`, directive/inbox state
- Transport is a **CLI-subprocess-per-file** call to `fulcra-api` (~0.9–1.3 s/op; gateway saturates
  ~15–18 concurrent — a hard no-more-concurrency constraint). Everything is best-effort / never-raise.

### 1.3 Identity, workstreams, roles
- Identity string `claude-code:<Host>:<workstream>` (e.g. `claude-code:Ashs-MBP-Work:fulcra-tools`),
  derived from tool + stable hostname + cwd/workstream, env-overridable.
- **Workstream** = a lane of work an agent declares presence in. **Role** = a durable named function
  with a resume/checkpoint registry (`checkpoint` reads/writes a role's durable resume point).

### 1.4 Data model (structured JSON, schema-versioned)
- Task record: `id` (`TASK-YYYYMMDD-slug-hash`), `title`, `status`, `priority` (P0–P3),
  `workstream`, `owner_agent`, `assignee`, `last_touched_by`, `tags` (incl. `kind:*`, `needs:human`),
  `current_summary`, `next_action`, `blocked_on`, `not_before`, `due`, `updated_at`, `done`/`done_at`,
  `acked_by[]`. `TERMINAL_STATUSES = {done, abandoned}`; non-terminal incl. proposed/active/waiting/blocked.
- `task_summary` projection is the read-side row (drives status/agents/inbox/search).

### 1.5 Command surface (~50 typed verbs, grouped)
- **Identity/presence:** `connect`, `identity`, `workstream`, `presence`, `agents`, `roles`, `capabilities`
- **Task lifecycle:** `start`, `update`, `block`, `pause`, `done`, `abandon`, `assign`, `restore`
- **Messaging/directives:** `tell`, `broadcast`, `remind`, `later`, `handoff`, `inbox` (`--ack`), `respond`, `human`
- **Review handshake:** `request-review`, `review-done` (`--verdict`)
- **Continuity/resume:** `snapshot`, `checkpoint`, `park`, `resume`, `briefing`, `handoff`
- **Discovery/query:** `status`, `board`, `search`, `needs-me`
- **Engine/ops:** `reconcile`, `health`, `doctor`, `retention` (via reconcile), `announce-version`
- **Automation install:** `install-heartbeat`, `install-listener`, `install-claude-code`, `install-codex`,
  `install-digest`, `notify-inbox`, `listener-tick`, `ensure-codex-watch`
- **Digest/annotations:** `digest`, `annotations`

### 1.6 The engine — reconcile
Authoritative view-repair path (launchd heartbeat ~every 1200 s):
- Loads the full authoritative `tasks/` listing (degraded/partial load → early-return, no view write).
- Rebuilds ~35 views: `index` (active/recent), `search-index`, `next`, `summaries` (the aggregate read
  source), `presence`, `roles`, `health`, `operator_digest`.
- Last-writer-wins **guards** (`_summaries_upload_would_clobber`) + a merge that unions cross-host open
  rows; **age-discriminator prune** of stale orphans (v0.15.16).
- Sub-passes: presence rebuild, review sweep, retention, **event-parity** + **directive-parity**
  (sampled drift detectors), loop/role health, verdict-adopt, undelivered. Phase-timed, deadline-gated.
- **Retention:** terminal tasks past a window (`FULCRA_COORD_RETENTION_DAYS`=30) archived (soft-delete;
  `search --archived`/`restore`), daily-throttled.

### 1.7 Directives & review handshake
- `tell`/`broadcast` create **directives** addressed to an agent (or wildcard) with priority; delivered
  via the target's `inbox`; **per-agent ack** stops re-notify. `respond` closes a loop with outcome/evidence.
- Review: `request-review` (records author=owner, pr=artifact) → reviewer → `review-done --verdict`
  creates a **P1 directive back to the author** with the verdict.

### 1.8 Continuity snapshots (structured, resumable)
- Snapshot JSON schema: `checkpoint_id`, `objective`, `decisions`, `next_actions`, `open_questions`,
  `artifacts`, `identity`, `owner_agent`, `source`, `task_id`, `transcript_path`,
  `context_used_percent`, `created_at`, `schema_version`.
- **Produced by** `snapshot`/`checkpoint`/`park`/`pause`/`done` and by Claude Code hooks
  (SessionStart/PreCompact/SessionEnd); written to `continuity/<ws>/<agent>/<task>/latest.json`.
- **Consumed by** `resume`/`briefing`/`handoff` to rehydrate a fresh session. A durable **launchd
  snapshot timer** can produce them unattended.

### 1.9 Automation (OS-level)
- launchd jobs: `heartbeat` (reconcile), `snapshot` (continuity timer), `listener` (runs `notify-inbox`
  every ~10 min → on **new** inbox work, fires a **wake chain** = headless `claude -p` to auto-handle),
  keepalive. Health/logs under `~/Library/Logs/fulcra-coord/`.

---

## PART 2 — Official `fulcra-agent-teams` (alpha), in brief

- **Positioning:** "Enable agents to collaborate using shared memory, team inboxes, and user artifacts
  via Fulcra's versioned file storage." User-invocable skill, MIT, OpenClaw 🤝.
- **Interface:** NO dedicated CLI — everything is the generic `uv tool run fulcra-api file {upload,
  download,list,stat,delete}`.
- **Data format:** **markdown files with OKF (Open Knowledge Format) YAML frontmatter**. No JSON, no schema.
- **Namespaces:**
  - `agent/<agent>/artifact/` — generated non-markdown deliverables (upload gated on explicit user approval)
  - `team/<team-name>/` — the coordination space, OKF-structured:
    `index.md` (members+concepts), `log.md` (chronological), `role.md` (team mission), `progress.md`
    (recent+next), `completed.md` (grow-only objectives), `artifact/`, `session/` (per-session summaries),
    `task/<name>.md` + `task/index.md` (long-running trackers), `knowledge/` (open-ended KB),
    `member/<agent>/{role.md, progress.md, inbox/, archive/}`.
- **Inbox lifecycle:** teammates drop markdown into `member/<agent>/inbox/`, named
  `YYYYMMDD-HHMMSS_<sender>_<topic>.md`; **thread continuity** = reuse the exact `<topic>`; processing =
  copy to `archive/` (prepend timestamp if missing) → **verify with `stat`** → **delete** from inbox.
  Audit trail comes from **Fulcra's file versioning** (create/delete timestamps), not a log.
- **Continuity across isolated runs:** `member/<agent>/progress.md` ("vital for maintaining context
  across isolated cron runs"), plus `session/` summaries and `task/` trackers — all freeform markdown.
- **Automation:** consent-gated **HEARTBEAT.md** task + **cron** jobs; the cron payload MUST instruct the
  agent to first read `progress.md`/`role.md`/member files. No listener/wake infra — relies on the agent
  runtime's own heartbeat/cron.
- **Memory integration:** consent-gated directive added to `~/.openclaw/workspace/MEMORY.md` to always
  read team state before acting.
- **Security:** explicit first-class warning about cross-agent data transfer / authorization boundaries.
- **Maturity:** alpha convention; no engine, no reconcile, no queries. State IS the files; humans/agents
  maintain `index.md`/`log.md` by hand ("in practice, download log.md, append, re-upload").

---

## PART 3 — Comparison

### 3.1 Structural mapping

| Concept | **fulcra-coord** (ours) | **fulcra-agent-teams** (official) |
|---|---|---|
| Shared substrate | Fulcra Files `/coordination/` | Fulcra Files `team/` + `agent/` |
| Interface | Dedicated `fulcra-coord` CLI (~50 typed verbs) | Generic `fulcra-api file` CLI (5 verbs) |
| Data format | Structured JSON, schema-versioned | Markdown + OKF YAML frontmatter |
| Task model | Task record (id/status/priority/owner/assignee/tags/dates) | `task/<name>.md` freeform + `task/index.md` |
| Status lifecycle | Typed (proposed/active/waiting/blocked/done/abandoned) | None (prose in markdown) |
| Identity | `claude-code:Host:workstream` (derived) | `<agent-name>` (freeform) |
| Grouping | Workstream + Role (+ checkpoints) | **Team** container + member + `role.md` |
| Inbox | Directives: priority + per-agent **ack** + re-notify | Markdown drop → **archive → delete** (versioning = audit) |
| Directed messaging | `tell`/`broadcast`/`remind`/`respond` | Write file to `member/<agent>/inbox/` |
| Derived views | **reconcile** builds index/search/next/summaries/presence/roles/health | Hand-maintained `index.md`/`log.md` |
| Queries | `board`/`search`/`next`/`needs-me`/`status` | `file list` only (no query) |
| Continuity | **Structured snapshot JSON** (objective/decisions/next/open-q/transcript_path/context%) | `member/progress.md` + `session/` + `task/` markdown |
| Resume | `resume`/`briefing`/`handoff` rehydrate | Read `progress.md`/`role.md` at wake |
| Review | `request-review`→`review-done`→P1 directive | **None** |
| Automation | launchd **heartbeat + snapshot + listener/wake** (headless `claude -p`) | HEARTBEAT.md + cron (runtime-driven), consent-gated |
| Consistency | Engine: reconcile, LWW guards, parity, retention, orphan-prune | **None** — files are truth; manual index upkeep |
| Audit trail | Task history + events + reconcile health | Fulcra **file versioning** (create/delete stamps) |
| Memory integ | Hooks + auto-memory | `MEMORY.md` directive (manual) |
| Knowledge base | (implicit in tasks) | First-class `knowledge/` OKF KB |
| Artifacts | (task artifacts) | `artifact/` namespace + explicit approval gate |
| Standard | Bespoke | **OKF** (Google knowledge-catalog spec) |
| Maturity | Production-ish, perf-tuned, fleet-deployed | Alpha convention |

### 3.2 Shared DNA (the official skill clearly reflects our concepts)
Fulcra Files as the coordination substrate; **per-agent inbox**; **progress/continuity files to survive
isolated cron/heartbeat runs**; **consent-gated background heartbeat + cron** that must read context
first; **MEMORY.md integration**; **session summaries**; handoff-across-sessions. These are exactly the
primitives coord pioneered — the official skill is the same idea, re-expressed as a lightweight convention.

### 3.3 What each has that the other lacks
- **coord has, agent-teams lacks:** typed task lifecycle + priority; queryable derived views;
  self-healing reconcile (consistency without human upkeep); directive **priority + ack + re-notify**;
  **review handshake**; structured **resumable** snapshots (context%, transcript pointer);
  roles + role-checkpoints; OS-level **wake** infra; retention/archival; health/doctor; operator digest.
- **agent-teams has, coord lacks (or does implicitly):** explicit **team** container as a first-class
  grouping; **OKF** standard alignment (interop); **human-readable markdown** state (grep/read without a
  tool); open-ended **knowledge/** base; **artifact approval gate**; a first-class **cross-agent
  data-transfer security warning**.

### 3.4 Trade-offs
- **agent-teams (convention):** near-zero install (any agent with `fulcra-api`), human-readable, portable,
  standard (OKF), low cognitive load. **But** no structured queries, no consistency guarantees (manual
  `index.md`/`log.md` **will drift** — the same failure *class* as coord's summaries-orphan leak, except
  there is no reconcile to heal it), no typed status/priority, inbox has no priority/ack beyond
  file-move, continuity is freeform, and automation is only as durable as the agent runtime's cron.
- **coord (platform):** rich queries, self-healing consistency, typed lifecycle, structured
  directives/review, resumable structured continuity, durable OS-level wake. **But** heavyweight —
  a dedicated CLI to install and keep version-synced across a fleet (the version-skew + stale-host pain),
  a slow CLI-subprocess-per-file transport, and many moving parts (reconcile/parity/launchd zoo) that
  can break (the wake-auth 401, the orphan leak, lease contention).

---

## PART 4 — Strategic read

1. **The official skill validates the core thesis** (Fulcra Files as an agent-coordination bus with
   inboxes + continuity + consent-gated automation) but ships it as a *thin convention*, not an engine.
2. **The official skill's biggest latent risk is consistency drift.** Hand-maintained `index.md`/`log.md`
   + archive-by-delete has no reconcile — at fleet scale it accrues exactly the kind of aggregate/index
   rot coord had to build reconcile + orphan-pruning to survive. If agent-teams grows, it will want a
   reconcile-equivalent.
3. **Complementary tiers, not competitors.** agent-teams = portability/convention tier (human-readable,
   standard, no install); coord = power/engine tier (typed, queryable, self-healing, resumable).
4. **Convergence opportunities:**
   - **coord → adopt from official:** OKF alignment + emit **human-readable markdown mirrors** of its
     views (so agent-teams-style agents can read coord state without the CLI); the explicit **team**
     container; the **artifact approval gate** + **cross-agent security warning** as first-class.
   - **official → adopt from coord:** typed status/priority + a queryable index; a **reconcile/heal**
     pass for `index.md`/`log.md`; **structured resumable** continuity fields (context%, transcript
     pointer) inside `progress.md`; directive **priority/ack**; a **review** handshake.
   - **Bridge:** have coord's `reconcile` optionally **publish an OKF `team/` projection** of its
     structured state — coord stays the engine of record, agent-teams-compatible agents consume the
     markdown. Best of both: coord's consistency, agent-teams' portability.

---

## PART 5 — Code-grounded refinements (verified against source)

A second, code-grounded survey confirmed the spec above and sharpened these (all verified in
`packages/fulcra-coord/fulcra_coord/`):

- **Architecture:** ~50 modules (`entry.py` dispatch + `COMMAND_MAP`; handlers in `lifecycle.py`,
  `presence.py`, `query.py`, `routing_ops.py`, `role_ops.py`, `continuity_ops.py`, `directives.py`,
  `forge_mirror.py`, `retention.py`, `installers.py`, …). `cli.py` (2949 lines) holds the **reconcile
  engine** + the summaries merge/clobber logic.
- **Concurrency = NO-CAS.** Immutable records (tasks, directives) are conflict-safe via **append-only
  sub-log shards** keyed by unique event-id / agent-slug; mutable records (views, index, roles registry)
  use **last-writer-wins** (safe because a single writer — reconcile/announce-version/operator — owns them).
- **Directives are a dual-write MIRROR of tasks**, not a separate primary store. The authoritative
  object is an ordinary `proposed` **task** with `assignee`=recipient; the `directives/{id}.json` record
  is an additive mirror with a **deterministic id `DIR-T-<task_id>`** and its own status vocab
  (`proposed|acked|acted|expired`), carrying append-only sub-logs: `acks/`, `routing/`, `responses/`,
  `evidence/`. (The summaries `acked_by` is the union of `acks/`.) So "inbox" = tasks assigned to you,
  surfaced via the directive mirror for board/loop views.
- **Identity = 4-tier precedence:** `--agent` > `$FULCRA_COORD_AGENT` > per-cwd persisted
  `identities/<cwd-hash>.json` (hashed realpath, so repos don't clobber) > derived
  `claude-code:<stable-host>:<cwd-basename>`. Human handle is separate/global.
- **Roles = registry + ephemeral leases.** `roles/<name>.json` (policy shared|exclusive, sla_hours,
  maintainer, standing_instructions) + `roles/<name>/leases/<agent>.json`. A **role_status fold** yields
  HELD/VACANT/CONTESTED/UNKNOWN from holder presence freshness; **vacancy past sla_hours** writes a daily
  `escalations/` marker → escalation directive to the maintainer. (No agent-teams analogue.)
- **forge-mirror** (`forge_mirror.py`): mirrors GitHub signals (merge, verdict comments) into a review
  loop's `evidence/` sub-log — a real VCS↔bus bridge. (No agent-teams analogue.)
- **Continuity is a SEPARATE system.** coord stores only an **opaque `checkpoint_ref`**; the structured
  snapshot (objective/decisions/next/open-q/transcript_path/context%) is produced/consumed by a distinct
  **`fulcra-continuity`** CLI that `snapshot`/`park`/`handoff` delegate to. So coord ↔ continuity are two
  systems bridged by an opaque pointer — cleaner separation than the comparison first implied.

**Net effect on the comparison:** every refinement makes coord *more* of a structured engine, not less —
strengthening the "platform vs. convention" thesis. Three comparison rows to add:
| Concept | fulcra-coord | fulcra-agent-teams |
|---|---|---|
| Concurrency model | NO-CAS append-only sub-logs + LWW views | file move + Fulcra versioning |
| VCS / forge bridge | `forge-mirror` (GitHub → evidence) | none |
| Continuity architecture | separate `fulcra-continuity` via opaque `checkpoint_ref` | inline `progress.md` (no separation) |
