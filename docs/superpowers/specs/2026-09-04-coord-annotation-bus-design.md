# Coord on annotations: design

**Status:** proposed, awaiting Ash's review
**Author:** coord-boss (Tycho)
**Date:** 2026-09-04
**Implementer:** coord-maintainer · **Reviewer:** codex-reviewer

---

## 1. Why this exists

Ash, 2026-09-04: *"I MUST stop seeing anything about failures reading degraded
file folds."*

That is achievable in exactly one way, and this document exists because the
reason is now measured rather than argued.

### The measurement

Every operator-visible `*-degraded` marker in coord-engine — all nine families
(`review-fold`, `review-head`, `review-orphan`, `review-role`, `forge`,
`presence`, `inbox`, `threads`, `engagement`) — sits downstream of a `list_dir`
call and a wall-clock deadline check. Nine of nine. No exceptions.

A falsifiable test, run on the live store on 2026-09-04:

| `COORD_ATTENDANCE_SCAN_BUDGET` | dirs scanned | rc |
| --- | --- | --- |
| 20s | 14 / 592 | 3 (DEGRADED) |
| 90s | 40 / 592 | **0 — degradation gone** |

The test would have been falsified if scan depth had pinned at the same number
under both budgets, which is what a read error looks like. It did not. Scan
depth tracks wall clock at roughly 0.37 directories per second, and the
degradation *disappeared* at 90s — not because anything was repaired, but
because the clock was longer.

**A degraded marker is not an error report.** It is the fail-closed receipt of
an enumeration that ran out of time. `attendance=21/592` means the scan
correctly refused to claim coverage it did not have. The markers are the system
being honest.

Extrapolated, one full pass over the 592 review directories costs **~20–26
minutes**, on every wake, growing with the corpus forever. "Raise the budget"
buys silence at 26 minutes a wake and stops working next month.

So the markers cannot be fixed, suppressed, or budgeted away. They disappear if
and only if enumeration is removed from every operator-facing path.

### Why the existing rule did not achieve that

The rule already exists. `team/fulcra/_coord/rules/2026-08-21-stream-is-the-architecture.md`,
set after Ash's fifth correction, states it exactly:

> Any coord-engine change that ADDS a `list_dir` to a fold path is rejected at
> review regardless of its other merits.

It has been standing for two weeks. There are still **88 `list_dir` call
sites**, 56 of them in a single 13,848-line `cli.py`.

The rule failed because it is a *policy*, enforced by a reviewer's attention,
against a codebase where enumeration is the easiest thing to reach for. This
design's central move is to make the rule a *property of the type system*
instead: the fold engine is handed a transport that has no enumeration method
on it. A fold that wants to list a directory cannot be written.

---

## 1a. Why the LAST rebuild did not fix this

There is already an executed rebuild plan for this system:
`docs/superpowers/plans/2026-08-14-collect-coord-bus-rebuild.md`, 661 lines, 80
test-first steps, with a 446-line design beside it. It was not ignored. It was
**executed**.

Its own verified status banner records what happened:

> **STATUS (verified 2026-09-04): EXECUTED, and the module layout below is NOT
> what shipped.** [...] It names files that do not exist in the tree —
> `coord_engine/queue.py`, `routing.py`, `operator.py`, `parking.py`,
> `responses.py`, `review_store.py`, `fleet.py`, `cursor.py`, `acceptance.py`,
> `output.py` [...] because the implementation consolidated into
> `packages/coord-engine/coord_engine/cli.py` and its siblings instead.

The plan called for a fold layer, a cursor layer, a routing layer and an output
layer as **separate modules with separate responsibilities**. What shipped put
all of them in one file. That file is now 13,848 lines and holds 56 of the
codebase's 88 `list_dir` calls.

**This is the actual mechanism of failure, and it is not a coding mistake.** Once
every fold lives in the same module as every enumerator, sharing the same
transport object, "do not enumerate on a fold path" has no surface to attach to.
The enumerator is right there, already imported, already holding a live
transport. The 2026-08-21 review gate was then asked to hold a line that the
code's own structure had erased. It did not hold, and no amount of reviewer
attention would have.

