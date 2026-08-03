# T4 evidence: cross-platform park/resume acceptance (2026-08)

Task 4 of the promise plan (`team/fulcra/reports/2026-08-02-promise-plan.md`):
prove the continuity promise end to end — one agent parks a session with
genuine in-flight state, a *different* agent on a *different platform* cold-resumes
it, then the exchange reverses. The run is only an acceptance if divergences
between the written procedure and the real surface are recorded as findings,
so this document is findings-first.

**Participants.** coord-boss (Claude, cloud container, the parker) and
coord-maintainer (different vendor harness, Ash's MacBook Pro, the cold
resumer). Neither shares a machine, filesystem, or session with the other;
the only shared state is the Fulcra account.

## Findings

### F1 — the plan's resume command does not exist as written

The plan said `coord-engine continuity resume fulcra --agent coord-boss`.
That fails with `error: unrecognized arguments: --agent`. The real surface is
asymmetric between the two halves of the same round trip:

```
continuity park   [-h] [--agent AGENT] [--objective ...] team      # agent is a FLAG
continuity resume [-h] [--json] team agent [task]                  # agent is POSITIONAL
```

Correct form: `coord-engine continuity resume fulcra coord-boss`. Low severity
alone, but it is precisely the class T4 exists to catch: a cold resumer
following the written procedure fails at the first command. (Found by
coord-maintainer, pre-run, 2026-08-03.)

**Disposition:** align the two surfaces or fix every doc that spells the
command; the plan text itself ruled that a verb-surface divergence is a
finding.

### F2 — resume presents an 18-day-old checkpoint as current: rc 0, no age field, no warning

Running the *correct* command on 2026-08-03 returned a checkpoint dated
2026-07-16 — citing a rollout two minor versions behind and an agent-dark
count from mid-July — with **no age field, no staleness warning, and rc 0**,
byte-shaped identically to a fresh park. A cold resumer acting on it would
have chased work that finished weeks ago. Quiet-because-current and
quiet-because-frozen are indistinguishable, again — the same defect family as
the rc3 conflation and the health fold's last-reconcile-as-current rendering.
(Found by coord-maintainer, pre-run, 2026-08-03.)

**Disposition:** `resume` should print checkpoint age on every invocation and
go loud (distinct rc or banner) past a staleness threshold; a `--max-age`
gate would let acceptance runs assert it. Filed for the 5b/cleanup wave.

### F3 — parking "at session exit" writes a contentless checkpoint that satisfies nothing

coord-boss's own 11:10Z park — written by the generic at-session-exit path —
carried `objective: "parked role coord-boss at session exit"` and empty
`decisions`, `next_actions`, and `open_questions`. It is a checkpoint in name
only: a cold resumer receives no thread to pick up, yet the file exists, is
fresh, and `resume` serves it at rc 0. A park with no content is
indistinguishable (to every reader and to F2's missing age check alike) from
a park that carries the agent's real thread. (Found by coord-boss during the
run, 2026-08-03.)

**Disposition:** park should refuse or warn when objective is boilerplate and
all three state lists are empty; the skill text should require real content
at park time.

## The run

### Step 1 — coord-boss parks with genuine in-flight state (DONE, 2026-08-03 ~13:40Z)

```
coord-engine continuity park fulcra --agent coord-boss \
  --objective "T4 acceptance run: drive promise-plan to done. PR 509 merged (main 4ebe51ed, pin pp-4ebe51ed); pr-513 docs QA at round 5 head 82660379 awaiting codex-reviewer verdict; PR 514 (README agent-first demo) open awaiting Ash's merge nod." \
  --next "Merge pr-513 on APPROVE + dual-green CI (uv-workspace resolve + macos test)" \
  --next "Merge PR 514 on Ash's nod; both workflows manually dispatched on readme-agent-first" \
  --next "T4 step 3 after coord-maintainer's step-2 cold resume: run continuity resume fulcra coord-maintainer (agent is POSITIONAL, not --agent), then file docs/coord/evidence/continuity-park-resume-2026-08.md with all findings" \
  --next "After fixes land: full dead/obsolete code + docs sweep per Ash 2026-08-03" \
  --open-question "W1-s5 schema ruling owed to codex-coder — their ask doc still unlocated" \
  --open-question "Who is the remaining engine-1.9.0 writer on the bus (coord-maintainer resync warning #3)? T2 census is the instrument"
# -> parked coord-boss -> team/fulcra/member/coord-boss/continuity/role-coord-boss/latest.json
```

Note the park's own content became stale within the hour (pr-513 merged as
851fa067; W1-s5 was ruled) — which is F2's point from the other side: a
checkpoint is a snapshot, and only an age field lets the resumer weigh it.

