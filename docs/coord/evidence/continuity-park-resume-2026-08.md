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

### Step 2 — coord-maintainer cold-resumes coord-boss's park (PENDING)

*Transcript and observations to be inserted from coord-maintainer's step-2
report when filed.*

### Step 3 — coord-boss cold-resumes coord-maintainer's park (PENDING)

*Transcript to be inserted after step 2 completes and coord-maintainer parks
their side: `coord-engine continuity resume fulcra coord-maintainer`.*

## Acceptance verdict

*To be written after steps 2–3: does the round trip, with the findings above
dispositioned, satisfy the README's promise that work parks in one session
and resumes in another — different day, different machine, different model or
vendor?*
