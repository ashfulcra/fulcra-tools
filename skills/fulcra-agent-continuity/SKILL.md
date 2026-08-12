---
name: fulcra-agent-continuity
description: "Give agents structured, resumable continuity in a fulcra-agent-teams space: snapshot objective / decisions / next-actions / open-questions to a schema, and get a deterministic resume brief when a fresh session or cron run wakes up."
homepage: "https://github.com/ashfulcra/fulcra-tools"
license: "MIT"
user-invocable: true
metadata: { "openclaw": { "emoji": "🧭" } }
---

# Fulcra Agent Continuity

Enhances the [`fulcra-agent-teams`](https://github.com/fulcradynamics/agent-skills) skill. Teams already
uses `member/<agent>/progress.md` to survive isolated cron/heartbeat runs, but it's freeform — a fresh
session has to re-read prose and guess what mattered. This skill adds a **structured** snapshot
(objective, decisions, next actions, open questions, artifacts, context-used-%) and a **deterministic
resume brief**, so waking up is reliable instead of a re-read.

Whether/when to snapshot is a judgment call (prose); building the schema and folding many snapshots to
the newest is deterministic (the `coord-engine` tool).

## The lifecycle contract (applies on every harness)

Any agent participating in a team space owes these four behaviors. Harness adapters
(see fulcra-agent-automation §Harness adapters) automate them where the platform
allows; where it doesn't, follow them as prose:

1. **On wake** (new session, cron fire, heartbeat, automation tick):
   `coord-engine continuity resume <team> <agent>` and read your inbox
   (`coord-engine inbox <team> --agent <agent>`) BEFORE taking new work.
2. **On material change** (a decision made, an artifact produced, a task claimed or
   finished) and at least hourly while actively working:
   `coord-engine continuity snapshot <team> <agent> <task> --objective … [--decision …] [--next …]`.
3. **Before context loss** (compaction, model handoff, session end):
   `coord-engine continuity park <team> --agent <agent> --objective "<one line>"` —
   parks every held role with a checkpoint. Handing off to a successor session?
   The checkpoint's CONTENT has rules of its own — see
   [Parking for a successor](#parking-for-a-successor-handoff-doctrine).
4. **Inbox cadence:** while live, read your queue and inbox at least every 30 minutes. If your
   platform has a durable scheduler, arm ONE scheduled wake that runs
   `coord-engine queue <team> --agent <you>` instead of trusting yourself — a schedule, never a
   resident listener (those are retired; see the pickup note below).

An agent that beats presence but has no fresh snapshot is flagged `continuity-stale` by `coord-engine health`.

## Timeline visibility: the checkpoint channel

Every **successful** `continuity snapshot` and `continuity park` also emits one
moment to the account's **Agent Checkpoint** channel, so a human watching the
Fulcra timeline sees the fleet checkpointing instead of inferring it from files
nobody opens. The moment carries the same four-dimension identity tags a bus
event does (`agent:` / `platform:` / `harness:` / `model:` plus the base tag),
from the same `tags.json` registry — one `bus-v3 tag-provision` covers both.
Its note is compact JSON:

```json
{"v":1,"kind":"checkpoint","agent":"amy","task":"role-reviewer",
 "objective":"first 140 chars of the objective",
 "path":"team/r/member/amy/continuity/role-reviewer/latest.json"}
```

`objective` is a hard 140-character slice (no ellipsis) — the note is a
timeline label and the snapshot at `path` holds the full text.

**Reads never emit.** `resume`, `checkpoint --role`, and `briefing` are pure
reads; a moment for a read would claim state was saved when nothing was.

Emission is driven by one document, `team/<team>/_coord/bus-v3/checkpoints.json`
(**separate from `records.json` on purpose** — old engines fail their queue
closed on a bus authority carrying fields they do not know; see
[bus v3 setup step 5](../../docs/coord/BUS-V3.md#5-seed-the-checkpoint-channel-config)).
With no such document the engine emits nothing and says nothing, so
pre-adoption teams see no change at all. Malformed bytes are loud every time
and are never auto-repaired.

### The fail-open rule (deliberately the inverse of park's)

`park` is loud and **non-zero** when it cannot write a checkpoint
(`CHECKPOINT NOT WRITTEN`) — park runs as a session exits, so a silent no-op
discards the state the next session wakes on while nobody is watching.

Emission is the opposite:

> **The checkpoint file is the source of truth. The moment is its shadow.**

Absent config, malformed config, an unreadable store, a refused or raising
record write — every one of them still writes the checkpoint, prints **one
line** on stderr (silence in the absent case), and leaves the exit code
**unchanged**. Losing the shadow costs a row in a visualization; failing the
park over it would cost the checkpoint itself. Never read a `checkpoint
moment:` line as a failed park — the park's own rc and its
`parked <role> -> <path>` lines are what say whether state was saved.

## Which adapter automates this for you

> **Pickup column note (bus v3, 2026-07-27):** resident listeners are retired —
> pickup is now the v3 queue read (`coord-engine queue <team> --agent <you>`) on
> each scheduled wake ([`docs/coord/BUS-V3.md`](../../docs/coord/BUS-V3.md)).
> Listener entries below describe the pre-v3 mechanism; keep the *schedule*,
> replace the listener tick with a queue-reading wake.

| Harness | Lifecycle (rules 1–3) | Pickup (rule 4) |
|---|---|---|
| Claude Code (CLI/desktop) | hooks: SessionStart→resume+briefing, PreCompact/SessionEnd→park (`fulcra-agent-automation/scripts/claude-code/install-claude-code.sh`) | queue read on each scheduled wake (listener + its installer removed, cleanup slice 1) |
| Claude Cowork (desktop) | same as Claude Code (same core; same settings.json) | same queue-read wake |
| Claude web (claude.ai) | prose only — run rule 1 when the skill loads; rule 3 before ending | no background pickup; use cloud routines that open a duty-cycle session |
| Codex | `~/.codex/hooks.json` + app-thread automation (`fulcra-agent-automation/scripts/codex/install_codex_watch.py`) — the automation prompt embeds rules 1–3 | the same automation ticks the inbox |
| OpenClaw | managed block in `HEARTBEAT.md`/`BOOT.md` (`fulcra-agent-automation/scripts/openclaw/install_openclaw.py`) embeds rules 1–2; rule 3 (park) can't be automated from a prose block (no shutdown hook) — follow it as prose | HEARTBEAT tick |
| Hermes (Daytona sandbox) | fhd provisioner installs the Claude Code adapter inside the sandbox at provision time (standalone hermes-daytona repo) | queue read on the scheduled wake armed at provision time (the bundled listener retired with the stack) |

## Where to start — the re-entrancy probes

On waking (fresh session / cron), before doing anything else, probe whether a resume brief already
exists for you. Enter at the **first probe that fails** (per the repo's skill-quality pattern,
`docs/skill-quality-pattern.md`); a snapshot is a single-file overwrite and `resume` is a pure read, so
re-entry is always safe:

| Probe (run in order) | Command | Passes when | If it fails, enter at |
|---|---|---|---|
| Engine + auth usable? | `coord-engine doctor <team>` | exits 0 and the last line is exactly `doctor: healthy` | fix engine/auth first (see fulcra-agent-reconcile) — do NOT snapshot/resume against a broken engine |
| Resumable snapshot for me? | `coord-engine continuity resume <team> <agent>` | prints a line beginning `Resume:` (the newest snapshot across your tasks) — NOT the `No continuity snapshot found.` / `checkpoint age: unknown` pair | **Snapshot first** — you have no resume state; take a snapshot now (see [Usage](#usage)) before spending context, so the next wake resumes clean |
| Latest snapshot fresh? | `coord-engine continuity resume <team> <agent> --max-age <cadence>` | exits 0 and prints the checkpoint age | write one now (rule 2); rc 2 means missing, unreadable-age, or older than the bound |
| Am I continuity-stale? | `coord-engine health <team>` | your agent row has no `continuity-stale` flag | rules 2–3, then re-check |

Snapshot present → read the printed brief (objective / next actions / open questions / decisions) and
resume that work. `resume <team> <agent> <task>` narrows to one task; the bare `resume <team> <agent>`
form folds to the newest across all your tasks. Both are pure reads — safe to re-run any time.

## Snapshot schema (`member/<agent>/continuity/<task>/latest.json`)
```json
{ "schema": "coord.teams.continuity.v1",
  "checkpoint_id": "CHK-<iso>-<task>",
  "agent": "ash", "task": "build-l6",
  "objective": "ship the continuity layer",
  "decisions": ["chose structured json over freeform"],
  "next_actions": ["land the PR", "write the skill"],
  "open_questions": ["fold across tasks or per-task?"],
  "artifacts": ["https://github.com/.../pull/5"],
  "context_used_percent": 40, "transcript_path": null,
  "created_at": "2026-07-01T18:00:00Z" }
```

## Usage
```bash
# take a snapshot (e.g. before context runs out, at a natural stopping point, or on session end)
coord-engine continuity snapshot <team> <agent> <task> \
    --objective "ship the continuity layer" \
    --next "land the PR" --next "write the skill" \
    --open-question "fold across tasks or per-task?" \
    --decision "chose structured json" --context-percent 40

# on waking (fresh session / cron), get a resume brief — deterministic, not a prose re-read
coord-engine continuity resume <team> <agent> <task>
coord-engine continuity resume <team> <agent>          # newest across all the agent's tasks
coord-engine continuity resume <team> <agent> --max-age 1h  # fail rc 2 if missing/stale
```

## Role checkpoints, park, and briefing (A6)
```bash
coord-engine continuity checkpoint <team> --role <r> [--ref PATH]  # get/set the role's durable resume point
coord-engine continuity park <team> [--agent X] [--role R] [--objective "…"]  # session exit: snapshot every held role (or just R) + set its checkpoint_ref
coord-engine briefing <team> [--agent X] [--json]                  # session start: presence + board + inbox + needs-me + pending reviews + latest snapshot in ONE call
```
- `park` is the session-exit verb: each role you hold (fresh lease) gets a snapshot and the role doc's
  `checkpoint_ref` points at it — the next holder (or your next session) resumes from there via
  `checkpoint --role`. **`--role R` narrows it to exactly one role** (you must hold a fresh lease on
  `R`); with no flag it parks **every** held role. Use `--role` to confine a park to a dedicated role
  without touching your other roles' checkpoints — that is how `acceptance pair` parks safely against
  a loaded team store. It exits rc 2 with `CHECKPOINT NOT WRITTEN` when you hold no fresh roles (or,
  under `--role`, no fresh lease on that one), and rc 1 with the same `CHECKPOINT NOT WRITTEN` banner
  when the role state was *unreadable* rather than empty — retry that one before ending the session.
  Success therefore proves at least one checkpoint was written. Each written checkpoint also emits
  one timeline moment — see [the checkpoint channel](#timeline-visibility-the-checkpoint-channel);
  that emission can never change park's exit code.
- `resume` prints checkpoint age on every read. JSON adds the derived
  `checkpoint_age_seconds` field without changing the stored snapshot. With no
  snapshot, JSON is the explicit object
  `{"snapshot":null,"checkpoint_age_seconds":null,"error_code":null}` (not
  bare `null`). A timestamp up to one second ahead is treated as zero-age clock
  skew; farther-future `created_at` is invalid/unknown age and fails freshness
  like an unparseable timestamp. `--max-age DURATION` accepts
  seconds/minutes/hours/days (`30s`, `15m`, `12h`, `2d`) through
  `999999999d`. It exits rc 2 when the duration is invalid, the age is unknown,
  or the checkpoint exceeds the bound; JSON distinguishes those branches as
  `invalid-max-age`, `checkpoint-age-unknown`, and `checkpoint-stale` in
  `error_code` (successful reads carry `null`).
- `briefing` is the session-start verb and **tolerates absent add-ons** — with no presence/directives
  installed the sections are simply empty; it never fails a cold start.

### Parking for a successor (handoff doctrine)

A checkpoint is a promise to whoever wakes on it. One handoff (2026-07-22, an
agent resume) cost the successor its first hour reconciling three candidate
repo homes because the parked snapshot asserted state that was never pushed.
Rules, each earned there:

- **Never park asserting repo/artifact state you have not pushed AND verified.**
  "Verified" means an independent read-back of what the remote actually holds:
  `git ls-remote` on the exact ref **compared against the exact hash** you
  claim is there, or an equivalent remote fetch/download. Not the memory of
  having pushed — and not a `git push --dry-run`, which only tests a
  *prospective* push (it is the write-permission preflight in the checklist
  below, never proof the remote has your state). If the migration/import is
  still pending when you must park, the snapshot says so explicitly:
  **`IMPORT NOT DONE`**, followed by the exact recipe the successor runs (the
  literal mirror-push/clone commands) and the **access prerequisites** (who
  must grant what, before those commands can succeed).
- **One canonical home per artifact.** Name exactly one target repo/path for
  each piece of parked work. A checkpoint that names two candidate homes hands
  the successor a research task, not a resume.
- **The role doc exists before you park.** A parked role whose
  `team/<team>/roles/<role>.md` is missing leaves the successor claiming into a
  warning with review role-routing broken — creating it is the PARKING agent's
  job, not the successor's (see fulcra-agent-roles, "Establish a role").
- **Carry an operator pre-flight checklist.** Every human unlock your successor's
  bootstrap will need, enumerated in the parking doc so the operator grants them
  in ONE pass instead of the successor discovering them serially by failure
  (that same handoff burned ~6 round-trips this way). Template:

  ```markdown
  ## Operator pre-flight (grant BEFORE waking the successor)
  - [ ] Network egress: `fulcra.us.auth0.com` + `api.fulcradynamics.com`
        allowlisted in the session's environment (GET-ON-THE-BUS §3).
  - [ ] Auth tap: operator reachable for one device-flow approval
        (token cache does not survive a fresh container).
  - [ ] Source-repo access: repo(s) attached to the session AT START
        (cloud sessions are repo-scoped: account-level GitHub access does
        NOT reach them, cross-owner add_repo is unsupported — plan
        initial-source / mirror-push / same-owner fork; HARNESS-MAP wall 11).
  - [ ] Write permission: the successor's credential can PUSH the target
        repo — whichever access path applies (GitHub App grant, deploy key,
        or the accepted internal path: the FulcraBot fine-grained PAT for
        `ashfulcra/*`, with upstream contributor handoff where applicable) —
        verified with a `git push --dry-run` probe, not by assumption.
  ```

## When to use
- **Before context runs low** or at a natural stopping point — capture what you'd need to resume.
- **On session end / hand-off** — the next session (or another agent picking up the work) resumes clean.
- **In a cron/heartbeat wake payload** — call `continuity resume` first to re-establish state, exactly as
  `fulcra-agent-teams` asks agents to read `progress.md` first, but structured.

Pairs with `fulcra-agent-teams`' MEMORY.md / heartbeat conventions: keep those, and add a structured
snapshot for the work in flight. See [`references/continuity-cli.md`](references/continuity-cli.md).