### Step 2 — coord-maintainer cold-resumes coord-boss's park (DONE, 2026-08-03 ~17:35Z)

coord-maintainer, on macOS in a different vendor session, using only the
checkpoint and the bus (their report, quoted):

> I cold-resumed coord-boss's 13:40Z park on macOS in a different vendor
> session, using only the checkpoint and the bus. It carried real in-flight
> state — PR 509 merged at `4ebe51ed`, pr-513 at round 5, PR 514 awaiting
> your nod. Coord-boss had also already folded my earlier finding into their
> own next-actions ("agent is POSITIONAL, not --agent"), so that loop closed.

The data half of step 2 worked. The *choreography* half produced findings
F4–F6 below: their reverse park silently wrote nothing, and the command
chain announced success anyway.

### Step 3 — coord-boss cold-resumes coord-maintainer's park (DONE, 2026-08-03 ~18:0xZ)

After coord-maintainer replaced the silent no-op with a real checkpoint (via
`continuity snapshot`) and verified it by resuming their own id:

```
$ coord-engine continuity resume fulcra coord-maintainer ; echo rc=$?
Resume: role-coord-maintainer (as of 2026-08-03T17:35:28.927983Z)
  agent: coord-maintainer
  objective: T4 step-2 COMPLETE: cold-resumed coord-boss's 2026-08-03T13:40Z park
    on Ashs-MBP-Work (macOS, Claude Code, different vendor session) using only the
    checkpoint and the bus. Parked for coord-boss's step-3 reverse resume.
  next actions:
    - coord-boss runs step 3: ... Verify you receive THIS objective, not the
      2026-07-22 context-loss park.
  ...
rc=0
```

Provenance verified by content, not by rc: the checkpoint's own next-action
instructed the resumer to confirm it received the 17:35 objective rather than
the stale 2026-07-22 park — and it did. Round trip complete in both
directions, cross-machine, cross-vendor, same day.

### F4 — park is role-gated and silently no-ops at rc 0 when the agent holds no roles

coord-maintainer's reverse park printed "coord-maintainer holds no fresh
roles — nothing to park", **wrote no checkpoint, and exited 0**. Any caller
treating rc 0 as "a checkpoint now exists" is wrong, and nothing in the
output contract says so machine-readably.

**Disposition:** park must exit nonzero when it snapshots nothing
(dispatched to codex-coder as P0, 2026-08-03).

### F5 — resume cannot tell you that what you got is not what was parked

Before coord-maintainer's fix, `resume` for their id returned **rc 0 and a
confident, plausible brief dated 2026-07-22 — twelve days stale**. Combined
with F4: had step 3 run in that window, it would have received a success
code, a plausible brief, and twelve-day-old state — and this acceptance run
would have **PASSED while transmitting nothing**. The test built to catch
the silent-success class nearly emitted a silent success.

**Disposition:** same fix family as F2 — age on every resume, `--max-age`
gate (dispatched to codex-coder as P0, 2026-08-03).

### F6 — the orchestrator repeated the pattern: a `&&` chain announced success it never verified

coord-boss (this document's author) supplied the step-2 command chain
`resume && park && tell`, where the `tell` declared "mine is parked" —
guarded only by exit codes, on the same day the fleet adopted the doctrine
that silence and rc 0 are not evidence. F4's silent no-op sailed through the
`&&` and the bus carried a false claim until coord-maintainer caught it.
Human-shaped lesson, filed against the coordinator, not the engine:
**announcements of state must be generated from verified state (read it
back), never chained off exit codes of the commands that were supposed to
produce it.**

## Acceptance verdict

**The data plane passes; the promise as shipped does not — yet.**

- PASS: a checkpoint with genuine in-flight state parked on a cloud
  container round-tripped through a cold macOS session on a different vendor
  and back, same day, using only the account. The store, the paths, the
  formats, and both directions of resume all worked.
- FAIL (promise as experienced): every finding F1–F6 is an instance of one
  defect class — **silent no-op or silent staleness rendered as rc-0
  success**. A cold resumer following the docs faithfully can receive
  nothing (F3, F4), receive the wrong thing (F5), receive the right thing
  with no way to know its age (F2), or fail at the first command (F1) — and
  in no case does the surface say so loudly.

T4 therefore certifies the continuity *substrate* and indicts the
continuity *interface*. The verdict flips to PASS when the two P0 fixes land
(park fails loud on empty; resume prints age and enforces `--max-age`) and a
re-run of this choreography completes with zero human rescue. That re-run
should take under ten minutes — which is the promise.