So a design that merely *specifies* clean boundaries will be defeated the same
way. This one has to make the boundaries **load-bearing and checkable**:

1. **The fold engine is a separate package**, not a module inside
   `coord-engine`. A package boundary survives a refactor that a module boundary
   does not — you cannot casually reach across it, and dependency direction is
   declared in metadata rather than implied by an import.
2. **The no-enumeration property is a test, not a note.** A structural test
   asserts the fold package's transport has no `list_dir`, and that the fold
   package's import graph never reaches the enumerating transport at all. If
   someone consolidates the packages later, that test goes red before a human
   has to notice.
3. **A file-size ceiling is a CI gate on the new package.** The number matters
   less than its existence; the point is that "just put it in the big file" has
   to fail a check rather than pass a review.

Acceptance for this rebuild therefore includes *the shape of what shipped*, not
only its behaviour. A plan whose modules dissolve into one file has not been
implemented, however green its tests are.

---

## 2. What we are building

An extension of [`fulcra-workspaces`](https://github.com/fulcradynamics/agent-skills#-fulcra-workspaces),
not a replacement for it.

`fulcra-workspaces` gives agents a shared, durable, OKF-structured file space:
`team/<team>/task/`, `session/`, `knowledge/`, `member/<agent>/inbox/`. It is a
good substrate and we keep all of it.

What it does not give is a **signal**. Its discovery primitive is directory
enumeration — an agent finds work by listing its inbox, and finds team activity
by listing task directories. That is correct and simple at the scale the skill
is written for. At 592 review directories and 3,266 tasks it is the defect
measured above.

This design adds the missing layer: **annotations are the signal, files are the
content the signal points at.** That is the capability we can then take back to
the base skill as a demonstrated improvement.

---

## 3. Architecture

Three planes, strictly separated. The separation is the whole design.

### 3.1 Signal plane — annotations

Every coordination event is one MomentAnnotation record on a team channel. The
payload is small, fixed, and self-describing:

```json
{
  "v": 1,
  "at": "2026-09-04T13:45:00Z",
  "from": "coord-boss",
  "to": "coord-maintainer",
  "kind": "open",
  "slug": "p1-merge-lane-pr-694-7ca915c9",
  "pri": "P1",
  "ptr": "team/fulcra/task/p1-merge-lane-pr-694-7ca915c9.md"
}
```

`kind` is a closed set: `open`, `close`, `claim`, `release`, `note`.

`ptr` is **one file path**. Never a directory, never a glob, never absent on an
`open`. The event carries enough to route and prioritise; the file carries the
bulk. An agent that only reads events knows what it owes and to whom, and pays
nothing for content it does not open.

### 3.2 Content plane — files (unchanged fulcra-workspaces / OKF)

Files hold everything bulky: task bodies, review docs, session summaries,
artifacts. Layout stays exactly as `fulcra-workspaces` specifies, so the two are
interoperable and a workspaces-only agent can still read a coord team space.

**Files are addressed only by `ptr` from an event. They are never listed to
discover what exists.**

Ash's requirement — *"any task should only point at specific files related to /
expanding on that task"* — becomes a property of the task document. A task
declares its own related files in frontmatter:

```yaml
---
type: Task
slug: p1-merge-lane-pr-694-7ca915c9
points_at:
  - team/fulcra/review/docs-qa-2026-09-04-plan-banners/verdicts/15048418--codex-reviewer.md
  - team/fulcra/_coord/responses/p1-merge-lane-.../reply.md
---
```

A fold never *discovers* a task's related files. The task names them, finitely,
and the author is responsible for that list. Expanding a task means appending a
pointer, which is an authored act with an event attached.

### 3.3 Fold plane — one durable checkpoint per agent

Each agent has exactly one checkpoint document:

```json
{
  "v": 1,
  "cursor": "2026-09-04T13:22:04.287653Z",
  "open": { "<slug>": { "pri": "P1", "from": "...", "ptr": "...", "at": "..." } },
  "unread_events": 0,
  "unreadable_pointers": []
}
```

A fold is: read annotations forward from `cursor`; apply each event to `open`
(`open` adds, `close`/`release` removes, `claim` annotates); write the new
checkpoint. **Cost is O(new events). Zero directory listings, always.**

This is not speculative. The existing `obligations.py` already works this way —
it contains **zero `list_dir` calls** — and today's live passes cost *0 events,
0 doc reads*. The measured comparison from the 2026-08-21 rule doc:

```
stream+cursor needs-me:  2.2 s, rc 0, clean, 0 enumeration
file-based    needs-me:   87 s, rc 3, 14 degraded markers, 300+ dirs walked
```

### 3.4 The enforcing interface

The fold library is constructed with a transport that exposes exactly two
methods:

```python
class PointerTransport(Protocol):
    def read_classified(self, path: str) -> tuple[str | None, Literal["ok", "absent", "error"]]: ...
    def read_events(self, channel: str, since: str) -> Iterator[Event]: ...
```

There is no `list_dir`. There is no `glob`.

**But this does NOT prevent enumeration, and the original version of this
section claimed it did.** That claim was wrong and was falsified four separate
ways over seven review rounds on 2026-09-04:

1. A type removes one *method*. `os.listdir`, `os.scandir`, `os.walk` and a
   subprocess launching the store CLI all remain, in any module.
2. Static gates that hunt those by name are defeated by **aliasing**, and there
   are unbounded spellings: a bare import alias, an assignment alias, an
   annotated assignment, a local binding of an owner operation. Two reviewers
   found a fresh one in each of three consecutive rounds; each round closed its
   counterexample and left the class open.
3. An **in-process** harness that patches those capabilities to raise is
   escapable from inside the same process — originals reachable via `gc`,
   `io.open` unlisted, the store fake leaking its corpus through a private
   attribute, and the test itself detectable. A gate that shares an address
   space with its subject is defeatable by its subject.
4. Only the **kernel** can make a capability absent.

So the enforcement is a **process boundary**, not a type: the fold runs under an
OS sandbox with the store served from outside it, and a fold that completes
there provably did not enumerate in that run. The type stays because it is cheap
and it documents intent — but it is a convenience, not the guarantee.

**The guarantee, stated at the strength it actually holds:** *these specific
capabilities were denied by the kernel and the fold still completed.* Not "the
fold ran with no capabilities" — a deny-default profile aborts the interpreter
at startup, so the measured profile is allow-default with targeted kernel
denies. And not "enumeration is impossible to write" — it is writable; it fails
its tests.

One property does hold by construction rather than by checking, and it is worth
more than any gate here: **the import machinery itself enumerates**, so under
denial an uncached module cannot be imported at all. That closes the
generated-module bypass because of what the system *is*, not because something
looks for it.

A structural test asserts this directly: `assert not hasattr(fold_transport,
"list_dir")`, and a test that the fold module's import graph never reaches the
enumerating transport. The invariant is itself under test.

---

## 4. What replaces the degraded vocabulary

With no enumeration there is no wall-clock-over-corpus, so the nine `*-degraded`
families cease to exist. Two things can still be unknown, and both are bounded
and name exactly one thing:

| State | Meaning | Operator sees |
| --- | --- | --- |
| `unread_events: N` | the annotation read stopped early; N events past the cursor are unapplied | `fold: 12 events unread past 2026-09-04T13:22Z — the answer is missing those` |
| `unreadable_pointers: [slug]` | a specific pointed-at file could not be read | `fold: pointer for <slug> unreadable — that one row is UNKNOWN` |

Neither can grow with corpus size. `unread_events` is bounded by events since
the last successful fold; `unreadable_pointers` names individual slugs. Neither
is ever the phrase "degraded fold", because neither is a fold that gave up part
way through a corpus it should not have been walking.

**Retained fail-closed discipline:** an unknown never reads as clear. This is
not a softening of the current honesty — it is the same honesty about a much
smaller and bounded set of failures.

---

## 5. Migration

### 5.1 Parallel bus, proven, then cut over

New engine, new package, new team prefix. Both run concurrently.

1. **Seed.** Import the currently-open obligations (253 as of 2026-09-04) as one
   `open` event each on the new channel, carrying the existing task path as
   `ptr`. Nothing else is imported.
2. **Dual-emit.** The old engine gains one hook: every obligation transition it
   makes also emits the corresponding annotation. This is the only change to the
   old engine, and it is the bridge that makes comparison possible.
3. **Shadow.** Both engines answer "what does coord-boss owe" every pass. A
   comparator diffs the two open sets and records agreement or divergence.
   Divergence is a finding to investigate, never a number to tune.
4. **Cut over** when N consecutive passes agree (N to be fixed in the plan; the
   comparator's output is the evidence, not a claim that it works).
5. **Freeze.** The old prefix becomes read-only. It is not deleted.

### 5.2 History

Ash: *"I care about having the history for review, but not carrying the full
history forward if that complexity is making it impossible to have a good
product."*

So: the entire existing bus stays exactly where it is, readable, frozen. The new
bus starts from the 253 open rows and nothing else. History is a thing you go
and read, not a thing every fold pays for on every wake.

### 5.3 Rollout

coord-boss alone until a full day of ticks, sweeps and folds is clean on the new
bus. Then one agent at a time, each with its own verification. Every other agent
stays on the current bus until its own move.

---

## 6. Verb surface

Minimal core. Five verbs:

| Verb | Does |
| --- | --- |
| `emit` | write one annotation event |
| `fold` | advance my checkpoint, print what I owe |
| `claim` | take an obligation |
| `close` | retire one, with evidence `ptr` |
| `status` | what does the fold say right now |

The current engine has 38. Every one of the other 33 must be *asked for by an
agent that actually needs it*, and arrives with a stated cursor and no
enumeration. Most exist because something was hard once; carrying all 38 forward
carries the reasons they were hard.

The kill/keep decision for each of the 38 is recorded in the implementation
plan, with a reason per verb, so the list is a reviewed artifact rather than a
judgement call made in passing.

---

## 7. The one honest carve-out

`fulcra-workspaces` lets a human drop a file into `member/<agent>/inbox/` with no
event. That is a real and good affordance, and it is the one case where
enumeration is unavoidable.

Resolution: **one bounded reconciler, off every fold path.** It lists inbox
directories on a schedule and emits one annotation per newly seen file. It is
allowed to be slow, and it is allowed to degrade, *because no fold depends on
it* — a fold reads only events. If the reconciler is behind, a human's dropped
file is late; nothing else is affected and no fold reports degradation.

This carve-out is precisely the improvement we can take back upstream: keep the
inbox for humans, put the signal on annotations.

---

## 8. Testing

- Every fold test drives the CLI and asserts on the stored checkpoint, not on a
  decision function. This repo has had three NameError-class defects pass full
  unit suites and get caught only by running the verb.
- Every test file is **mutation-verified**: the test must be shown to fail when
  the behaviour it names is removed. An unverified test is not evidence.
- The no-enumeration invariant has its own structural test.
- A degradation-vocabulary test asserts no output path can emit the string
  `degraded` — the old vocabulary is gone by construction, not by convention.
- The comparator from §5.1 is a test artifact in its own right, and its
  divergence log is the cutover evidence.

---

## 9. Open questions for review

1. **N for cutover.** How many consecutive agreeing passes before the switch?
   Proposed: 24 hourly passes, i.e. one full day.
2. **Channel granularity.** One annotation channel per team, or one per agent?
   Per-team is simpler and matches the current mesh pattern; per-agent makes a
   fold's read narrower. Recommend per-team, revisit if fold cost grows.
3. **Event retention.** Annotations are durable, but a fold that has been away
   for a month reads a month of events. Does a checkpoint need periodic
   compaction into a snapshot, and if so at what age?
4. Whether the five core verbs are the right five, which is exactly the kind of
   thing codex-reviewer should push back on.

---

## 10. What this does not do

- It does not fix the pre-fence publication overwrite. Three pre-2.0 hosts
  (DeskBookPro v1.11.0, vps-heartbeat v1.11.0, MacBookPro.localdomain v1.6.9)
  are still rewriting the shared aggregate. That is an operator decision already
  in Ash's batch and it is orthogonal: a stream fold does not read the aggregate.
- It does not migrate the 792 Python anti-slop findings or the 273 TypeScript
  ones. Those are a separate, unmade decision.
- It does not delete anything. The old bus is frozen, not removed.
