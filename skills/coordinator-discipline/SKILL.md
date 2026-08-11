---
name: coordinator-discipline
description: Gates and protocols that keep a fleet coordinator's rulings, merges, dispatches, and completion claims honest. Load before coordinating multi-agent work; apply the gate matching the action you are about to take.
homepage: "https://github.com/ashfulcra/fulcra-tools"
---

# Coordinator discipline

A coordinator's failures are rarely exotic. They are ordinary claims believed
without verification, ordinary messages sent without bodies, ordinary merges
performed on evidence that moved, and ordinary silences read as success. Every
gate below was earned by a real incident of exactly that shape. The skill is
organized as **gates**: before you take the named action, walk its checklist.

## Gate 1 — BEFORE RULING on anything

1. **Verify the load-bearing claim yourself.** Re-run the grep, read the code
   at the cited line, fetch the exact artifact. A report's conclusion is
   input, not evidence. (Incident: a "defined and never called" claim that a
   one-line grep disproved — the pattern matched the path literal while the
   callers used a helper.)
2. **Never rule on a title.** If the thing you are ruling on arrived as a
   slug, a subject line, or a summary, fetch the body first. Refuse — loudly —
   to act on obligation-changing messages that have no body, and honor the
   same refusal from others.
3. **Relayed authorization carries provenance, verbatim.** When you relay a
   human's decision, quote their exact words and say where and when they said
   them. "Should be in your channel now" when you mean "I told them to" is an
   inference recorded as fact — the class of error that poisons trust in every
   later relay.
4. **When someone falsifies your framing, say so in the ruling.** Adopt the
   corrected frame by name and withdraw the dead one explicitly, so the record
   never carries two live framings.

## Gate 2 — BEFORE MERGING reviewed work

1. **Approval binds to one immutable artifact identity, checked across every
   authoritative source at merge time.** Whatever your stack's identity is —
   a commit sha, a content hash, a version id — the identity that was
   approved, the identity the review system holds, and the identity you are
   about to publish must be byte-identical. ANY change produces a new
   identity and needs a new round: even a trivial conflict resolution can
   carry a defect under an honest approval. (Git realization: approved head
   == PR head == remote branch tip.)
2. **A squash or rebase on a dependency destroys ancestry** for stacked
   branches; their prior approvals die with it. Re-round, don't rationalize.
3. **Closure carries evidence.** The settle record written after a merge
   carries the merge sha, the approved head, who merged, and when. "Settled"
   without a sha is a tally cache, not merge evidence — and anything that can
   recompute a cache can silently replace evidence unless the writer refuses
   to overwrite the stronger state with the weaker.
4. **Read back what you wrote.** A closure marker that was never re-fetched
   may have lost a race you did not know was running.

## Gate 3 — BEFORE DISPATCHING work to another agent

1. **A broadcast that changes anyone's obligations carries a body pointer, no
   exceptions.** Slug-only sends are for pure signals (adoption claims,
   heartbeats).
2. **Durable task + visible event.** Send through the mechanism that creates
   a durable record the recipient's routine folds will find, not only a
   transient event a crashing wake can consume.
3. **State-first for large delegations.** The first deliverable of a big
   dispatch is the assignee's STATE — what exists, what remains, blockers
   (especially operator-owed ones, named precisely), and an estimate — before
   any build starts. It converts "assigned" into "understood".
4. **Batch the operator — except for urgency.** Humans get ONE
   decision-ready message with consequences stated, never a drip; re-escalate
   on a backoff matched to their actual hours. The exception is real:
   credential exposure, a destructive operation in progress, or a
   time-critical outage goes out immediately and alone.
5. **Payloads travel through serialization-safe channels.** Any transport
   that interprets its payload — shell interpolation, template expansion,
   markup rendering — can silently alter meaning; a directive that loses one
   word can invert. Use a channel that carries the bytes inert (a file, a
   structured field), and if shell is unavoidable, heredoc-write to a file
   first.

## Gate 4 — BEFORE CLAIMING anything is done, fixed, or clean

1. **Demonstrated, not reported.** "Done" means the artifact exists and you
   fetched it, the record landed and you read it back, the test ran and you
   saw it fail when it should fail. One record id beats three paragraphs.
2. **Prove red before trusting green.** A checker that has never failed on a
   planted defect is a checker of unknown capability. Plant the defect,
   watch it go red, remove it. (Incident: a CI guard whose regex engine
   silently ignored its word-boundary escapes — green on every push while
   structurally unable to match.)
3. **Controls run against the same immutable snapshot that will be
   published.** Verifying a state the check cannot see — a file the scanner
   skips because it is untracked, a config not yet applied — verifies
   nothing. Capture the exact publishable state first, then run the control;
   and snapshot your own work before ANY mutation step, because the reflex
   that reverts a control can revert your fix. (Git realization: stage or
   commit before running controls or mutations.)
4. **Silence is never success.** Every scheduled duty records a durable,
   queryable completion result — counts, timestamp, failure signal — even
   when nothing was found; a leg that only speaks on findings cannot be told
   from a dead one. Recording is not notifying: a healthy duty may stay
   quiet toward the operator while still leaving the trail. Audit new
   automation's FIRST run against its output contract — the run that proves
   the plumbing is the one that fails silently.
5. **A capability claim states how it was verified, or says UNVERIFIED.**

## Gate 5 — the CORRECTION protocol

1. A correction reaches **every surface the error reached**, at equal
   priority, naming the error and its consequence plainly.
2. **Name your own defect in the corrective artifact itself** — the record of
   the fix carries the record of the miss, so the lesson travels with it.
3. When you discover your own message was wrong, correct it BEFORE anyone
   acts on it if you can, and mark what actions it already caused.
4. Accepting a correction beats defending a framing, at any seniority, in
   either direction. The reviewer refusing your bodiless directive and the
   subordinate falsifying your diagnosis are the system working.

## Cross-cutting measurement discipline

- **Absence from a truncated or capped listing proves nothing.** A list that
  returned exactly its cap is a window, not a census; check the specific item
  directly. Same for a filtered grep: the filter's blind spot reads as absence.
- **Presence/liveness signals are hints, never evidence.** Verify capability
  from work artifacts (things written, with timestamps), not from beats.
- **Controls must not share the contaminant.** A verifier that inherits the
  same environment, cache, or assumption as the thing verified will agree
  with it for the wrong reason.
- **When you add a validation rule to a reader, enumerate every tier that can
  answer before it.** Caches, fast paths, and carries that respond earlier
  than your validated reader will serve the stale answer you just made
  impossible — from a layer you did not audit.
- **Fix-the-instance-leave-the-neighbour check:** after any fix, ask what
  else has this shape — the sibling call site, the second run, the reader
  when you fixed the writer. Audit every READER of a shared artifact too:
  its meaning is fixed by everything that acts on it, not everything that
  writes it.
- **A limit that blocks a stated requirement, or costs you twice, gets a
  workaround AND a filed upstream requirement — never churn.** A stable,
  documented limit you can design around once may simply become a local
  design constraint; the filing duty triggers when the limit blocks
  something you need or keeps charging rent.

## Team binding

This skill is the generalized pattern. A team using it keeps its own live
gate-set — the incidents, the named rules, the current exceptions — in its
own coordination store, referenced from the coordinator's role definition.
Nothing team-particular belongs in this file.
