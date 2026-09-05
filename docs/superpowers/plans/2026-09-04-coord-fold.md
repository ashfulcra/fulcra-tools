# coord-fold: Coord on Annotations Implementation Plan (r39)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Spec:** `docs/superpowers/specs/2026-09-04-coord-annotation-bus-design.md` (branch `claude/coord-boss-handoff-resume-60sjua` @ `3a4687b0`). Read it whole first; §1 and §1a are why every structural requirement below exists.

**Directive:** coord-boss `65761fbd` (P0). **Reviewers (both required):** codex-reviewer, codex-coder. **Implementer:** coord-maintainer.

**r5 is gated against itself before it is filed.** Round-4 verdicts (both reviewers) found the plan's structural suite failed the plan's own code. Task 0 below is a script that materializes every path-tagged code block in this document into a scratch tree and runs the structural gates against it; r5 was filed only after that run was green, and the run's output is in the filing note. Every code block that lands in the package is tagged `# packages/coord-fold/<path>`; a block that is not tagged does not exist as far as the gate is concerned, so it is not allowed to matter.

**Goal:** A separate `coord-fold` package whose fold engine is shown, **in a measured run under kernel denial, to reach the store only through observed, bounded requests** — we do not claim enumeration is impossible to write; we claim a fold that enumerates fails its tests, and only that (G29) — running in parallel with the old bus until a comparator proves agreement, then cut over. Seven rounds of syntactic gates (r2–r7) each closed one spelling and left the class open; coord-boss ruled (`cdfe666e`) that a static check cannot prove a negative about an unrestricted Python module, and the spec's §3.4 claim that a transport type *prevents* enumeration was wrong: a type stops a method call, not `os.listdir` or a subprocess. The guarantee is now behavioural **and enforced by the operating system at a process boundary** (r9): an in-process monkeypatch harness was shown escapable in one round (originals reachable via `gc`, `io.open` unlisted, the fake's corpus reachable through the object graph, the test detectable), so the proof runs the *production* reader inside an OS sandbox against a store that lives in *another process* and logs every request.

**Architecture:** Three planes (spec §3). Signal = one MomentAnnotation per event on a team channel. Content = unchanged OKF files addressed only by `ptr`. Fold = one checkpoint per agent, advanced by reading events forward from a cursor at O(new events). The fold is handed a **reader** with two public methods and three sealed subcommand calls; writes live on an unrelated class; process launch exists in exactly one module. The only changes to `coord-engine` are the seed export, the dual-emit mirror, and the comparator — old side, allowed to enumerate.

**Tech Stack:** Python ≥3.11, stdlib only inside `coord_fold`. Build: hatchling. Test: pytest<8. Dependency: `fulcra-common` only. The Fulcra API is reached by shelling out to the `fulcra-api` CLI — five fixed argv shapes, all in `transport.py`, nowhere else.

---

## Global Constraints

G-numbers are stable across revisions so verdicts can cite them.

- **G1.** `kind` is a closed set: `open`, `close`, `claim`, `release`, `note`.
- **G2.** `ptr` is one file path — never a directory or glob, never absent on `open` or `close`.
- **G3.** Event payload v1 fields, exactly: `v`, `at`, `from`, `to`, `kind`, `slug`, `pri`, `ptr`.
- **G4.** Checkpoint v1 fields, exactly: `v`, `cursor`, `open`, `unread_events`, `unreadable_pointers`, `seen`, `generation`, `writer`. `seen` dedupes the boundary event because the cursor is inclusive (Ruling 1); `generation`/`writer` carry lost-update detection (Ruling 2).
- **G5.** `PointerTransport` exposes exactly `read_classified` and `read_events`.
- **G6.** Separate uv workspace package; `pyproject.toml` must not depend on `coord-engine`.
- **G7.** No enumeration method on reader, writer, or fakes; the import graph never reaches `coord_engine`.
- **G8.** 400 lines per `.py` under `coord_fold/`, recursive, as a CI gate.
- **G9.** Every fold test drives `coord_fold.cli.main([...])` and asserts on the stored checkpoint in the fake store.
- **G10.** Every test file is mutation-verified.
- **G11.** No output path may emit the string `degraded`. Two diagnostics replace it, and they mean different things. `unreadable_pointers: [slug]` is an UNKNOWN (a read that did not answer) and exits 3. `unread_events: N` is a **remainder, not a failure, and exits 0** (Ruling 4, below). An unknown never reads as clear.
- **G25.** *(r6, Ruling 4 — its own paragraph, because it is the distinction Ash asked for.)* **A capped pass is not degraded.** Ash said he must stop seeing failures reading degraded file folds. The corpus fold's *partial* meant "I gave up part way through something I should not have been walking", and its remainder was bounded by **corpus size**, so it grew forever. The stream fold's *partial* means "I applied 500 of 700 new events, the cursor is here, 200 remain", and the remainder is bounded by **new events**, so the next pass gets them. Same shape of answer, opposite meaning. A pass that hits `max_events` applies what it read, advances the cursor to the last applied event (G26), reports `unread_events: N`, and **exits 0** — it did exactly what it promised and said so. The word `degraded` may not go near it. The **only** error is a pass that applies zero events while events exist: that is no progress, and it exits non-zero.
- **G26.** *(r6, Ruling 1; refined by G31)* **Lossless cursor.** The cursor advances to the `recorded_at` of the last observed record with **no unapplied relevant event before it** — never to `now`, never past a gap (an unapplied relevant event). The cursor is the only durable claim of coverage this design makes; if it could pass unapplied events, `unread_events` would become the sole record of the gap and any pass that lost that counter would silently claim coverage it never had — the failure class this rebuild exists to end, reintroduced in the one field everything trusts. Checkable consequence, made a test: re-running a fold from the stored cursor yields the same open set.
- **G27.** *(r6, Ruling 2)* **Lost-update detection, not CAS.** The store has no compare-and-swap (AGENTS.md records this); requiring one would build on a guarantee the platform does not offer, which reads as safety and is not. The checkpoint carries `writer` and a monotonic `generation`; a fold loads, computes, **re-reads before writing**, and if the generation moved it **refuses** — exit non-zero, visible, no silent retry — and never overwrites. It cannot prevent the race; it refuses to lose the update, which is the honest ceiling. The contended case is one agent running twice (two hosts, a duplicated cron) — the same double-acting condition coord already alarms on via the lease nonce — so the refusal says that **by name**.
- **G28.** *(r6, Ruling 3)* **No compaction in v1; never delete events.** Bound the fold *work*, not the stream *history*. A fold away for a month reads a month of events: O(new events since *its* cursor), correct by definition. Compaction is a second source of truth that can disagree with the stream — precisely what this design removes — and it would arrive with no measurement forcing it. If catch-up cost ever becomes real, the fix is a snapshot that is derivable, discardable, and provably equal to a replay from empty; built then, against a number, not now.
- **G12.** Six verbs: `emit`, `fold`, `claim`, `release`, `close`, `status`.
- **G13.** *(r13, codex-reviewer)* Parallel bus proven then cut over: seed, dual-emit, shadow, cut over after **24 agreeing passes spanning ≥24 wall-clock hours** with observed transitions **and one injected divergence/recovery drill** (Task 14 forces a divergence, watches the comparator report it, repairs, and requires the agreeing run to restart from zero), freeze.
- **G14.** coord-boss alone first, then one agent at a time.
- **G15.** Never hardcode the channel; resolve `data_type` from `team/<team>/_coord/bus-v4/records.json`.
- **G16.** No secrets. **G17.** Commits authored as `114089064+ashfulcra@users.noreply.github.com`.
- **G18–G23.** *(r2–r7)* **SUPERSEDED by G29** (coord-boss `cdfe666e`, 2026-09-04): ownership manifest, import DAG, closed call allowlist, alias-resolved launcher/enumeration/owner-op bans, required-call shape, mass ceilings. Each closed a specific counterexample (r2 shim modules, r3 one-statement wrappers, r4 `from subprocess import run as launch`, r5 `os.listdir` inside the allowlisted module + alias-bound owner ops + unreachable `fold.run`, r7 `x: object = reader.read_events` — `AnnAssign`, which no alias map handled) and left the class open. The cheap, *true* structural facts among them survive as boundary truths in Task 1 (G5–G7, ownership-defined-where-planned, DAG, tree = manifest); the rest are deleted, not kept as ceremony. Numbers retained so old verdicts still resolve.
- **G24.** *(r5)* **The plan is gated against itself.** Task 0's script materializes every path-tagged block and runs Tasks 1–3's tests; a revision is filed only with that run green, and the filing note carries the output.

- **G29.** *(r9; ruled in r8 by coord-boss `cdfe666e`, re-implemented after both reviewers escaped the in-process harness)* **THE GUARANTEE IS BEHAVIOURAL, OS-ENFORCED, AND OBSERVED AT A PROCESS BOUNDARY.** `tests/proof/run_proof.py` starts a **store server outside the sandbox** (the corpus lives in that process, loaded from a file the sandbox cannot read; it answers only the five fixed request shapes, **refuses and logs everything else**), then runs the **production** `CliPointerReader`/`CliPointerWriter` through all six verbs **inside an OS sandbox** (macOS seatbelt) that denies at the kernel: every network endpoint except the store's socket; every exec except the interpreter; every file read outside the interpreter, the package tree, and its own temp dir; every file write outside that temp dir (and `/dev/null`, a sink, allowed by literal). **Asserted both ways, by name AND by semantics** *(r10, codex-coder round 9: a `get-records` whose cursor is rewritten to the epoch enumerates downstream of an allowed name)*: (1) the clean run — all six verbs exit 0, every request is one of `file stat`, `file download`, `get-records`, `record`, `file upload`, and **the store logs each `get-records` with its channel, its `since`, and the count it returned**: against a 5000-record corpus the first fold reads the corpus once, and the second fold asks from exactly the last observed record minus `OVERLAP_SECONDS` and gets back a bounded count (G31); (2) the attack battery, run inside — `reader._s` does not exist on the production class, the `gc`-recovered original `posix.listdir` is denied by the kernel, `io.open('/etc/hosts')`, `/bin/ls`, reading the corpus file, and an outbound socket are denied, and a direct socket to the store asking `file list` is **logged and refused**; (3) the mutated run — `fold.run` wrapped to issue one `file list` through its own CLI — is **flagged**; (4) **the epoch-rewrite mutation** — the production `_records` sending `since=1970-01-01T00:00:00Z` — is **flagged** by the logged semantics (wrong cursor, unbounded return) although every verb name is allowed. **What this proves and no more (coord-boss `f6ceb0c4`, the weaker true claim):** *these specific capabilities were denied by the kernel and the fold still completed* — NOT "the fold ran with no capabilities". The measured profile is allow-default with kernel denies on the capabilities that matter, because deny-default aborts the interpreter at startup on the proof host (measured, rc 134). *(r11, codex-coder round 10)* Every allowed operation is checked by **semantics**, not prefix: the driver asserts the **exact request sequence of the clean run, with paths** (measured from the run and frozen; a change to the fold changes it deliberately), a **path allowlist** for `file stat/download/upload` (the channel config, the checkpoint, the close evidence — nothing else), per-shape bounds, and — because **the store holds the agent's checkpoint** — that no `get-records` asks from before the cursor the store held at that moment minus the overlap (the first observation from an absent checkpoint is allowed by construction; the epoch rewrite is flagged because a checkpoint exists by then) — so a production reader that point-probes thousands of guessed paths (phase 5, his mutation) is flagged although every request name is allowed. **What it does not claim:** that enumeration is impossible to write, that the fold cannot detect it is under test, or that a Python process can hide anything from code running inside it. **The driver exits 3 (UNKNOWN) where no OS sandbox exists — never green by absence, never softened to a skip.** Ruling `f6ceb0c4`: **no bwrap profile is written until it can be measured** — an untested sandbox profile reads as proof and is not. macOS is the proof host; Linux CI stays UNKNOWN; **the gate is that the proof ran and passed on at least one host, recorded with which host and when** — the driver prints host, platform and UTC time in its final line and the filing note carries it. Measured on this host before filing: the unit suite passes *inside* the sandbox; the proof passed all five phases (numbers in Task 1 Step 5).
- **G32.** *(r9; ruled `f6ceb0c4`; r12 per codex-coder round 11)* **No automated gate claims anti-consolidation, and the plan says so: *the gates do not check this*.** Seven rounds proved the automated form cannot close the class. **But the requester's acceptance condition stands** — *"if all five structural checks can pass with one big file, the plan is not ready"* — and a scope ruling on the bus does not waive it; codex-coder is right that recording the hole honestly is not closing it. So it is closed the only way the ruling allows: **a review-shaped ship gate (Task 16), bound to what ships** *(r13, both reviewers round 12)* — a required reviewer *reads the on-disk tree of the exact implementation commit* against a written rubric and files a verdict on a ship register keyed by that head, quoting the head and git's tree hash for the package; **any head change invalidates it, nothing carries forward from plan time, and the ship check refuses without both approvals on that exact head.** coord-boss ruled no waiver (`5ccfce26`). **The automated checks do not prove responsibility distribution and are never described as doing so** *(codex-reviewer, rounds 13–21, repeatedly)*: a ≤400-line owner file can hold essentially all logic while the manifest-named modules are thin, and every automated check passes. Task 16 — the exact-head, exact-tree human review — is the anti-consolidation gate. The automated checks that survive (symbols defined where planned, tree = manifest, DAG) are cheap facts, not the criterion. The answer to the review question is therefore: *yes, a ≤400-line `cli.py` behind owner shims would pass every automated check — and it would fail Task 16, which is a person or agent reading it.*
- **G34.** *(r13, codex-reviewer)* **The §7 reconciler stays out of the fold path as an invariant, not a habit:** `fold`/`status` never invoke, await, or freshness-gate on the reconciler. Evidence, all already in the plan: the import graph never reaches `coord_engine` (Task 1 truths); inside the proof every exec but the interpreter is denied and the clean run's exact request sequence contains no reconciler call (G29); the CLI has no flag that reaches one (Task 7).
- **G33.** *(r11, named property per `f6ceb0c4`)* **Denial is indifferent to how code arrives.** Because the denial is the kernel's, it holds regardless of what the source says — written module, generated module, alias, annotation, `getattr` — a property of what the process *cannot do*, not of what a gate looks for. In r8's in-process harness this surfaced as "the import machinery itself enumerates" (an uncached module could not be imported under denial); under the OS sandbox that special case is not needed and not claimed: a module the fold writes to its temp dir *can* be imported and faces the same denies.
- **G31.** *(r8, codex-coder round 7)* **The cursor passes irrelevant records.** The cursor is the `recorded_at` of the last **observed** record before the first **unapplied relevant** event. Records that are unparseable, foreign-schema, or addressed to someone else are observed and passed, never re-read; otherwise an agent with no recent addressed events would re-read every other agent's traffic on every pass — corpus-shaped work at rc 0. This is consistent with G26: a *gap* is an unapplied relevant event, not an irrelevant one. Test: thousands of other-agent events after the last relevant one are not re-read on the next pass.

---

## File Structure

```
packages/coord-fold/
  pyproject.toml, README.md
  scripts/
    materialize_plan.py               Task 0: extract tagged blocks from the plan, run the gates   (G24)
  coord_fold/
    __init__.py                       __version__
    events.py                         KINDS, PRIORITIES, PAYLOAD_VERSION, build_payload, parse_event
    transport.py                      PointerTransport, ReadState, TransportUnavailable, CliPointerReader, CliPointerWriter
    channel.py                        CONFIG_PATH, ChannelUnresolved, config_path, resolve
    checkpoint.py                     SCHEMA_VERSION, path, empty, apply, load, save
    fold.py                           OVERLAP_SECONDS, FoldOutcome, FoldRefused, FoldContended, run
    cli.py                            main, build_parser, RC_*, cmd_* (six), helpers
  tests/
    fakes.py                          FakeStore / FakeReader / FakeWriter — for BEHAVIOUR tests only, in-process
    proof/
      store_server.py                 THE STORE, outside the sandbox: corpus from a file the sandbox cannot read; logs + refuses   (G29)
      fake_cli.py                     thin client the PRODUCTION reader/writer exec as their `cli`; argv -> socket -> store
      inside.py                       runs INSIDE the sandbox: six verbs | attack battery | mutated fold
      run_proof.py                    THE PROOF driver: profile, server, three phases; exit 0 proven / 1 failed / 3 UNKNOWN
    test_structural.py                boundary truths: G5–G7, ownership-defined-where-planned, DAG, tree
    test_tripwire.py                  demoted syntactic scan — fast feedback, NOT proof                   (G30)
    test_file_size_ceiling.py         G8
    test_no_degraded_vocabulary.py    G11
    test_events.py, test_channel.py, test_checkpoint.py
    test_cli_fold.py, test_cli_emit.py, test_cli_claim_release_close.py, test_cli_status.py

packages/coord-engine/…               Tasks 12–14 (old side): export-open, dual_emit, compare-to-fold, cutover-ready
.github/workflows/uv-workspace.yml    named step "coord-fold structural gates"
docs/coord/COORD-FOLD-CUTOVER.md      runbook
```

---

## Verb Disposition (spec §6)

42 top-level nouns at `5db5c3e5` (spec counts 38 at `3a4687b0`; the four extra — `annotate`, `stash`, `wake`, `acceptance` — reported, not reconciled). Kept: six (`tell→emit`, `respond→close`, `owed/obligations/needs-me/inbox→fold+status`, `roles claim/release→claim/release`, engine `status→status`). Killed: 36, each because it is a corpus walk under a deadline, sugar over `emit`, content-plane state, dispatch over an enumerated board, or old-bus plumbing. Any killed verb returns only by a directive naming the agent that needs it and the cursor it reads from.

---

### Task 0: The plan gate — materialize and run (G24)

**Files:** `packages/coord-fold/scripts/materialize_plan.py`

- [ ] **Step 1: Write the script**

```python
# packages/coord-fold/scripts/materialize_plan.py
"""Extract every path-tagged code block from the plan into a scratch tree and run the
structural gates against it. Round-4 verdicts: 'the proposed structural suite fails its own
planned source.' This makes that a check, not a review finding.

Usage: python scripts/materialize_plan.py <plan.md> <out-dir>
Exit 0 iff the gates pass on the materialized tree.
"""
from __future__ import annotations

import pathlib
import re
import subprocess
import sys

TICKS = "`" * 3                                  # never spelled literally: this file lives inside a fence
FENCE = re.compile(TICKS + r"(python|toml|yaml)\n(.*?)" + TICKS, re.S)
TAG = re.compile(r"#\s*((?:packages/coord-fold|\.github/workflows)/\S+)")   # workflow YAML materializes too, so the wiring test runs here
# codex-coder rounds 27-28: the prose contract drifted from argparse twice. An invocation of the ship gate written
# without its stated trust roots is a plan defect the plan gate itself refuses (a builder following it can never cut over).
TICKS = "`" * 3                                   # never spell the delimiter literally: this file itself lives in a Markdown fence
FENCE_DELIM = re.compile("^" + re.escape(TICKS) + r"[A-Za-z0-9_-]*\s*$")
# r39 (codex-coder round 34): match the COMMAND TOKEN first, then judge every positional. A mention is an invocation
# when it is preceded by a run-style word (python/python3/run/$/uv run) OR followed by at least one positional; a
# bare path reference in prose ("see scripts/ship_check.py") is not one. Missing positionals are REPORTED.
INVOCATION = re.compile(r"(?P<pre>(?:python3?|run|\$)\s+`?)?(?:[\w./-]*/)?ship_check\.py(?:[ \t]+(?P<team>[^\s`]+))?(?:[ \t]+(?P<head>[^\s`]+))?(?P<tail>[^\n`]*)")
HEAD_PLACEHOLDERS = ("<HEAD>", "<40-hex-head>")                                   # `<40-hex head>` (with the space) is normalized to this before matching
HEAD_OK = re.compile(r"^[0-9a-f]{40}$")                                          # exactly what ship_check.main fullmatches


def head_problem(head: str):
    return None if HEAD_OK.match(head) or head in HEAD_PLACEHOLDERS else f"head {head!r} is not 40 lowercase hex (or a documented placeholder)"
REQUIRED_ROOTS = ("--git", "--fulcra-api")


PLACEHOLDERS = ("<abs>", "<abs path>", "<path>")   # r37: an explicit ALLOWLIST substituted verbatim — a pattern let `<abs --bogus>` hide an unknown option (codex-coder round 32)


def parse_invocation(rest: str) -> list[str]:
    """Problems with the SAME-LINE COMMAND SHAPE after `ship_check.py <team> <head>`, validated against the documented
    argparse shape (codex-coder rounds 30-31): a shell comment ends the command; the rest is shlex-tokenized; the tail
    may contain ONLY `--git V` / `--git=V` and `--fulcra-api V` / `--fulcra-api=V`, each exactly once, each V an
    ABSOLUTE path (resolve_trust_roots refuses anything else) or a documentation placeholder like `<abs>`; any other
    token — an unknown option, a relative value, a trailing positional — is a problem, because argparse or the gate
    refuses the documented command at runtime while a presence check would have passed it."""
    import shlex
    command = rest.split("#", 1)[0]
    for ph in PLACEHOLDERS:                                                   # only the allowlisted placeholders collapse to one token; any other <...> is tokenized and judged
        command = command.replace(ph, "<placeholder>")
    try:
        toks = shlex.split(command)
    except ValueError as exc:
        return [f"unparseable shell syntax ({exc})"]
    problems, seen, i = [], {r: 0 for r in REQUIRED_ROOTS}, 0
    def check_value(root, v):
        if not v:
            problems.append(f"{root} has no value")
        elif not (v.startswith("/") or v == "<placeholder>"):
            problems.append(f"{root} value {v!r} is not an absolute path")
        seen[root] += 1
    while i < len(toks):
        t = toks[i]
        root = next((r for r in REQUIRED_ROOTS if t == r or t.startswith(r + "=")), None)
        if root is None:
            problems.append(f"unexpected token {t!r}"); i += 1; continue
        if t == root:
            nxt = toks[i + 1] if i + 1 < len(toks) else None
            if nxt is None or nxt.startswith("--"):
                problems.append(f"{root} has no value"); seen[root] += 1; i += 1; continue
            check_value(root, nxt); i += 2
        else:
            check_value(root, t[len(root) + 1:]); i += 1
    for root in REQUIRED_ROOTS:
        if seen[root] == 0:
            problems.append(f"missing {root}")
        elif seen[root] > 1:
            problems.append(f"{root} given {seen[root]} times")
    return problems


def bare_invocations(text: str) -> list[str]:
    out = []
    text = text.replace("<40-hex head>", "<40-hex-head>")                          # a documented head placeholder with a space: one token
    for m in INVOCATION.finditer(text):
        team, head, tail = m.group("team"), m.group("head"), m.group("tail") or ""
        if not m.group("pre") and team is None:
            continue                                                                # a path reference, not an invocation
        problems = []
        if team is None:
            problems.append("missing team")
        if head is None:
            problems.append("missing head")
        elif head_problem(head):
            problems.append(head_problem(head))
        if head is not None:
            problems += parse_invocation(tail)
        if problems:
            out.append(f"{'; '.join(problems)}: {m.group(0).strip()[:100]}")
    return out


def refuse_bare_runbook_invocations(plan_text: str) -> list[str]:
    """Scan INSTRUCTIONS only: fenced code is checked by the tests it materializes into, and the revision log is
    history (it quotes the forms that were wrong). Everything else in the plan is prose a builder follows."""
    out, in_fence, in_log = [], False, False
    for i, ln in enumerate(plan_text.splitlines(), 1):
        if FENCE_DELIM.match(ln):            # a delimiter is three backticks plus at most a language word; code that merely BEGINS with three backticks is not one
            in_fence = not in_fence
            continue
        if ln.startswith("## "):
            in_log = ln.startswith("## Revision log")
        if in_fence or in_log:
            continue
        for why in bare_invocations(ln):
            out.append(f"line {i}: {why}")
    return out


GATES = ["tests/test_structural.py", "tests/test_tripwire.py", "tests/test_ship_check.py", "tests/test_ci_wiring.py",
         "tests/test_file_size_ceiling.py", "tests/test_no_degraded_vocabulary.py"]
PROOF = "tests/proof/run_proof.py"   # G29; exits 3 = UNKNOWN where no OS sandbox exists — never read as green


def materialize(plan: str, out: pathlib.Path) -> tuple[list[str], list[str]]:
    written, untagged = [], []
    for lang, body in FENCE.findall(plan):
        first, _, rest = body.partition("\n")
        m = TAG.match(first)
        if not m:
            if lang != "yaml":                       # a YAML snippet (a CI step to add) is prose; whole workflows are tagged
                untagged.append(first[:70])
            continue
        target = out / m.group(1)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():                      # a second block for the same file APPENDS
            target.write_text(target.read_text().rstrip("\n") + "\n\n\n" + rest)
        else:
            target.write_text(rest)
        written.append(m.group(1))
    return written, untagged


def main(argv: list[str]) -> int:
    plan, out = pathlib.Path(argv[1]).read_text(), pathlib.Path(argv[2])
    bare = refuse_bare_runbook_invocations(plan)
    if bare:
        print("Task 0: the plan invokes ship_check WITHOUT its stated trust roots (--git/--fulcra-api) — a builder following it cannot cut over:")
        for b in bare:
            print("  " + b)
        return 1
    written, untagged = materialize(plan, out)
    pkg = out / "packages/coord-fold"
    (pkg / "README.md").write_text("# coord-fold\nevery module under **400 lines**\n")
    print(f"materialized {len(written)} blocks; untagged python/toml blocks: {len(untagged)}")
    for u in untagged:
        print("  untagged:", u)
    env = {"PYTHONPATH": ".:tests", "PATH": "/usr/bin:/bin", "HOME": str(out)}
    rc = subprocess.run([sys.executable, "-m", "pytest", *GATES, "-q"], cwd=pkg, env=env).returncode
    if rc:
        return rc
    proof = subprocess.run([sys.executable, PROOF], cwd=pkg, env=env).returncode
    print(f"proof rc={proof} (0 proven, 1 failed, 3 UNKNOWN: no OS sandbox on this host)")
    return proof


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
```

- [ ] **Step 2: Run it against this plan.** `python packages/coord-fold/scripts/materialize_plan.py docs/superpowers/plans/2026-09-04-coord-fold.md /tmp/coord-fold-gate` — expected: `untagged: 0` and the four gate files pass. **This is the filing precondition for every revision.** (The script lives under `scripts/`, outside `coord_fold/`, so the recursive artifact scan does not count it.)

---

### Task 1: SPLIT (coord-boss ruling `6bb7fa0f`, 2026-09-05T11:42Z) — **1a** package scaffold, fakes, boundary truths (G5–G7) BEGINS NOW; **1b** THE PROOF — the OS-sandboxed, process-boundary run (G29) — is a SHIP gate beside Task 16

**The ruling, verbatim in substance (r31):** a ship gate had been sitting on the START of the work. Task 1 bundled the product scaffold with THE PROOF, so the first task could not begin until the proof converged, and the proof is the artifact that never converged (28 rounds; of 14 heads both reviewers reached, both approved on none). **1a** — `pyproject.toml`, `coord_fold/__init__.py`, `coord_fold/transport.py`, `tests/coord_fold_fakes.py`, `tests/test_structural.py` (Steps 3, 4, 5a below) — begins immediately against the current plan text, no further review round required to start. **1b** — `tests/proof/{store_server,fake_cli,inside,run_proof}.py` (Steps 1, 2, 5b) — moves to the ship gate beside Task 16: it still must pass before anything ships, and Task 16 is untouched (NO WAIVER stands); it simply no longer blocks the first line of code. The plan register keeps running for 1b and Task 16 on its own cadence and no longer gates the build. **Process change (same ruling):** do not open a new head while a reviewer has an unanswered verdict on the current one — 13 of 27 heads moved before the second reviewer could file.

**Files (1a):** `pyproject.toml`, `coord_fold/__init__.py`, `coord_fold/transport.py`, `tests/coord_fold_fakes.py`, `tests/test_structural.py`; **and one line in the ROOT `pyproject.toml`** — add `"packages/coord-fold/tests",` to `[tool.pytest.ini_options].pythonpath` (workspace convention: helper modules are package-prefixed and each tests dir is listed there; without it the workspace-root test job cannot import the fakes — measured on CI, r32). **Files (1b, ship gate):** `tests/proof/{store_server,fake_cli,inside,run_proof}.py`.

**Why this shape (r9).** r8's in-process harness was escaped in one round, four ways: the originals it saved were reachable through the suspended generator's frame via `gc`; `io.open` (and the rest of the low-level I/O surface) was not on its list and no list is ever complete; the fake leaked its corpus through `reader._s.events`, so the fold could enumerate without any patched callable; and the fold could tell it was under test. All four share a cause — *code running inside a Python process can reach anything in that process*. So the proof moves the two things that matter out of the process: **the corpus** (into a store server the sandbox cannot read, which logs and refuses) and **the denial** (into the kernel).

- [ ] **Step 1 (1b, ship gate): The store, outside the sandbox**

```python
# packages/coord-fold/tests/proof/store_server.py
"""THE STORE (G29), outside the sandbox. Holds the corpus (loaded from a file the sandboxed fold
cannot read); answers ONLY the five fixed request shapes; LOGS EVERY REQUEST; refuses the rest.
Nothing in the fold's process holds the corpus, so there is nothing there to walk."""
import json
import os
import socket
import sys
import threading

sock_path, log_path, corpus_path = sys.argv[1], sys.argv[2], sys.argv[3]
store = json.load(open(corpus_path))          # {"docs": {path: text}, "events": [record, ...]}
lock = threading.Lock()


CKPT_SUFFIX = "/fold/checkpoint.json"


def held_cursor():
    """The cursor of the checkpoint THE STORE holds right now (None before the first save)."""
    for path, text in store["docs"].items():
        if path.endswith(CKPT_SUFFIX):
            try:
                return json.loads(text).get("cursor")
            except (ValueError, AttributeError):
                return None
    return None


def log(argv, returned=None, ckpt_cursor=None):
    with lock, open(log_path, "a") as f:
        f.write(json.dumps({"argv": argv, "returned": returned, "ckpt_cursor": ckpt_cursor}) + "\n")


def handle(req):
    argv, stdin = req["argv"], req.get("stdin", "")
    if argv[:1] == ["get-records"]:                       # SEMANTICS are logged, not just the verb (codex-coder round 9)
        since = argv[2]
        hits = [e for e in store["events"] if e["recorded_at"] >= since]
        log(argv, returned=len(hits), ckpt_cursor=held_cursor())
        return (0, "".join(json.dumps(e) + "\n" for e in hits), "")
    log(argv)
    if argv[:2] == ["file", "stat"]:
        p = argv[2]
        return (0, f"/{p} ({len(store['docs'][p])} bytes)\n", "") if p in store["docs"] else (1, "", f"Error: File not found in Fulcra: /{p}\n")
    if argv[:2] == ["file", "download"]:
        p = argv[2]
        return (0, store["docs"][p], "") if p in store["docs"] else (1, "", "Error: File not found\n")
    if argv[:1] == ["record"]:
        doc = json.loads(stdin)
        with lock:
            store["events"].append({"id": f"w{len(store['events'])}", "recorded_at": doc["recorded_at"], "note": doc["note"]})
        return (0, "recorded\n", "")
    if argv[:2] == ["file", "upload"]:
        with lock:
            store["docs"][argv[3]] = req.get("upload_body", "")
        return (0, "uploaded\n", "")
    return (2, "", f"REFUSED: {argv[:2]} is not a supported request\n")


def serve(conn):
    with conn:
        data = b""
        while not data.endswith(b"\n"):
            chunk = conn.recv(1 << 16)
            if not chunk:
                break
            data += chunk
        rc, out, err = handle(json.loads(data))
        conn.sendall((json.dumps({"rc": rc, "stdout": out, "stderr": err}) + "\n").encode())


if os.path.exists(sock_path):
    os.unlink(sock_path)
srv = socket.socket(socket.AF_UNIX)
srv.bind(sock_path)
srv.listen(16)
print("store server up", flush=True)
while True:
    c, _ = srv.accept()
    threading.Thread(target=serve, args=(c,), daemon=True).start()
```

```python
# packages/coord-fold/tests/proof/fake_cli.py
"""The thin client the PRODUCTION reader/writer exec as their `cli`: argv -> unix socket -> store."""
import json
import socket
import sys

sock_path, argv = sys.argv[1], sys.argv[2:]
req = {"argv": argv}
if argv[:1] == ["record"]:
    req["stdin"] = sys.stdin.read()
if argv[:2] == ["file", "upload"]:
    req["upload_body"] = open(argv[2]).read()      # the writer's own temp file, under its TMPDIR
c = socket.socket(socket.AF_UNIX)
c.connect(sock_path)
c.sendall((json.dumps(req) + "\n").encode())
data = b""
while not data.endswith(b"\n"):
    chunk = c.recv(1 << 20)
    if not chunk:
        break
    data += chunk
r = json.loads(data)
if argv[:2] == ["file", "download"]:
    # Behave like the REAL fulcra-api (measured 2026-09-05): LOCAL_FILE is validated as a readable path, so
    # /dev/stdout under a pipe is REFUSED, and a successful download writes the body to LOCAL_FILE — never stdout.
    # The old fake printed the body to stdout, which is how the production reader's /dev/stdout form passed the
    # proof and then refused every real fold at the channel config.
    if len(argv) < 4 or argv[3] == "/dev/stdout":
        sys.stderr.write("Error: Invalid value for '[LOCAL_FILE]': Path '/dev/stdout' is not readable.\n")
        sys.exit(2)
    if r["rc"] == 0:
        with open(argv[3], "w") as f:                 # the reader's own private temp file, under its TMPDIR
            f.write(r["stdout"])
        sys.exit(0)
sys.stdout.write(r["stdout"])
sys.stderr.write(r["stderr"])
sys.exit(r["rc"])
```

- [ ] **Step 2 (1b, ship gate): What runs inside, and the driver**

```python
# packages/coord-fold/tests/proof/inside.py
"""Runs INSIDE the OS sandbox. Modes: verbs (the clean run) | attack (the battery) | mutate (file list
through its own CLI) | epoch (get-records from the epoch) | probe (2000 guessed stats). Every mutation
must be FLAGGED by the request log. Prints one JSON line."""
import io
import json
import socket
import subprocess
import sys

sock, mode, corpus_path = sys.argv[1], sys.argv[2], sys.argv[3]
from coord_fold import fold
from coord_fold.cli import main
from coord_fold.transport import CliPointerReader, CliPointerWriter

HERE = __file__.rsplit("/", 1)[0]
cli = [sys.executable, HERE + "/fake_cli.py", sock]
r, w = CliPointerReader(cli=cli), CliPointerWriter(cli=cli)
VERBS = [("fold", ["fold", "r", "--agent", "me", "--now", "2026-09-04T11:00:00Z"]),
         ("status", ["status", "r", "--agent", "me"]),
         ("emit", ["emit", "r", "--from", "me", "--to", "boss", "--kind", "note", "--slug", "s0", "--pri", "P3", "--at", "2026-09-04T11:01:00Z"]),
         ("claim", ["claim", "r", "s1", "--agent", "me", "--at", "2026-09-04T11:02:00Z"]),
         ("release", ["release", "r", "s2", "--agent", "me", "--at", "2026-09-04T11:03:00Z"]),
         ("close", ["close", "r", "s0", "--agent", "me", "--evidence", "team/r/_coord/responses/s0/reply.md", "--at", "2026-09-04T11:04:00Z"]),
         ("fold2", ["fold", "r", "--agent", "me", "--now", "2026-09-04T12:00:00Z"])]
out = {"mode": mode}
if mode == "epoch":
    from coord_fold import transport
    real_records = transport.CliPointerReader._records
    def epoch_records(self, channel, since):
        return real_records(self, channel, "1970-01-01T00:00:00Z")     # codex-coder round 9: enumeration downstream of an allowed name
    transport.CliPointerReader._records = epoch_records
if mode == "probe":
    real = fold.run
    def probing(reader, writer, team, agent, **kw):
        for i in range(2000):                                     # codex-coder round 10: point-probe a namespace through an allowed name
            reader.read_classified(f"team/{team}/task/guess-{i}.md")
        return real(reader, writer, team, agent, **kw)
    fold.run = probing
if mode == "mutate":
    real = fold.run
    def enumerating(reader, writer, team, agent, **kw):
        subprocess.run([*cli, "file", "list", f"team/{team}/"], capture_output=True)   # the only road to a store is observed
        return real(reader, writer, team, agent, **kw)
    fold.run = enumerating
if mode in ("verbs", "mutate", "epoch", "probe"):
    out["rc"] = {label: main(argv, reader=r, writer=w) for label, argv in VERBS}
if mode == "attack":
    res = {}
    def t(label, fn):
        try:
            fn()
            res[label] = "NOT denied"
        except BaseException as e:
            res[label] = f"denied {type(e).__name__}"
    import posix
    t("codex-reviewer: reader._s.events", lambda: r._s.events)
    t("codex-reviewer: io.open('/etc/hosts')", lambda: io.open("/etc/hosts").read(4))
    t("codex-coder: original posix.listdir('/Users')", lambda: posix.listdir("/Users"))
    t("read the corpus file", lambda: open(corpus_path).read(4))
    t("subprocess /bin/ls", lambda: subprocess.run(["/bin/ls", "/"], capture_output=True))
    t("outbound socket 192.0.2.1:53 (RFC 5737 documentation address: unroutable; the seatbelt denies the connect regardless of destination)", lambda: socket.create_connection(("192.0.2.1", 53), timeout=2))
    def direct():
        c = socket.socket(socket.AF_UNIX); c.connect(sock); c.sendall(b'{"argv":["file","list","team/r/"]}\n')
        reply = json.loads(c.recv(1 << 16)); assert reply["rc"] == 2, reply; raise PermissionError("store refused: " + reply["stderr"].strip())
    t("direct socket asking the store for file list", direct)
    out["attack"] = res
    out["test_detectable"] = type(r).__module__     # stated, not hidden: a fold CAN see this is a CliPointerReader
print(json.dumps(out))
```

```python
# packages/coord-fold/tests/proof/run_proof.py
"""THE PROOF (G29). Exit 0 = proven for this run; 1 = failed; 3 = UNKNOWN — no OS sandbox on this
host. UNKNOWN is never green: CI must run this on a host that has one (macOS seatbelt today; a
Linux bwrap profile is an open infrastructure ask)."""
import calendar
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time

HERE = pathlib.Path(__file__).resolve().parent
PKG = HERE.parent.parent
PROFILE = """(version 1)
(allow default)
(deny network*)
(allow network-outbound (literal "{sock}"))
(deny process-exec (with no-log))
(allow process-exec (subpath "{pyprefix}") (subpath "{venv}"))
(deny file-read-data (subpath "/Users") (subpath "/home") (subpath "/private/tmp") (subpath "/tmp") (subpath "/private/etc") (subpath "/etc") (subpath "/private/var") (subpath "/var") (subpath "/Applications") (subpath "/Volumes") (subpath "/Library"))
(allow file-read-data (subpath "{pyprefix}") (subpath "{venv}") (subpath "{pkg}") (subpath "{tmp}"))
(deny file-write* (subpath "/Users") (subpath "/home") (subpath "/private/tmp") (subpath "/tmp") (subpath "/dev"))
(allow file-write* (subpath "{tmp}") (literal "/dev/tty") (literal "/dev/null"))
"""
ALLOWED = {("file", "stat"), ("file", "download"), ("get-records",), ("record",), ("file", "upload")}
CFG = "team/r/_coord/bus-v4/records.json"
CKPT = "team/r/member/me/fold/checkpoint.json"
EVIDENCE = "team/r/_coord/responses/s0/reply.md"
ALLOWED_PATHS = {CFG, CKPT, EVIDENCE}                 # the ONLY paths the clean run may stat/download/upload
MAX_PER_SHAPE = {("file", "stat"): 20, ("file", "download"): 20, ("file", "upload"): 4, ("record",): 4, ("get-records",): 2}
# The exact clean-run request sequence, MEASURED from the proof (upload temp paths and since values normalised).
# A change to the fold changes this list deliberately, in the same commit, with the new measurement.
EXPECTED_SEQUENCE = [
    [
    "file",
    "stat",
    "team/r/_coord/bus-v4/records.json"
    ],
    [
    "file",
    "download",
    "team/r/_coord/bus-v4/records.json",
    "<tmp>"
    ],
    [
    "file",
    "stat",
    "team/r/member/me/fold/checkpoint.json"
    ],
    [
    "get-records",
    "MomentAnnotation/x",
    "<since>"
    ],
    [
    "file",
    "stat",
    "team/r/member/me/fold/checkpoint.json"
    ],
    [
    "file",
    "upload",
    "<tmp>",
    "team/r/member/me/fold/checkpoint.json"
    ],
    [
    "file",
    "stat",
    "team/r/member/me/fold/checkpoint.json"
    ],
    [
    "file",
    "download",
    "team/r/member/me/fold/checkpoint.json",
    "<tmp>"
    ],
    [
    "file",
    "stat",
    "team/r/_coord/bus-v4/records.json"
    ],
    [
    "file",
    "download",
    "team/r/_coord/bus-v4/records.json",
    "<tmp>"
    ],
    [
    "record"
    ],
    [
    "file",
    "stat",
    "team/r/member/me/fold/checkpoint.json"
    ],
    [
    "file",
    "download",
    "team/r/member/me/fold/checkpoint.json",
    "<tmp>"
    ],
    [
    "file",
    "stat",
    "team/r/_coord/bus-v4/records.json"
    ],
    [
    "file",
    "download",
    "team/r/_coord/bus-v4/records.json",
    "<tmp>"
    ],
    [
    "record"
    ],
    [
    "file",
    "stat",
    "team/r/member/me/fold/checkpoint.json"
    ],
    [
    "file",
    "download",
    "team/r/member/me/fold/checkpoint.json",
    "<tmp>"
    ],
    [
    "file",
    "stat",
    "team/r/_coord/bus-v4/records.json"
    ],
    [
    "file",
    "download",
    "team/r/_coord/bus-v4/records.json",
    "<tmp>"
    ],
    [
    "record"
    ],
    [
    "file",
    "stat",
    "team/r/member/me/fold/checkpoint.json"
    ],
    [
    "file",
    "download",
    "team/r/member/me/fold/checkpoint.json",
    "<tmp>"
    ],
    [
    "file",
    "stat",
    "team/r/_coord/responses/s0/reply.md"
    ],
    [
    "file",
    "download",
    "team/r/_coord/responses/s0/reply.md",
    "<tmp>"
    ],
    [
    "file",
    "stat",
    "team/r/_coord/bus-v4/records.json"
    ],
    [
    "file",
    "download",
    "team/r/_coord/bus-v4/records.json",
    "<tmp>"
    ],
    [
    "record"
    ],
    [
    "file",
    "stat",
    "team/r/_coord/bus-v4/records.json"
    ],
    [
    "file",
    "download",
    "team/r/_coord/bus-v4/records.json",
    "<tmp>"
    ],
    [
    "file",
    "stat",
    "team/r/member/me/fold/checkpoint.json"
    ],
    [
    "file",
    "download",
    "team/r/member/me/fold/checkpoint.json",
    "<tmp>"
    ],
    [
    "get-records",
    "MomentAnnotation/x",
    "<since>"
    ],
    [
    "file",
    "stat",
    "team/r/member/me/fold/checkpoint.json"
    ],
    [
    "file",
    "download",
    "team/r/member/me/fold/checkpoint.json",
    "<tmp>"
    ],
    [
    "file",
    "upload",
    "<tmp>",
    "team/r/member/me/fold/checkpoint.json"
    ]
    ]


CORPUS_N, OVERLAP = 5000, 5                       # large corpus; OVERLAP must equal coord_fold.fold.OVERLAP_SECONDS
BASE_EPOCH = 1788512400                           # 2026-09-04T09:00:00Z (calendar.timegm)


def _iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def corpus():
    ev = []
    for i in range(CORPUS_N):                     # one record per second; the first five are addressed to me, the rest to others
        at = _iso(BASE_EPOCH + i)
        to = "me" if i < 5 else "them"
        ev.append({"id": str(i), "recorded_at": at, "note": json.dumps({"v": 1, "at": at, "from": "boss", "to": to, "kind": "open", "slug": f"s{i}", "pri": "P1", "ptr": f"team/r/task/s{i}.md"})})
    return {"docs": {CFG: json.dumps({"data_type": "MomentAnnotation/x", "api_version": "v1alpha1"}), "team/r/_coord/responses/s0/reply.md": "done"}, "events": ev}


LAST_CORPUS_AT = _iso(BASE_EPOCH + CORPUS_N - 1)
EXPECTED_SINCE_2 = _iso(BASE_EPOCH + CORPUS_N - 1 - OVERLAP)   # the second fold must ask from the last observed record minus the overlap
BOUNDED_RETURN = OVERLAP + 1 + 4                                # overlap window + the boundary record + the four records the verbs wrote


def shape(argv):
    return tuple(argv[:2]) if argv[:1] == ["file"] else tuple(argv[:1])


def norm(argv):
    if argv[:2] == ["file", "upload"]:
        return ["file", "upload", "<tmp>", argv[3]]
    if argv[:2] == ["file", "download"] and len(argv) == 4:
        # the reader's private temp file (never /dev/stdout: the real CLI refuses it under a pipe, measured 2026-09-05)
        return ["file", "download", argv[2], "<tmp>"]
    if argv[:1] == ["get-records"]:
        return ["get-records", argv[1], "<since>"]
    return argv


def path_of(argv):
    if argv[:2] in (["file", "stat"], ["file", "download"]):
        return argv[2]
    if argv[:2] == ["file", "upload"]:
        return argv[3]
    return None


def reads(reqs):
    """(channel, since, returned) for every get-records the store served, in order."""
    return [(r["argv"][1], r["argv"][2], r["returned"]) for r in reqs if r["argv"][:1] == ["get-records"]]


def main() -> int:
    if sys.platform != "darwin" or not shutil.which("sandbox-exec"):
        print("PROOF UNKNOWN (rc 3): no OS sandbox on this host — this is not a pass and must never be softened to a skip; the gate is a PASSED record from a host that has one")
        return 3
    private = pathlib.Path(os.path.realpath(tempfile.mkdtemp(prefix="coord-fold-proof-")))   # realpath: seatbelt matches RESOLVED paths (/tmp -> /private/tmp); under /private/tmp: DENIED to the fold
    tmp = private / "sandbox-tmp"; tmp.mkdir()                                  # the fold's only writable place (allowed after the deny)
    sock = f"/private/tmp/cf-{os.getpid()}.sock"                                 # AF_UNIX paths must stay short
    log, corpus_path, profile = private / "argv.jsonl", private / "corpus.json", private / "profile.sb"
    corpus_path.write_text(json.dumps(corpus()))
    pyprefix = os.path.realpath(sys.base_prefix)
    # A venv interpreter reads <venv>/pyvenv.cfg at startup (site.venv); on CI that file lives under the repo's .venv,
    # outside the package tree, and the kernel deny aborted the interpreter before phase 1 (measured on the macOS runner).
    venv = os.path.realpath(sys.prefix)
    profile.write_text(PROFILE.format(sock=sock, pyprefix=pyprefix, venv=venv, pkg=os.path.realpath(PKG), tmp=tmp))
    server = subprocess.Popen([sys.executable, str(HERE / "store_server.py"), sock, str(log), str(corpus_path)], stdout=subprocess.DEVNULL)
    for _ in range(50):
        if os.path.exists(sock):
            break
        time.sleep(0.1)
    env = {"PYTHONPATH": str(PKG), "PYTHONDONTWRITEBYTECODE": "1", "TMPDIR": str(tmp), "HOME": str(tmp), "PATH": "/usr/bin:/bin"}
    failures = []

    def run(mode):
        before = log.read_text().count("\n") if log.exists() else 0
        p = subprocess.run(["sandbox-exec", "-f", str(profile), sys.executable, str(HERE / "inside.py"), sock, mode, str(corpus_path)],
                           capture_output=True, text=True, env=env, cwd=PKG)
        lines = [l for l in p.stdout.splitlines() if l.startswith("{")]
        result = json.loads(lines[-1]) if lines else {"error": p.stderr[-800:]}
        result["stderr_tail"] = p.stderr[-1200:]
        requests = [json.loads(l) for l in log.read_text().splitlines()[before:]] if log.exists() else []
        return result, requests

    def enumerating(reqs):
        """A request enumerates if its verb is not allowed OR an allowed verb is used with enumerating semantics."""
        bad = [r["argv"] for r in reqs if shape(r["argv"]) not in ALLOWED]
        for r in reqs:
            if r["argv"][:1] != ["get-records"]:
                continue
            held = r.get("ckpt_cursor")
            floor = _iso(calendar.timegm(time.strptime(held, "%Y-%m-%dT%H:%M:%SZ")) - OVERLAP) if held else None
            if r["argv"][1] != "MomentAnnotation/x" or (floor is not None and r["argv"][2] < floor):
                bad.append(r["argv"] + [f"returned={r['returned']}", f"held_cursor={held}"])   # reading behind the checkpoint the store holds = re-reading the corpus
        bad += [r["argv"] for r in reqs if path_of(r["argv"]) not in (None, *ALLOWED_PATHS)]          # point-probing a namespace (codex-coder round 10)
        counts = {}
        for r in reqs:
            counts[shape(r["argv"])] = counts.get(shape(r["argv"]), 0) + 1
        bad += [[f"{k}: {v} requests > bound {MAX_PER_SHAPE.get(k, 0)}"] for k, v in counts.items() if v > MAX_PER_SHAPE.get(k, 0)]
        return bad

    try:
        res, reqs = run("verbs")
        bad_rc = {k: v for k, v in res.get("rc", {}).items() if v != 0}
        shapes = {shape(r["argv"]) for r in reqs}
        rd = reads(reqs)
        first_ok = len(rd) == 2 and rd[0][0] == "MomentAnnotation/x" and rd[0][2] == CORPUS_N                    # first observation reads the whole corpus, once
        second_ok = len(rd) == 2 and rd[1] == ("MomentAnnotation/x", EXPECTED_SINCE_2, rd[1][2]) and rd[1][2] <= BOUNDED_RETURN   # G31: cursor semantics, bounded return
        print(f"phase 1 clean run: rc={res.get('rc', res)} requests={len(reqs)} shapes={sorted(shapes)} get-records={rd} expected second since={EXPECTED_SINCE_2} bounded<={BOUNDED_RETURN}")
        observed = [norm(r["argv"]) for r in reqs]
        if observed != EXPECTED_SEQUENCE:
            print("phase 1: request sequence differs from the measured expectation:")
            for i, (a, b) in enumerate(zip(observed + [None] * len(EXPECTED_SEQUENCE), EXPECTED_SEQUENCE + [None] * len(observed))):
                if a != b:
                    print(f"  #{i}: observed={a} expected={b}")
        if bad_rc or not reqs or not shapes <= ALLOWED or not first_ok or not second_ok or observed != EXPECTED_SEQUENCE or enumerating(reqs):
            failures.append("phase 1")
            print("phase 1 enumerating:", enumerating(reqs)[:3], "| stderr tail:", res.get("stderr_tail", "")[-600:])
        res, reqs = run("attack")
        print("phase 2 attack battery:", json.dumps(res.get("attack", res), indent=1), "| the fold can see:", res.get("test_detectable"))
        if any(not v.startswith("denied") for v in res.get("attack", {}).values()) or not res.get("attack"):
            failures.append("phase 2")
        res, reqs = run("mutate")
        flagged = enumerating(reqs)
        print(f"phase 3 mutated fold (file list through its own CLI): FLAGGED = {flagged[:2]}")
        if not flagged:
            failures.append("phase 3 (the mutation was not detected — the proof cannot fail, so it proves nothing)")
        res, reqs = run("epoch")
        flagged = enumerating(reqs)
        print(f"phase 4 epoch-rewritten production reader (codex-coder round 9): get-records={reads(reqs)} FLAGGED = {bool(flagged)}")
        if not flagged:
            failures.append("phase 4 (an allowed verb with enumerating semantics was not detected)")
        res, reqs = run("probe")
        flagged = enumerating(reqs)
        print(f"phase 5 point-probing production reader (codex-coder round 10): requests={len(reqs)} FLAGGED = {bool(flagged)} e.g. {flagged[:1]}")
        if not flagged:
            failures.append("phase 5 (point-probing through an allowed name was not detected)")
    finally:
        server.terminate()
        shutil.rmtree(private, ignore_errors=True)
        if os.path.exists(sock):
            os.unlink(sock)
    if failures:
        print("PROOF FAILED:", failures)
        return 1
    import platform
    print(f"PROOF PASSED on host={platform.node()} platform={platform.platform()} at {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}: "
          "these specific capabilities were denied by the kernel and the fold still completed; the clean run's request sequence matched the measured expectation; all three mutated folds were flagged.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 3 (1a): Boundary truths — cheap and true, kept**

```python
# packages/coord-fold/tests/test_structural.py
"""Boundary truths (G5–G7 + ownership-defined-where-planned + DAG + tree). Cheap, true, and NOT the
guarantee — G29's harness is. These say what the package IS; the harness says what a fold DID."""
from __future__ import annotations

import ast
import pathlib
import tomllib

import coord_fold
from coord_fold import transport as tr

PKG_DIR = pathlib.Path(coord_fold.__file__).parent
ENUM_NAMES = ("list_dir", "glob", "listdir", "scandir", "walk", "rglob", "iterdir")
WRITE_NAMES = {"write_event", "save_doc", "_record", "_upload"}
READ_NAMES = {"read_classified", "read_events", "_stat", "_download", "_records"}
OWNERSHIP: dict[str, dict[str, str]] = {
    "events.py": {"PAYLOAD_VERSION": "value", "KINDS": "value", "PRIORITIES": "value", "build_payload": "callable", "parse_event": "callable"},
    "transport.py": {"ReadState": "value", "PointerTransport": "callable", "TransportUnavailable": "callable", "CliPointerReader": "callable", "CliPointerWriter": "callable"},
    "channel.py": {"CONFIG_PATH": "value", "ChannelUnresolved": "callable", "config_path": "callable", "resolve": "callable"},
    "checkpoint.py": {"SCHEMA_VERSION": "value", "path": "callable", "empty": "callable", "apply": "callable", "load": "callable", "save": "callable"},
    "fold.py": {"OVERLAP_SECONDS": "value", "FoldOutcome": "callable", "FoldRefused": "callable", "FoldContended": "callable", "run": "callable"},
    "cli.py": {"main": "callable", "build_parser": "callable"},
    "__init__.py": {"__version__": "value"},
}
ALLOWED_EDGES: dict[str, set[str]] = {
    "cli.py": {"fold", "channel", "events", "checkpoint", "transport"},
    "fold.py": {"channel", "checkpoint", "events", "transport"},
    "channel.py": {"transport"}, "checkpoint.py": {"transport"},
    "events.py": set(), "transport.py": set(), "__init__.py": set(),
}


def _modules():
    return sorted(p for p in PKG_DIR.rglob("*.py") if "__pycache__" not in p.parts)


def _tree(name):
    return ast.parse((PKG_DIR / name).read_text(), filename=name)


def _top_defs(tree):
    out = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out[node.name] = "callable"
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                names = [t] if isinstance(t, ast.Name) else (list(t.elts) if isinstance(t, ast.Tuple) else [])
                out.update({n.id: "value" for n in names if isinstance(n, ast.Name)})
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            out[node.target.id] = "value"
    return out


def _package_imports(tree):
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 1:
            out.update([node.module.split(".")[0]] if node.module else [a.name for a in node.names])
    return out


def test_no_enumeration_method_on_reader_writer_or_fakes():
    from coord_fold_fakes import FakeReader, FakeStore, FakeWriter
    st = FakeStore({}, [])
    for obj in (tr.CliPointerReader(cli=["true"]), tr.CliPointerWriter(cli=["true"]), FakeReader(st), FakeWriter(st)):
        for n in ENUM_NAMES:
            assert not hasattr(obj, n), f"{type(obj).__name__} has {n}"


def test_import_graph_never_reaches_coord_engine():
    for p in _modules():
        for node in ast.walk(ast.parse(p.read_text())):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else ([node.module] if isinstance(node, ast.ImportFrom) and node.module else [])
            for n in names:
                assert n.split(".")[0] != "coord_engine", f"{p.name} imports {n}"


def test_pyproject_does_not_depend_on_coord_engine_and_ships_only_the_package():
    data = tomllib.loads((pathlib.Path(__file__).resolve().parents[1] / "pyproject.toml").read_text())   # relative to THIS file: under --no-editable the imported package lives in site-packages
    assert not any(d.startswith("coord-engine") for d in data["project"].get("dependencies", []))
    wheel = data["tool"]["hatch"]["build"]["targets"]["wheel"]
    assert wheel.get("packages") == ["coord_fold"] and not ({"include", "artifacts", "force-include", "only-include"} & set(wheel))


def test_the_protocol_has_exactly_two_methods():
    assert {n for n in dir(tr.PointerTransport) if not n.startswith("_")} == {"read_classified", "read_events"}


def test_reader_and_writer_are_unrelated_classes_with_disjoint_surfaces():
    assert tr.CliPointerReader.__mro__ == (tr.CliPointerReader, object)
    assert tr.CliPointerWriter.__mro__ == (tr.CliPointerWriter, object)
    for n in WRITE_NAMES:
        assert not hasattr(tr.CliPointerReader, n), f"reader has {n}"
    for n in READ_NAMES:
        assert not hasattr(tr.CliPointerWriter, n), f"writer has {n}"
    assert {n for n in vars(tr.CliPointerReader) if not n.startswith("_")} == {"read_classified", "read_events"}
    assert {n for n in vars(tr.CliPointerWriter) if not n.startswith("_")} == {"write_event", "save_doc"}


def test_every_manifest_symbol_is_defined_in_its_module_with_the_right_kind():
    for mod, symbols in OWNERSHIP.items():
        defs = _top_defs(_tree(mod))
        for name, kind in symbols.items():
            assert name in defs, f"{mod} does not define {name!r}"
            if kind == "callable":
                assert defs[name] == "callable", f"{mod}: {name!r} is a bare assignment"


def test_package_tree_recursively_equals_the_manifest():
    found = sorted(p.relative_to(PKG_DIR).as_posix() for p in PKG_DIR.rglob("*") if p.is_file() and "__pycache__" not in p.parts)
    assert found == sorted(OWNERSHIP), {"unplanned": sorted(set(found) - set(OWNERSHIP)), "missing": sorted(set(OWNERSHIP) - set(found))}


def test_every_intra_package_import_is_an_allowed_edge_and_no_owner_imports_cli():
    for mod, allowed in ALLOWED_EDGES.items():
        bad = _package_imports(_tree(mod)) - allowed
        assert not bad, f"{mod} imports {sorted(bad)}"
        if mod != "cli.py":
            assert "cli" not in _package_imports(_tree(mod)), f"{mod} imports cli"
```

- [ ] **Step 4 (1a): Scaffold** (unchanged from r7; the transport keeps `pathlib` and never imports `os`)

```toml
# packages/coord-fold/pyproject.toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "coord-fold"
version = "0.1.0"
description = "Coord on annotations: a fold engine whose store access is observed and bounded."
requires-python = ">=3.11"
dependencies = ["fulcra-common>=0.3.0"]

[project.optional-dependencies]
dev = ["pytest>=7,<8"]

[project.scripts]
coord-fold = "coord_fold.cli:main"

[tool.hatch.build.targets.wheel]
packages = ["coord_fold"]

[tool.uv.sources]
fulcra-common = { workspace = true }

[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["tests"]
```

```python
# packages/coord-fold/coord_fold/__init__.py
"""coord-fold — a fold engine whose store access is observed and bounded (G29)."""
__version__ = "0.1.0"
```

```python
# packages/coord-fold/coord_fold/transport.py
"""The enforcing interface (spec §3.4) as a capability boundary (G5; the proof that it is not bypassed is G29).

Two unrelated classes; process launch exists only here, only as subprocess.run with a
literal argv. There is no generic argv receiver anywhere in the package.
"""
from __future__ import annotations

import json
import pathlib
import subprocess
import tempfile
from typing import Iterator, Literal, Protocol

ReadState = Literal["ok", "absent", "error"]
_FAR_FUTURE = "2999-01-01T00:00:00Z"


class PointerTransport(Protocol):
    def read_classified(self, path: str) -> tuple[str | None, ReadState]: ...
    def read_events(self, channel: str, since: str) -> Iterator[dict]: ...


class TransportUnavailable(RuntimeError):
    """The event read did not complete. The fold must NOT advance its cursor."""


class CliPointerReader:
    def __init__(self, cli: list[str], timeout: float = 60.0) -> None:
        self._cli = list(cli)
        self._timeout = timeout

    def _stat(self, path: str) -> tuple[int, str, str]:
        try:
            p = subprocess.run([*self._cli, "file", "stat", path], capture_output=True, text=True, timeout=self._timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return 127, "", str(exc)
        return p.returncode, p.stdout, p.stderr

    def _download(self, path: str) -> tuple[int, str, str]:
        # The real CLI validates LOCAL_FILE as a readable path and REFUSES /dev/stdout under a pipe (measured
        # 2026-09-05 on the first real run: every fold refused at the channel config). A private temp file, read
        # back and removed. pathlib + tempfile only: the transport never imports os (Task 1 boundary truth).
        d = pathlib.Path(tempfile.mkdtemp(prefix="coord-fold-read-"))
        d.chmod(0o700)
        local = d / "body"
        try:
            try:
                p = subprocess.run([*self._cli, "file", "download", path, str(local)], capture_output=True, text=True, timeout=self._timeout)
            except (OSError, subprocess.TimeoutExpired) as exc:
                return 127, "", str(exc)
            if p.returncode != 0:
                return p.returncode, "", p.stderr
            try:
                return 0, local.read_text(encoding="utf-8"), p.stderr
            except OSError as exc:
                return 1, "", str(exc)
        finally:
            try:
                local.unlink(missing_ok=True)
                d.rmdir()
            except OSError:
                pass

    def _records(self, channel: str, since: str) -> tuple[int, str, str]:
        try:
            p = subprocess.run([*self._cli, "get-records", channel, since, _FAR_FUTURE], capture_output=True, text=True, timeout=self._timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return 127, "", str(exc)
        return p.returncode, p.stdout, p.stderr

    def read_classified(self, path: str) -> tuple[str | None, ReadState]:
        rc, _out, err = self._stat(path)
        if rc != 0:
            return (None, "absent") if "File not found" in err else (None, "error")
        rc, out, _err = self._download(path)
        return (out, "ok") if rc == 0 else (None, "error")

    def read_events(self, channel: str, since: str) -> Iterator[dict]:
        rc, out, _err = self._records(channel, since)
        if rc != 0:
            raise TransportUnavailable(f"get-records rc={rc}")
        for line in out.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as exc:
                raise TransportUnavailable(f"malformed record line: {exc}") from exc


class CliPointerWriter:
    def __init__(self, cli: list[str], timeout: float = 60.0) -> None:
        self._cli = list(cli)
        self._timeout = timeout

    def _record(self, doc: str) -> tuple[int, str, str]:
        try:
            p = subprocess.run([*self._cli, "record"], input=doc, capture_output=True, text=True, timeout=self._timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return 127, "", str(exc)
        return p.returncode, p.stdout, p.stderr

    def _upload(self, local: str, remote: str) -> tuple[int, str, str]:
        try:
            p = subprocess.run([*self._cli, "file", "upload", local, remote], capture_output=True, text=True, timeout=self._timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return 127, "", str(exc)
        return p.returncode, p.stdout, p.stderr

    def write_event(self, channel_cfg: dict[str, str], payload: dict, *, sender: str) -> bool:
        # Key names: GOLDEN COMPARISON against coord_engine/transport.py record_write (~line 385 at
        # 5db5c3e5). Task 5 asserts them; if the old transport differs, change both there.
        doc = {"data_type": channel_cfg["data_type"], "api_version": channel_cfg["api_version"],
               "note": json.dumps(payload, separators=(",", ":")), "source": sender, "recorded_at": payload["at"]}
        rc, _o, _e = self._record(json.dumps(doc))
        return rc == 0

    def save_doc(self, path: str, text: str) -> bool:
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            f.write(text)
            tmp = f.name
        try:
            rc, _o, _e = self._upload(tmp, path)
            return rc == 0
        finally:
            pathlib.Path(tmp).unlink()
```

```python
# packages/coord-fold/tests/coord_fold_fakes.py
"""One store, two views. FakeReader has read_classified/read_events and nothing else;
FakeWriter has write_event/save_doc and nothing else."""
from __future__ import annotations

import json
from typing import Iterator


class FakeStore:
    def __init__(self, docs: dict[str, str], events: list[dict]) -> None:
        self.docs = dict(docs)
        self.events = list(events)
        self.written: list[dict] = []
        self.saved: dict[str, str] = {}
        self.fail_reads = False          # every read answers 'error'
        self.fail_paths: set[str] = set()   # ONLY these paths answer 'error' (Task 9 r32: a test that fails every read never reaches the evidence branch)
        self.fail_events = False


class FakeReader:
    def __init__(self, store: FakeStore) -> None:
        self._s = store

    def read_classified(self, path: str):
        if self._s.fail_reads or path in self._s.fail_paths:
            return None, "error"
        if path in self._s.saved:
            return self._s.saved[path], "ok"
        if path in self._s.docs:
            return self._s.docs[path], "ok"
        return None, "absent"

    def read_events(self, channel: str, since: str) -> Iterator[dict]:
        if self._s.fail_events:
            from coord_fold.transport import TransportUnavailable
            raise TransportUnavailable("fake outage")
        for rec in self._s.events:
            if rec.get("recorded_at", "") >= since:
                yield rec


class FakeWriter:
    def __init__(self, store: FakeStore) -> None:
        self._s = store

    def write_event(self, channel_cfg, payload, *, sender):
        self._s.written.append({"channel": channel_cfg["data_type"], "payload": dict(payload), "sender": sender})
        self._s.events.append({"id": f"w{len(self._s.written)}", "recorded_at": payload["at"], "note": json.dumps(payload)})
        return True

    def save_doc(self, path: str, text: str) -> bool:
        self._s.saved[path] = text
        return True
```

README (not gate-relevant beyond the ceiling sentence): six verbs, the guarantee in G29's words, what it does not claim, the tripwire's demotion in G30's words, and the sentence `every module under **400 lines**`.

- [ ] **Step 5: Run.** (5a = 1a: the boundary truths; 5b = 1b: the proof, at ship time.) Boundary truths: **5 pass now** *(r32, measured at 1a: the three whole-manifest tests — recursive tree equals manifest, allowed import edges, symbol ownership — fail-first until Tasks 4–7 create `events/channel/checkpoint/fold/cli`; the earlier "8 pass now" was never measured)*. The proof needs Tasks 4–10 (there is no fold yet): `python tests/proof/run_proof.py` → exit 1 until then, exit 3 on a host with no sandbox — **commit failing-first**. Measured on this host at plan time, against the materialized tree: the unit suite passes *inside* the sandbox; proof phase 1 — seven verb invocations rc 0, 36 requests, shapes exactly the five, the first `get-records` read the 5000-record corpus once and the second asked from the last observed record minus the overlap and got 10 back (bound 10); phase 2 — every attack denied or refused (the direct-socket `file list` logged and refused by the store); phase 3 — the mutated fold's `file list` flagged; phase 4 — the epoch-rewritten production reader flagged by semantics (5006 and 5008 returned against the bound) with every verb name allowed; phase 5 — a production reader point-probing 2000 guessed task paths flagged (paths outside the allowlist, `file stat` count over its bound, sequence mismatch). The clean run's exact request sequence (36 entries, paths included) is frozen in the driver from this measurement. **Mutations for the truths** (each restored): (a) give `CliPointerReader` a base class → FAILS; (b) `def _upload` on the reader → FAILS; (c) `from coord_engine import x` anywhere → FAILS; (d) an extra file under `coord_fold/` → FAILS. **Commit** — `coord-fold: scaffold, boundary truths, and the OS-sandboxed process-boundary proof (G5–G7, G29)`

---

### Task 2: File-size ceiling (G8)

```python
# packages/coord-fold/tests/test_file_size_ceiling.py
import pathlib
import coord_fold
CEILING = 400
PKG_DIR = pathlib.Path(coord_fold.__file__).parent


def test_every_module_is_under_the_ceiling_recursively():
    over = {}
    for p in PKG_DIR.rglob("*.py"):
        if "__pycache__" in p.parts:
            continue
        n = sum(1 for _ in p.open())
        if n > CEILING:
            over[p.name] = n
    assert not over, over


def test_the_ceiling_is_the_documented_number():
    assert f"{CEILING} lines" in (pathlib.Path(__file__).resolve().parents[1] / "README.md").read_text()   # relative to THIS file: under --no-editable the imported package lives in site-packages, beside no README
```

CI step in `.github/workflows/uv-workspace.yml` after the pytest step:

```yaml
      - name: coord-fold structural gates
        run: |
          uv run --package coord-fold --extra dev python -m pytest \
            packages/coord-fold/tests/test_structural.py \
            packages/coord-fold/tests/test_tripwire.py \
            packages/coord-fold/tests/test_file_size_ceiling.py \
            packages/coord-fold/tests/test_no_degraded_vocabulary.py \
            packages/coord-fold/tests/test_ship_check.py \
            packages/coord-fold/tests/test_ci_wiring.py -q
```

**The proof runs on a real macOS job** *(r13, codex-reviewer: an `if: runner.os == 'macOS'` step inside a workflow whose only job is `ubuntu-latest` always skips)* — its own always-on workflow, no `paths-ignore`, modelled on `uv-workspace.yml`:

```yaml
# .github/workflows/coord-fold-proof.yml
name: coord-fold proof (G29)
on:
  push:
    branches: [main]
  pull_request:
    branches: [main]
  workflow_dispatch:
jobs:
  proof:
    runs-on: macos-latest
    timeout-minutes: 15
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v3
        with:
          version: "0.11.x"
          enable-cache: true
      - run: uv python install
      - run: uv sync --all-packages
      - name: the proof host has a sandbox (else this job is the wrong host, not a pass)
        run: test -x /usr/bin/sandbox-exec
      - name: coord-fold proof — exit 3 is UNKNOWN and fails this job
        run: uv run --package coord-fold --extra dev python packages/coord-fold/tests/proof/run_proof.py
```

**Wiring test** — fails if the proof stops being referenced by a macOS job, so coverage cannot silently disappear:

```python
# packages/coord-fold/tests/test_ci_wiring.py
"""The proof must be run by a job whose ACTUAL `runs-on` is a macOS runner, with no `if:` that can
skip the step. *(r15, codex-reviewer round 14: `"macos" in job` matched a comment; and this test was
run by nothing.)* It runs from the always-on uv-workspace job and from Task 0 (which materializes the
workflow), so deleting the proof workflow turns a required gate red. Honest status: a green run of
the proof workflow in CI has NOT yet been observed; the G29 gate is the PASSED record from a host."""
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[3]
WORKFLOWS = ROOT / ".github" / "workflows"


def _jobs(text):
    """Split a workflow into its jobs (2-space-indented keys under `jobs:`), comments stripped."""
    body = re.sub(r"(?m)^\s*#.*$", "", text)
    m = re.search(r"(?m)^jobs:\s*$", body)
    if not m:
        return []
    jobs_text = body[m.end():]
    return [j for j in re.split(r"(?m)^  (?=[A-Za-z_][A-Za-z0-9_-]*:\s*$)", jobs_text) if j.strip()]


def test_a_macos_job_runs_the_proof_unconditionally():
    found = []
    for wf in sorted(WORKFLOWS.glob("*.yml")):
        for job in _jobs(wf.read_text()):
            if "tests/proof/run_proof.py" not in job:
                continue
            runs_on = re.search(r"(?m)^\s+runs-on:\s*(\S+)", job)
            step_if = re.search(r"(?m)^\s+if:", job)
            found.append((wf.name, runs_on.group(1) if runs_on else None, bool(step_if)))
    ok = [f for f in found if f[1] and f[1].startswith("macos") and not f[2]]
    assert ok, f"no job with runs-on: macos-* runs the proof unconditionally; found {found}"
```

Mutation: append 401 comment lines to `__init__.py` → FAILS. **Commit.**

---

### Task 3: The syntactic tripwire — demoted (G30)

**Files:** `tests/test_tripwire.py`

```python
# packages/coord-fold/tests/test_tripwire.py
"""FAST FEEDBACK ONLY. THIS IS NOT THE GUARANTEE (G30).

It scans identifiers so a reviewer sees a plain `os.listdir` or `subprocess.Popen` in seconds. It
approximates behaviour and can be walked past by any spelling the AST does not resolve — seven
rounds (r2–r7) proved that. G29's harness is the guarantee; cite that, never this.
"""
import ast
import pathlib

import coord_fold

PKG_DIR = pathlib.Path(coord_fold.__file__).parent
SUSPECT_IDENTIFIERS = {"listdir", "scandir", "walk", "fwalk", "glob", "iglob", "rglob", "iterdir", "list_dir",
                       "system", "popen", "fork", "Popen", "check_output", "check_call", "posix_spawn"}
SUSPECT_MODULES = {"os", "glob", "ctypes", "pty", "multiprocessing", "asyncio", "shutil"}
LAUNCH_ALLOWED_IN = {"transport.py"}


def test_tripwire_identifiers_and_modules():
    hits = []
    for p in PKG_DIR.rglob("*.py"):
        if "__pycache__" in p.parts:
            continue
        for node in ast.walk(ast.parse(p.read_text())):
            if isinstance(node, ast.Attribute) and node.attr in SUSPECT_IDENTIFIERS:
                hits.append(f"{p.name}: .{node.attr}")
            if isinstance(node, ast.Name) and node.id in SUSPECT_IDENTIFIERS:
                hits.append(f"{p.name}: {node.id}")
            if isinstance(node, ast.Import) and any(a.name.split(".")[0] in SUSPECT_MODULES for a in node.names):
                hits.append(f"{p.name}: import {[a.name for a in node.names]}")
            if isinstance(node, ast.ImportFrom) and node.module and node.module.split(".")[0] in SUSPECT_MODULES:
                hits.append(f"{p.name}: from {node.module}")
            if isinstance(node, (ast.Import, ast.ImportFrom)) and p.name not in LAUNCH_ALLOWED_IN:
                mods = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
                if any(m.split(".")[0] == "subprocess" for m in mods):
                    hits.append(f"{p.name}: subprocess outside transport")
    assert not hits, hits
```

- [ ] **Run — 1 passed. Mutations:** a plain `os.listdir(".")` anywhere → FAILS (that is what it is for). `scan = getattr(pathlib.Path("."), "iter" + "dir"); list(scan())` in `fold.run` → the tripwire **PASSES** (no suspect identifier exists in the source) — and inside the proof's sandbox the call raises `PermissionError` on any directory outside the package tree, while enumerating the package's own source directory is not enumerating a store (the store is in another process and logs what it is asked). Record both results in the commit message: that pair is why G30 is not the guarantee. (The annotated-assignment spelling `scan: object = pathlib.Path(".").iterdir` trips the identifier scan too; the tripwire is useful exactly that far.) **Commit** — `coord-fold: syntactic tripwire, demoted (G30)`

---

### Task 4: Event schema (G1–G3)

```python
# packages/coord-fold/tests/test_events.py
import json
import pytest
from coord_fold import events


def _rec(note, rid="r1", at="2026-09-04T13:45:00Z"):
    return {"id": rid, "recorded_at": at, "note": json.dumps(note)}


def test_build_produces_exactly_the_eight_fields():
    p = events.build_payload(at="t", sender="boss", to="me", kind="open", slug="s", pri="P1", ptr="x.md")
    assert set(p) == {"v", "at", "from", "to", "kind", "slug", "pri", "ptr"} and p["v"] == 1


def test_open_and_close_without_ptr_are_refused():
    for k in ("open", "close"):
        with pytest.raises(ValueError):
            events.build_payload(at="t", sender="a", to="b", kind=k, slug="s", pri="P1", ptr=None)


def test_ptr_must_be_a_single_file_path():
    for bad in ("team/r/task/", "team/r/task/*.md", ""):
        with pytest.raises(ValueError):
            events.build_payload(at="t", sender="a", to="b", kind="open", slug="s", pri="P1", ptr=bad)


def test_unknown_kind_and_priority_are_refused():
    with pytest.raises(ValueError):
        events.build_payload(at="t", sender="a", to="b", kind="directive", slug="s", pri="P1", ptr="x.md")
    with pytest.raises(ValueError):
        events.build_payload(at="t", sender="a", to="b", kind="note", slug="s", pri="P9", ptr=None)


def test_parse_accepts_v1_and_carries_record_id_and_recorded_at():
    p = events.build_payload(at="t", sender="a", to="b", kind="open", slug="s", pri="P1", ptr="x.md")
    ev = events.parse_event(_rec(p, rid="abc", at="T2"))
    assert ev and ev["record_id"] == "abc" and ev["recorded_at"] == "T2"


def test_parse_skips_free_text_and_foreign_payloads_silently():
    assert events.parse_event({"id": "x", "note": "hello"}) is None
    assert events.parse_event(_rec({"kind": "directive", "v": 1})) is None
    assert events.parse_event(_rec({"v": 2, "kind": "open", "slug": "s"})) is None
```

```python
# packages/coord-fold/coord_fold/events.py
"""Signal-plane payload v1 (spec §3.1)."""
from __future__ import annotations

import json
from typing import Any

PAYLOAD_VERSION = 1
KINDS = ("open", "close", "claim", "release", "note")
PRIORITIES = ("P0", "P1", "P2", "P3")
_PTR_REQUIRED = ("open", "close")


def build_payload(*, at: str, sender: str, to: str, kind: str, slug: str, pri: str, ptr: str | None) -> dict[str, Any]:
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}, got {kind!r}")
    if pri not in PRIORITIES:
        raise ValueError(f"pri must be one of {PRIORITIES}, got {pri!r}")
    if not slug:
        raise ValueError("slug is required")
    if ptr is None and kind in _PTR_REQUIRED:
        raise ValueError(f"ptr is required on {kind}")
    if ptr is not None and (not ptr or ptr.endswith("/") or "*" in ptr or "?" in ptr):
        raise ValueError("ptr must be one file path — never a directory or a glob")
    return {"v": PAYLOAD_VERSION, "at": at, "from": sender, "to": to, "kind": kind, "slug": slug, "pri": pri, "ptr": ptr}


def parse_event(record: dict[str, Any]) -> dict[str, Any] | None:
    note = record.get("note")
    if not isinstance(note, str):
        return None
    try:
        p = json.loads(note)
    except json.JSONDecodeError:
        return None
    if not isinstance(p, dict) or p.get("v") != PAYLOAD_VERSION or p.get("kind") not in KINDS or not p.get("slug"):
        return None
    out = dict(p)
    out["record_id"] = record.get("id")
    out["recorded_at"] = record.get("recorded_at")
    return out
```

Run — 6 passed. Mutation: `if ptr is None and kind in _PTR_REQUIRED` → `if False` → FAILS. **Commit.**

---

### Task 5: Channel resolution + golden-compared write keys (G15)

```python
# packages/coord-fold/tests/test_channel.py
import json
import subprocess
import pytest
from coord_fold import channel
from coord_fold.transport import CliPointerWriter
from coord_fold_fakes import FakeReader, FakeStore

CFG = "team/r/_coord/bus-v4/records.json"


def test_resolves_data_type_from_the_config_document():
    st = FakeStore({CFG: json.dumps({"data_type": "MomentAnnotation/abc", "api_version": "v1alpha1"})}, [])
    assert channel.resolve(FakeReader(st), "r")["data_type"] == "MomentAnnotation/abc"


def test_absent_and_unreadable_config_both_raise_with_different_words():
    with pytest.raises(channel.ChannelUnresolved, match="absent"):
        channel.resolve(FakeReader(FakeStore({}, [])), "r")
    st = FakeStore({}, []); st.fail_reads = True
    with pytest.raises(channel.ChannelUnresolved, match="error"):
        channel.resolve(FakeReader(st), "r")


def test_config_missing_data_type_raises():
    with pytest.raises(channel.ChannelUnresolved):
        channel.resolve(FakeReader(FakeStore({CFG: json.dumps({"api_version": "v1alpha1"})}, [])), "r")


def test_write_event_stdin_document_matches_the_old_transport_keys(monkeypatch):
    """GOLDEN: the key set is copied from coord_engine/transport.py record_write (~line 385 at 5db5c3e5).
    If the old transport's keys differ, change BOTH the writer and this set. Never guess."""
    seen = {}
    class R:
        returncode, stdout, stderr = 0, "", ""
    def fake_run(argv, input=None, **kw):
        seen["doc"] = json.loads(input)
        return R()
    monkeypatch.setattr(subprocess, "run", fake_run)
    CliPointerWriter(cli=["true"]).write_event({"data_type": "D", "api_version": "v1alpha1"},
        {"v": 1, "at": "T", "from": "a", "to": "b", "kind": "note", "slug": "s", "pri": "P3", "ptr": None}, sender="a")
    assert set(seen["doc"]) == {"data_type", "api_version", "note", "source", "recorded_at"}
```

```python
# packages/coord-fold/coord_fold/channel.py
"""Resolve the new bus's channel from its config document (G15). No default channel."""
from __future__ import annotations

import json

from .transport import PointerTransport

CONFIG_PATH = "team/{team}/_coord/bus-v4/records.json"
_REQUIRED = ("data_type", "api_version")


class ChannelUnresolved(RuntimeError):
    pass


def config_path(team: str) -> str:
    return CONFIG_PATH.format(team=team)


def resolve(reader: PointerTransport, team: str) -> dict[str, str]:
    body, state = reader.read_classified(config_path(team))
    if state != "ok" or body is None:
        raise ChannelUnresolved(f"bus-v4 config for team {team}: {state}")
    try:
        cfg = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ChannelUnresolved(f"bus-v4 config unparsable: {exc}") from exc
    missing = [k for k in _REQUIRED if not cfg.get(k)]
    if missing:
        raise ChannelUnresolved(f"bus-v4 config missing {missing}")
    return {k: str(cfg[k]) for k in _REQUIRED}
```

Run — 4 passed. Mutation: replace the first `raise` with a hardcoded return → 2 FAIL. **Commit.**

---

### Task 6: Checkpoint (G4)

```python
# packages/coord-fold/tests/test_checkpoint.py
import json
from coord_fold import checkpoint as cp
from coord_fold_fakes import FakeReader, FakeStore, FakeWriter
NOW = "2026-09-04T13:45:00Z"


def _ev(kind, slug="s1", rid="r1", **kw):
    b = {"v": 1, "at": NOW, "from": "boss", "to": "me", "kind": kind, "slug": slug, "pri": "P1", "ptr": f"team/r/task/{slug}.md", "record_id": rid}
    b.update(kw)
    return b


def test_empty_has_exactly_the_eight_fields():
    assert set(cp.empty(NOW)) == {"v", "cursor", "open", "unread_events", "unreadable_pointers", "seen", "generation", "writer"}
    assert cp.empty(NOW)["generation"] == 0


def test_open_adds_close_and_release_remove_claim_annotates():
    st = cp.empty(NOW); cp.apply(st, _ev("open"))
    assert st["open"]["s1"] == {"pri": "P1", "from": "boss", "ptr": "team/r/task/s1.md", "at": NOW}
    cp.apply(st, _ev("claim", rid="r2", **{"from": "me"})); assert st["open"]["s1"]["claimed_by"] == "me"
    cp.apply(st, _ev("release", rid="r3")); assert "s1" not in st["open"]
    cp.apply(st, _ev("open", rid="r4")); cp.apply(st, _ev("close", rid="r5")); assert st["open"] == {}


def test_close_of_unknown_slug_is_a_noop():
    st = cp.empty(NOW); cp.apply(st, _ev("close")); assert st["open"] == {}


def test_a_record_id_seen_before_is_not_applied_twice():
    st = cp.empty(NOW); cp.apply(st, _ev("open", rid="same")); cp.apply(st, _ev("close", rid="same"))
    assert "s1" in st["open"]


def test_load_states_and_save_roundtrip():
    store = FakeStore({}, []); r, w = FakeReader(store), FakeWriter(store)
    assert cp.load(r, "r", "me")[1] == "fresh"
    store.docs[cp.path("r", "me")] = "not json"; assert cp.load(r, "r", "me")[1] == "corrupt"
    bad = FakeStore({}, []); bad.fail_reads = True; assert cp.load(FakeReader(bad), "r", "me")[1] == "error"
    s = cp.empty(NOW); cp.apply(s, _ev("open")); s["cursor"] = NOW
    assert cp.save(w, "r", "me", s)
    back, src = cp.load(r, "r", "me"); assert src == "ok" and back["open"] == s["open"]
```

```python
# packages/coord-fold/coord_fold/checkpoint.py
"""One durable checkpoint per agent (spec §3.3). Eight fields (G4): generation/writer carry lost-update detection (G27)."""
from __future__ import annotations

import json
from typing import Any, Literal

from .transport import PointerTransport

SCHEMA_VERSION = 1
_SEEN_CAP = 500
_PATH = "team/{team}/member/{agent}/fold/checkpoint.json"
LoadState = Literal["ok", "fresh", "corrupt", "error"]


def path(team: str, agent: str) -> str:
    return _PATH.format(team=team, agent=agent)


def empty(now: str) -> dict[str, Any]:
    return {"v": SCHEMA_VERSION, "cursor": now, "open": {}, "unread_events": 0, "unreadable_pointers": [], "seen": [],
            "generation": 0, "writer": ""}


def apply(state: dict[str, Any], ev: dict[str, Any]) -> None:
    rid = ev.get("record_id")
    if rid and rid in state["seen"]:
        return
    slug, kind, rows = ev["slug"], ev["kind"], state["open"]
    if kind == "open":
        rows[slug] = {"pri": ev["pri"], "from": ev["from"], "ptr": ev["ptr"], "at": ev["at"]}
    elif kind in ("close", "release"):
        rows.pop(slug, None)
    elif kind == "claim" and slug in rows:
        rows[slug]["claimed_by"] = ev["from"]
    if rid:
        state["seen"].append(rid)
        del state["seen"][:-_SEEN_CAP]


def load(reader: PointerTransport, team: str, agent: str) -> tuple[dict[str, Any], LoadState]:
    body, st = reader.read_classified(path(team, agent))
    if st == "error":
        return {}, "error"
    if st == "absent":
        return {}, "fresh"
    try:
        state = json.loads(body or "")
    except json.JSONDecodeError:
        return {}, "corrupt"
    if not isinstance(state, dict) or state.get("v") != SCHEMA_VERSION:
        return {}, "corrupt"
    return state, "ok"


def save(writer: Any, team: str, agent: str, state: dict[str, Any]) -> bool:
    return bool(writer.save_doc(path(team, agent), json.dumps(state, indent=1)))
```

Run — 5 passed. Mutation: delete the `seen` guard → FAILS. **Commit.**

---

### Task 7: `fold` and the CLI skeleton (§3.3, G9, G25–G27, G31)

```python
# packages/coord-fold/tests/test_cli_fold.py
import json
from coord_fold import checkpoint as cp
from coord_fold.cli import main
from coord_fold_fakes import FakeReader, FakeStore, FakeWriter

CFG = "team/r/_coord/bus-v4/records.json"
CFG_DOC = json.dumps({"data_type": "MomentAnnotation/x", "api_version": "v1alpha1"})


def _rec(kind, slug, at, rid, to="me", sender="boss", ptr=None):
    p = {"v": 1, "at": at, "from": sender, "to": to, "kind": kind, "slug": slug, "pri": "P1", "ptr": ptr or f"team/r/task/{slug}.md"}
    return {"id": rid, "recorded_at": at, "note": json.dumps(p)}


def _team(events):
    return FakeStore({CFG: CFG_DOC}, events)


def _run(st, *extra, now="2026-09-04T11:00:00Z"):
    return main(["fold", "r", "--agent", "me", "--now", now, *extra], reader=FakeReader(st), writer=FakeWriter(st))


def _ckpt(st):
    return json.loads(st.saved[cp.path("r", "me")])


def test_fold_from_fresh_applies_open_events_and_stores_the_checkpoint():
    st = _team([_rec("open", "a", "2026-09-04T10:00:00Z", "1"), _rec("open", "b", "2026-09-04T10:01:00Z", "2")])
    assert _run(st) == 0 and set(_ckpt(st)["open"]) == {"a", "b"}


def test_close_after_open_removes_the_row():
    st = _team([_rec("open", "a", "2026-09-04T10:00:00Z", "1"), _rec("close", "a", "2026-09-04T10:05:00Z", "2")])
    _run(st); assert _ckpt(st)["open"] == {}


def test_events_for_someone_else_do_not_land_but_broadcast_does():
    st = _team([_rec("open", "a", "2026-09-04T10:00:00Z", "1", to="them"), _rec("open", "b", "2026-09-04T10:00:00Z", "2", to="all")])
    _run(st); assert set(_ckpt(st)["open"]) == {"b"}


def test_cursor_is_the_last_applied_event_never_now():
    """G26 / Ruling 1."""
    st = _team([_rec("open", "a", "2026-09-04T10:00:00Z", "1")]); _run(st)
    assert _ckpt(st)["cursor"] == "2026-09-04T10:00:00Z"
    st.events.append(_rec("close", "a", "2026-09-04T11:30:00Z", "2"))
    _run(st, now="2026-09-04T12:00:00Z"); assert _ckpt(st)["open"] == {} and _ckpt(st)["cursor"] == "2026-09-04T11:30:00Z"
    _run(st, now="2026-09-04T13:00:00Z"); assert _ckpt(st)["cursor"] == "2026-09-04T11:30:00Z"


def test_rerunning_from_the_stored_cursor_yields_the_same_open_set():
    """G26's checkable consequence."""
    st = _team([_rec("open", f"s{i}", f"2026-09-04T10:{i:02d}:00Z", str(i)) for i in range(6)] + [_rec("close", "s2", "2026-09-04T10:07:00Z", "x")])
    _run(st); first = _ckpt(st)["open"]
    _run(st, now="2026-09-04T12:00:00Z"); assert _ckpt(st)["open"] == first == {f"s{i}": first[f"s{i}"] for i in (0, 1, 3, 4, 5)}


def test_other_agents_traffic_after_my_last_event_is_not_reread():
    """G31 / codex-coder round 7: without this, an agent with no recent addressed events rereads everyone else's traffic forever."""
    st = _team([_rec("open", "a", "2026-09-04T10:00:00Z", "1")]); _run(st)
    st.events.extend(_rec("open", f"o{i}", f"2026-09-04T10:{i // 60 + 1:02d}:{i % 60:02d}Z", f"o{i}", to="them") for i in range(3000))
    _run(st, now="2026-09-04T12:00:00Z"); assert _ckpt(st)["cursor"] == "2026-09-04T10:50:59Z" and set(_ckpt(st)["open"]) == {"a"}
    r = FakeReader(st); yielded = []; orig = r.read_events
    r.read_events = lambda ch, since: (yielded.append(x) or x for x in orig(ch, since))
    assert main(["fold", "r", "--agent", "me", "--now", "2026-09-04T13:00:00Z"], reader=r, writer=FakeWriter(st)) == 0
    assert len(yielded) < 10 and set(_ckpt(st)["open"]) == {"a"}


def test_a_failed_event_read_does_not_advance_the_cursor_and_exits_3(capsys):
    st = _team([]); st.fail_events = True
    assert _run(st) == 3 and cp.path("r", "me") not in st.saved and "degraded" not in capsys.readouterr().out.lower()


def test_a_capped_pass_is_a_remainder_not_an_error(capsys):
    """G25 / Ruling 4: exit 0, cursor at the last applied event, unread_events is the bounded remainder."""
    st = _team([_rec("open", f"s{i}", f"2026-09-04T10:{i:02d}:00Z", str(i)) for i in range(7)])
    assert _run(st, "--max-events", "5") == 0
    c = _ckpt(st); assert c["unread_events"] == 2 and len(c["open"]) == 5 and c["cursor"] == "2026-09-04T10:04:00Z"
    out = capsys.readouterr(); assert "2 events remain" in out.out and "degraded" not in (out.out + out.err).lower()
    assert _run(st, "--max-events", "5", now="2026-09-04T12:00:00Z") == 0 and len(_ckpt(st)["open"]) == 7 and _ckpt(st)["unread_events"] == 0


def test_zero_progress_with_events_present_is_the_only_error(capsys):
    st = _team([_rec("open", "a", "2026-09-04T10:00:00Z", "1")])
    assert _run(st, "--max-events", "0") == 2 and "no progress" in capsys.readouterr().err and cp.path("r", "me") not in st.saved


def test_a_concurrent_writer_is_refused_by_name_and_nothing_is_overwritten(capsys):
    """G27 / Ruling 2: the generation moves between load and the re-read before write."""
    st = _team([_rec("open", "a", "2026-09-04T10:00:00Z", "1")]); _run(st)
    assert _ckpt(st)["generation"] == 1 and _ckpt(st)["writer"].startswith("me:")
    r = FakeReader(st); calls = []; orig = r.read_classified
    def bumping(path):
        body, state = orig(path)
        if path == cp.path("r", "me"):
            calls.append(path)
            if len(calls) == 2:
                other = json.loads(body); other["generation"] += 1; other["writer"] = "me:other-host"; body = json.dumps(other)
        return body, state
    r.read_classified = bumping
    before = st.saved[cp.path("r", "me")]
    st.events.append(_rec("close", "a", "2026-09-04T11:30:00Z", "2"))
    assert main(["fold", "r", "--agent", "me", "--now", "2026-09-04T12:00:00Z"], reader=r, writer=FakeWriter(st)) == 2
    err = capsys.readouterr().err
    assert "acting twice" in err and "me:other-host" in err and st.saved[cp.path("r", "me")] == before


def test_corrupt_checkpoint_is_refused_and_untouched(capsys):
    st = _team([]); st.docs[cp.path("r", "me")] = "{not json"
    assert _run(st) == 2 and cp.path("r", "me") not in st.saved and "corrupt" in capsys.readouterr().err


def test_unresolved_channel_is_refused():
    assert _run(FakeStore({}, [])) == 2


def test_verify_pointers_records_an_absent_pointer_and_default_reads_none():
    st = _team([_rec("open", "a", "2026-09-04T10:00:00Z", "1", ptr="team/r/task/gone.md")])
    assert _run(st, "--verify-pointers") == 3 and _ckpt(st)["unreadable_pointers"] == ["a"]
    st2 = _team([_rec("open", "a", "2026-09-04T10:00:00Z", "1")]); reads = []
    r = FakeReader(st2); orig = r.read_classified; r.read_classified = lambda p: (reads.append(p), orig(p))[1]
    main(["fold", "r", "--agent", "me", "--now", "2026-09-04T11:00:00Z"], reader=r, writer=FakeWriter(st2))
    assert not any("/task/" in p for p in reads)
```

```python
# packages/coord-fold/coord_fold/fold.py
"""One pass: read forward from the cursor, apply, re-read, persist, report (spec §3.3). O(new events).

Ruling 1 (G26): the cursor is the recorded_at of the LAST APPLIED event, never now.
Ruling 2 (G27): re-read before write; a moved generation is refused BY NAME, never overwritten.
Ruling 4 (G25): hitting max_events is a remainder (rc 0); zero progress with events present is the only error.
G31 (codex-coder r7): the cursor PASSES observed-irrelevant records; a gap is an unapplied RELEVANT event.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, NamedTuple

from . import channel, checkpoint as cp, events
from .transport import PointerTransport, TransportUnavailable

OVERLAP_SECONDS = 5
_BROADCAST = "all"
_EPOCH = "1970-01-01T00:00:00Z"


class FoldOutcome(NamedTuple):
    state: dict[str, Any]
    source: str
    applied: int
    unread: int
    rc: int


class FoldRefused(RuntimeError):
    pass


class FoldContended(RuntimeError):
    """The checkpoint generation moved under this pass: this agent is acting twice."""


def _minus_overlap(iso: str) -> str:
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return (dt - timedelta(seconds=OVERLAP_SECONDS)).strftime("%Y-%m-%dT%H:%M:%SZ")


def run(reader: PointerTransport, writer: Any, team: str, agent: str, *, now: str, writer_id: str,
        max_events: int = 5000, verify_pointers: bool = False) -> FoldOutcome:
    cfg = channel.resolve(reader, team)
    state, source = cp.load(reader, team, agent)
    if source == "corrupt":
        raise FoldRefused("checkpoint is corrupt — left untouched for forensics; repair or reseed it explicitly")
    if source == "error":
        raise TransportUnavailable("checkpoint unreadable")
    if source == "fresh":
        state = cp.empty(_EPOCH)
    generation = int(state.get("generation", 0))
    applied = unread = 0
    last_observed = state["cursor"]          # G31: advances past irrelevant records until the first UNAPPLIED relevant one
    for rec in reader.read_events(cfg["data_type"], _minus_overlap(state["cursor"])):
        at = rec.get("recorded_at") or last_observed
        ev = events.parse_event(rec)
        if ev is None or (ev["to"] not in (agent, _BROADCAST) and ev["from"] != agent):
            if not unread:
                last_observed = at
            continue
        if applied >= max_events:
            unread += 1
            continue
        cp.apply(state, ev)
        applied += 1
        last_observed = at
    if applied == 0 and unread:
        raise FoldRefused(f"no progress: {unread} events present and none applied (max_events={max_events})")
    state["cursor"] = last_observed
    state["unread_events"] = unread
    state["unreadable_pointers"] = []
    if verify_pointers:
        for slug, row in state["open"].items():
            _body, st = reader.read_classified(row["ptr"])
            if st != "ok":
                state["unreadable_pointers"].append(slug)
    again, src2 = cp.load(reader, team, agent)
    if src2 == "error":
        raise TransportUnavailable("checkpoint re-read before write did not answer; not writing")
    if src2 == "ok" and int(again.get("generation", 0)) != generation:
        raise FoldContended(f"{agent} is acting twice (two hosts or a duplicated cron): checkpoint generation moved "
                            f"{generation} -> {again.get('generation')} by writer {again.get('writer')!r} under this pass; not overwriting")
    state["generation"] = generation + 1
    state["writer"] = writer_id
    if not cp.save(writer, team, agent, state):
        raise TransportUnavailable("checkpoint save failed")
    return FoldOutcome(state, source, applied, unread, 3 if state["unreadable_pointers"] else 0)
```

```python
# packages/coord-fold/coord_fold/cli.py
"""Six verbs. Wiring only; the guarantee that no verb enumerates is G29's harness, not this file's shape."""
from __future__ import annotations

import argparse
import sys
import uuid
from datetime import datetime, timezone

from . import channel, checkpoint, events, fold
from .transport import CliPointerReader, CliPointerWriter, TransportUnavailable

RC_OK, RC_REFUSED, RC_UNKNOWN = 0, 2, 3


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _default_transports() -> tuple[CliPointerReader, CliPointerWriter]:
    from fulcra_common.client import find_fulcra_cli
    cli = find_fulcra_cli()
    if not cli:
        print("coord-fold: fulcra-api CLI not found on PATH", file=sys.stderr)
        raise SystemExit(RC_REFUSED)
    return CliPointerReader(cli=[cli]), CliPointerWriter(cli=[cli])


def _row_sort_key(item: tuple) -> tuple:
    return (item[1]["pri"], item[1]["at"])


def _render_open(state: dict) -> str:
    lines = []
    for slug, r in sorted(state["open"].items(), key=_row_sort_key):
        claimed = f"  claimed_by={r['claimed_by']}" if r.get("claimed_by") else ""
        lines.append(f"  [{r['pri']}] {slug}  from={r['from']}  ptr={r['ptr']}{claimed}")
    return "\n".join(lines) if lines else "  (nothing open)"


def _report_unknowns(state: dict) -> None:
    if state.get("unread_events"):
        print(f"fold: applied through {state['cursor']}; {state['unread_events']} events remain — bounded by new events, the next pass gets them")
    for slug in state.get("unreadable_pointers", []):
        print(f"fold: pointer for {slug} unreadable — that one row is UNKNOWN", file=sys.stderr)


def _emit_kind(reader, writer, team, *, sender, to, kind, slug, pri, ptr, at) -> int:
    try:
        cfg = channel.resolve(reader, team)
        payload = events.build_payload(at=at, sender=sender, to=to, kind=kind, slug=slug, pri=pri, ptr=ptr)
    except (channel.ChannelUnresolved, ValueError) as exc:
        print(f"{kind}: refused — {exc}", file=sys.stderr)
        return RC_REFUSED
    if not writer.write_event(cfg, payload, sender=sender):
        print(f"{kind}: UNKNOWN — the record write did not confirm", file=sys.stderr)
        return RC_UNKNOWN
    print(f"{kind} {slug} -> {to}")
    return RC_OK


def _owed_row(reader, team, agent, slug) -> tuple:
    """(row, load_state). A None row with load_state 'error' is UNKNOWN, never 'not owed'."""
    state, src = checkpoint.load(reader, team, agent)
    if src != "ok":
        return None, src
    return state["open"].get(slug), src


def cmd_fold(args, reader, writer) -> int:
    try:
        out = fold.run(reader, writer, args.team, args.agent, now=args.now, writer_id=f"{args.agent}:{uuid.uuid4().hex[:8]}", max_events=args.max_events, verify_pointers=args.verify_pointers)
    except (channel.ChannelUnresolved, fold.FoldRefused) as exc:
        print(f"fold: refused — {exc}", file=sys.stderr)
        return RC_REFUSED
    except fold.FoldContended as exc:
        print(f"fold: REFUSED, not overwriting — {exc}", file=sys.stderr)
        return RC_REFUSED
    except TransportUnavailable as exc:
        print(f"fold: UNKNOWN — event read did not complete ({exc}); cursor not advanced", file=sys.stderr)
        return RC_UNKNOWN
    print(f"fold [{args.agent}] cursor={out.state['cursor']} applied={out.applied} open={len(out.state['open'])} source={out.source}")
    print(_render_open(out.state))
    _report_unknowns(out.state)
    return out.rc


def cmd_emit(args, reader, writer) -> int:
    return _emit_kind(reader, writer, args.team, sender=args.sender, to=args.to, kind=args.kind, slug=args.slug, pri=args.pri, ptr=args.ptr, at=args.at)


def cmd_claim(args, reader, writer) -> int:
    row, src = _owed_row(reader, args.team, args.agent, args.slug)
    if src == "error":
        print(f"claim: UNKNOWN — checkpoint unreadable; cannot tell whether {args.slug} is owed", file=sys.stderr)
        return RC_UNKNOWN
    if row is None:
        print(f"claim: refused — {args.slug} is not open in {args.agent}'s checkpoint", file=sys.stderr)
        return RC_REFUSED
    return _emit_kind(reader, writer, args.team, sender=args.agent, to=row["from"], kind="claim", slug=args.slug, pri=row["pri"], ptr=row["ptr"], at=args.at)


def cmd_release(args, reader, writer) -> int:
    row, src = _owed_row(reader, args.team, args.agent, args.slug)
    if src == "error":
        print(f"release: UNKNOWN — checkpoint unreadable; cannot tell whether {args.slug} is owed", file=sys.stderr)
        return RC_UNKNOWN
    if row is None:
        print(f"release: refused — {args.slug} is not open in {args.agent}'s checkpoint", file=sys.stderr)
        return RC_REFUSED
    return _emit_kind(reader, writer, args.team, sender=args.agent, to=row["from"], kind="release", slug=args.slug, pri=row["pri"], ptr=row["ptr"], at=args.at)


def cmd_close(args, reader, writer) -> int:
    row, src = _owed_row(reader, args.team, args.agent, args.slug)
    if src == "error":
        print(f"close: UNKNOWN — checkpoint unreadable; cannot tell whether {args.slug} is owed", file=sys.stderr)
        return RC_UNKNOWN
    if row is None:
        print(f"close: refused — {args.slug} is not open in {args.agent}'s checkpoint", file=sys.stderr)
        return RC_REFUSED
    _body, st = reader.read_classified(args.evidence)
    if st == "absent":
        print(f"close: refused — evidence {args.evidence} is absent", file=sys.stderr)
        return RC_REFUSED
    if st == "error":
        print(f"close: UNKNOWN — evidence {args.evidence} unreadable; not closing on a read that did not answer", file=sys.stderr)
        return RC_UNKNOWN
    return _emit_kind(reader, writer, args.team, sender=args.agent, to=row["from"], kind="close", slug=args.slug, pri=row["pri"], ptr=args.evidence, at=args.at)


def cmd_status(args, reader, writer) -> int:
    state, src = checkpoint.load(reader, args.team, args.agent)
    if src == "fresh":
        print(f"status: {args.agent} has never folded — run `coord-fold fold`", file=sys.stderr)
        return RC_REFUSED
    if src == "corrupt":
        print("status: refused — checkpoint corrupt", file=sys.stderr)
        return RC_REFUSED
    if src == "error":
        print("status: UNKNOWN — checkpoint unreadable", file=sys.stderr)
        return RC_UNKNOWN
    print(f"status [{args.agent}] cursor={state['cursor']} open={len(state['open'])}")
    print(_render_open(state))
    _report_unknowns(state)
    return RC_UNKNOWN if state.get("unreadable_pointers") else RC_OK


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--now", default=None)
    common.add_argument("--at", default=None)
    p = argparse.ArgumentParser(prog="coord-fold")
    sub = p.add_subparsers(dest="verb", required=True)
    e = sub.add_parser("emit", parents=[common])
    e.add_argument("team")
    for flag in ("--from", "--to", "--kind", "--slug", "--pri"):
        e.add_argument(flag, dest="sender" if flag == "--from" else flag[2:], required=True)
    e.add_argument("--ptr", default=None)
    e.set_defaults(func=cmd_emit)
    for name, fn in (("fold", cmd_fold), ("claim", cmd_claim), ("release", cmd_release), ("close", cmd_close), ("status", cmd_status)):
        sp = sub.add_parser(name, parents=[common])
        sp.add_argument("team")
        if name in ("claim", "release", "close"):
            sp.add_argument("slug")
        sp.add_argument("--agent", required=True)
        if name == "close":
            sp.add_argument("--evidence", required=True)
        if name == "fold":
            sp.add_argument("--max-events", type=int, default=5000)
            sp.add_argument("--verify-pointers", action="store_true")
        sp.set_defaults(func=fn)
    return p


def main(argv: list[str] | None = None, *, reader=None, writer=None) -> int:
    args = build_parser().parse_args(argv)
    if args.now is None:
        args.now = _now()
    if args.at is None:
        args.at = args.now
    if reader is None or writer is None:
        reader, writer = _default_transports()
    return int(args.func(args, reader, writer))


if __name__ == "__main__":
    raise SystemExit(main())
```

(`cli.py` above is the complete file for all six verbs; Tasks 8–10 add only their tests. A single tagged block is what Task 0 materializes.)

Run — **13 passed** *(r32: measured; the fence holds 13 tests, "14" was a count error)*. Mutations: (g) `if not unread: last_observed = at` → `pass` → other-agents-traffic FAILS (G31); (a) swallow `TransportUnavailable` in the read loop → failed-read test FAILS; (b) drop the addressee filter → someone-else test FAILS; (c) force `verify_pointers=False` → absent-pointer test FAILS; (d) `state["cursor"] = now` → cursor-never-now FAILS (Ruling 1); (e) skip the re-read → concurrent-writer FAILS (Ruling 2); (f) treat the cap as rc 3 → capped-pass FAILS (Ruling 4). **Commit** — `coord-fold: fold verb, six-verb cli wiring, --verify-pointers (G9, G20, G23)`

---

### Task 8: `emit` tests

```python
# packages/coord-fold/tests/test_cli_emit.py
import json
from coord_fold import checkpoint as cp
from coord_fold.cli import main
from coord_fold_fakes import FakeReader, FakeStore, FakeWriter
CFG = "team/r/_coord/bus-v4/records.json"
CFG_DOC = json.dumps({"data_type": "MomentAnnotation/x", "api_version": "v1alpha1"})


def _m(st, argv):
    return main(argv, reader=FakeReader(st), writer=FakeWriter(st))


def test_emit_writes_one_v1_open_record():
    st = FakeStore({CFG: CFG_DOC}, [])
    rc = _m(st, ["emit", "r", "--from", "boss", "--to", "me", "--kind", "open", "--slug", "p1-x", "--pri", "P1", "--ptr", "team/r/task/p1-x.md", "--at", "2026-09-04T13:45:00Z"])
    assert rc == 0 and len(st.written) == 1 and set(st.written[0]["payload"]) == {"v", "at", "from", "to", "kind", "slug", "pri", "ptr"}


def test_emit_open_without_ptr_is_refused_and_writes_nothing(capsys):
    st = FakeStore({CFG: CFG_DOC}, [])
    assert _m(st, ["emit", "r", "--from", "boss", "--to", "me", "--kind", "open", "--slug", "s", "--pri", "P1"]) == 2
    assert not st.written and "ptr is required" in capsys.readouterr().err


def test_emit_then_fold_sees_the_event():
    st = FakeStore({CFG: CFG_DOC}, [])
    _m(st, ["emit", "r", "--from", "boss", "--to", "me", "--kind", "open", "--slug", "s", "--pri", "P2", "--ptr", "x.md", "--at", "2026-09-04T13:45:00Z"])
    _m(st, ["fold", "r", "--agent", "me", "--now", "2026-09-04T14:00:00Z"])
    assert "s" in json.loads(st.saved[cp.path("r", "me")])["open"]


def test_a_failed_write_exits_3_not_0():
    st = FakeStore({CFG: CFG_DOC}, []); w = FakeWriter(st); w.write_event = lambda *a, **k: False
    assert main(["emit", "r", "--from", "boss", "--to", "me", "--kind", "note", "--slug", "s", "--pri", "P3"], reader=FakeReader(st), writer=w) == 3
```

Run — 4 passed. Mutation: `return RC_UNKNOWN` → `return RC_OK` in `_emit_kind` → FAILS. **Commit.**

---

### Task 9: `claim`, `release`, `close` tests

```python
# packages/coord-fold/tests/test_cli_claim_release_close.py
import json
from coord_fold import checkpoint as cp
from coord_fold.cli import main
from coord_fold_fakes import FakeReader, FakeStore, FakeWriter
CFG = "team/r/_coord/bus-v4/records.json"
CFG_DOC = json.dumps({"data_type": "MomentAnnotation/x", "api_version": "v1alpha1"})
T0, T1, T2 = "2026-09-04T10:00:00Z", "2026-09-04T11:00:00Z", "2026-09-04T12:00:00Z"


def _open(slug, to="me"):
    p = {"v": 1, "at": T0, "from": "boss", "to": to, "kind": "open", "slug": slug, "pri": "P1", "ptr": f"team/r/task/{slug}.md"}
    return {"id": slug, "recorded_at": T0, "note": json.dumps(p)}


def _m(st, argv):
    return main(argv, reader=FakeReader(st), writer=FakeWriter(st))


def _folded(slug="s"):
    st = FakeStore({CFG: CFG_DOC}, [_open(slug)]); _m(st, ["fold", "r", "--agent", "me", "--now", T1]); return st


def _open_after_fold(st):
    _m(st, ["fold", "r", "--agent", "me", "--now", T2]); return json.loads(st.saved[cp.path("r", "me")])["open"]


def test_claim_annotates_on_the_next_fold():
    st = _folded(); assert _m(st, ["claim", "r", "s", "--agent", "me", "--at", T1]) == 0
    assert st.written[-1]["payload"]["kind"] == "claim" and _open_after_fold(st)["s"]["claimed_by"] == "me"


def test_release_drops_the_row_on_the_next_fold():
    st = _folded(); assert _m(st, ["release", "r", "s", "--agent", "me", "--at", T1]) == 0
    assert st.written[-1]["payload"]["kind"] == "release" and _open_after_fold(st) == {}


def test_claim_and_release_of_a_slug_i_do_not_owe_are_refused():
    st = _folded()
    assert _m(st, ["claim", "r", "not-mine", "--agent", "me"]) == 2 and _m(st, ["release", "r", "not-mine", "--agent", "me"]) == 2
    assert not any(w["payload"]["kind"] in ("claim", "release") for w in st.written)


def test_close_reads_the_evidence_then_emits_and_the_row_is_gone():
    st = _folded(); st.docs["team/r/_coord/responses/s/reply.md"] = "done"
    assert _m(st, ["close", "r", "s", "--agent", "me", "--evidence", "team/r/_coord/responses/s/reply.md", "--at", T1]) == 0
    assert st.written[-1]["payload"]["kind"] == "close" and _open_after_fold(st) == {}


def test_close_with_absent_evidence_is_refused_and_unreadable_is_unknown(capsys):
    st = _folded(); assert _m(st, ["close", "r", "s", "--agent", "me", "--evidence", "nope.md"]) == 2
    assert "absent" in capsys.readouterr().err and not any(w["payload"]["kind"] == "close" for w in st.written)
    # ONLY the evidence read fails (the checkpoint read still answers): this is the branch the plan's Task 9 mutation
    # removes. With fail_reads=True the checkpoint read failed first and the evidence branch was never reached,
    # so that mutation failed no test (measured while building, 2026-09-05).
    st.fail_paths = {"x.md"}; assert _m(st, ["close", "r", "s", "--agent", "me", "--evidence", "x.md"]) == 3
    err = capsys.readouterr().err; assert "evidence x.md unreadable" in err and not any(w["payload"]["kind"] == "close" for w in st.written)
    st.fail_paths = set(); st.fail_reads = True; assert _m(st, ["close", "r", "s", "--agent", "me", "--evidence", "x.md"]) == 3
    assert "checkpoint unreadable" in capsys.readouterr().err
```

Run — 5 passed. Mutation: make `cmd_close`'s `st == "error"` branch fall through → FAILS *(r32: measured FALSE on the r31 fixture — `fail_reads=True` failed the checkpoint read first, so the evidence branch was never reached and both messages contain "unreadable"; the fixture now fails ONLY the evidence path via `FakeStore.fail_paths` and the test asserts the evidence message, and the mutation fails it — measured on the branch)*. **Commit.**

---

### Task 10: `status` tests

```python
# packages/coord-fold/tests/test_cli_status.py
import json
from coord_fold import checkpoint as cp
from coord_fold.cli import main
from coord_fold_fakes import FakeReader, FakeStore, FakeWriter


def _st(state):
    st = FakeStore({}, []); st.docs[cp.path("r", "me")] = json.dumps(state); return st


def _m(st):
    return main(["status", "r", "--agent", "me"], reader=FakeReader(st), writer=FakeWriter(st))


def test_status_prints_open_rows_and_exits_0(capsys):
    s = cp.empty("T"); s["open"]["s"] = {"pri": "P1", "from": "boss", "ptr": "x.md", "at": "T"}
    assert _m(_st(s)) == 0 and "[P1] s" in capsys.readouterr().out


def test_status_exits_3_only_on_an_unknown_and_reports_a_remainder_at_0(capsys):
    s = cp.empty("T"); s["unread_events"] = 12; assert _m(_st(s)) == 0 and "12 events remain" in capsys.readouterr().out
    s = cp.empty("T"); s["unreadable_pointers"] = ["s9"]; assert _m(_st(s)) == 3 and "pointer for s9" in capsys.readouterr().err


def test_status_never_folded_exits_2_and_reads_no_events(capsys):
    assert _m(FakeStore({}, [])) == 2 and "never folded" in capsys.readouterr().err
    st = _st(cp.empty("T")); r = FakeReader(st); r.read_events = lambda *a: (_ for _ in ()).throw(AssertionError("status read events"))
    assert main(["status", "r", "--agent", "me"], reader=r, writer=FakeWriter(st)) == 0
```

Run — 3 passed; now run `python tests/proof/run_proof.py` — the proof (Task 1) is green end-to-end from here. **Commit** — `coord-fold: status tests; G29 proof green`

---

### Task 11: Degradation vocabulary (G11)

```python
# packages/coord-fold/tests/test_no_degraded_vocabulary.py
import pathlib
import coord_fold
PKG_DIR = pathlib.Path(coord_fold.__file__).parent


def test_the_token_degraded_never_appears_in_the_package():
    hits = {p.name for p in PKG_DIR.rglob("*.py") if "__pycache__" not in p.parts and "degraded" in p.read_text().lower()}
    assert not hits, hits
```

Mutation: append `# degraded` to `fold.py` → FAILS. **Commit.**

---

### Tasks 12–14: Old side (unchanged from r3; code at `6e0d42e5`)

**12 — Seed export** `obligations export-open <team> --agent <a>`: one bus-v4 `open` per open slug from the old stream fold, idempotent via `_coord/bus-v4/seeded/<a>.md`, eight-field payload written literally. Classified as a write. Mutation: remove the marker guard → idempotency FAILS.
**13 — Dual-emit** `coord_engine/dual_emit.py::mirror` called once at the end of `records.emit_event`; `directive→open`, `response→close` of `closes`, `claim→claim`, `verdict→note`; no v4 config → no-op; mirror failure never fails v3. Mutation: hardcode a cfg when absent → FAILS.
**14 — Comparator + `cutover-ready` + runbook**: tuples `(slug, pri, ptr)`; `AGREE n=k` / `DIVERGE slugs=[…]`; `cutover-ready` exits 0 only if trailing AGREE run ≥ 24, span ≥ 24h, the new open set both grew and shrank within it, **the injected divergence/recovery drill was performed and recorded (G13), and `scripts/ship_check.py fulcra <HEAD> --git <abs> --fulcra-api <abs>` exits 0 (Task 16; the two trust roots are STATED absolute paths, r29 — the bare two-argument form exits at argument parsing and must not appear in any runbook, r31)**. Mutation: force the 24h check true → the one-minute-apart test FAILS.

### Task 15: AGENTS.md (ship-gate)

The package, its four gate files, the proof driver and its two CI steps (the proof needs a macOS runner; exit 3 is UNKNOWN, not green); six verbs and exit codes; the remainder-vs-unknown distinction (G25) and the `degraded` ban; dependency direction; the reader/writer boundary; **the guarantee in G29's words** ("in that run the fold reached no store except through observed requests") with what it does not claim, the sandbox profile, and the store server's contract; **the tripwire's demotion in G30's words**; the cursor rules (G26/G31); §3.4 of the spec corrected (a type stops a method call, not `os.listdir`); and G24 — a revision is filed only with Task 0 green.

### Task 16: Review-shaped ship gate, bound to the shipped commit — responsibility distribution (G32)

**Not an automated gate, and not a plan-time approval.** Both reviewers (round 12): a verdict filed against the plan head and "carried" to a later implementation commit is stale evidence — one big file could ship behind it. So:

1. **When.** Only after implementation, on the **exact implementation commit** `<HEAD>` (40-hex) whose on-disk `packages/coord-fold/` is what ships. Never at plan time. Reading the materialized plan tree earlier is welcome as *feedback* and **carries nothing**.
2. **Register.** `coord-engine review request fulcra coord-fold-ship-<HEAD> --of packages/coord-fold --head <HEAD> --reviewer codex-reviewer --reviewer codex-coder`. The engine's `--head` keying means any head change is a new round with no verdicts.
3. **What the reviewer reads and files.** `git checkout <HEAD>`; read the on-disk tree against the rubric below; file with the **typed verb, nothing hand-uploaded**: `coord-engine review verdict fulcra coord-fold-ship-<HEAD> --head <HEAD> --verdict approve --from <reviewer> --note "tree: <git rev-parse <HEAD>:packages/coord-fold> …reading…"`. The engine writes an **append-only envelope** `verdicts/<HEAD>--<reviewer>--<UTC timestamp>-<nonce>.md` (as every verdict on this plan's register demonstrates); the `tree:` line in the note is the evidence. A verdict whose `tree` differs from the commit's is void.
4. **Ship check.** `scripts/ship_check.py <team> <HEAD> --git <abs> --fulcra-api <abs>` exits 0 only if: the stated trust roots resolve outside the tool environment *(r29)*; the working tree is at `<HEAD>` and clean for the package; **the engine's folded result** (`review status --json`) is `APPROVED` for that exact head with both required reviewers in `approvals`; and, for each required reviewer, **the exact winning shard the fold kept — `winning[reviewer].name` in that JSON — ** says `approve` and quotes the commit's tree hash. *(r15, both reviewers round 14: same-second shards were ordered by digest, so a refolded "latest" could be an earlier APPROVE; the ship check now never refolds filenames.)* **Engine prerequisite — bound to an APPROVED AND PINNED engine, never to a named commit** *(r16; codex-reviewer round 13 P0 one, verified at source by coord-boss `149e7d11`: the commit r15 named still had the double clock sample, so a named prerequisite bought nothing)*: `ship_check` downloads the fleet pin from `team/<team>/_coord/bus-v3/records.json`'s sibling `adopt-latest.sh` (the plan's own rule: pins come from there, never from a slug) and requires `PIN ∈ APPROVED_ENGINE_PINS` — a list in the script that is **empty until a deliberate plan revision adds the head that register `review-winning-envelope-e9c0089b` reads APPROVED for and that the pin PR shipped**. Until then `ship_check` refuses, which is the correct state. **And the pin must be the engine that answers** *(r17, codex-reviewer round 14: on a lagging host the authority can name an approved pin while `PATH` still executes an older engine that exposes `winning` with stale-approval defects)*: `ship_check` resolves the `coord-engine` executable it will call, reads `vcs_info.commit_id` from the `direct_url.json` beside the installed `coord_engine-*.dist-info` — the build-identity mechanism `adopt-latest.sh` itself uses — and refuses unless that commit equals the pin; no executable, no `direct_url.json`, or a different commit is a refusal **before** `winning` is consumed. **And the module that answers must be that build** *(r18, codex-coder round 15, reproduced end to end by coord-boss `8268376f`: a pinned launcher answered with a capability its build lacks because `subprocess.run` inherits `PYTHONPATH`/`PYTHONHOME` and an editable tree shadowed the installed package while `importlib.metadata` still reported the approved commit)*: `ship_check` resolves the executable **once, in `main`**, and passes that absolute path to *both* the identity read and every invocation — a bound runner, so no second `which` can ever run *(r19, both reviewers round 16: r18 resolved twice, and a `PATH` swap between the identity read and `review status` would let approved launcher A authorise unapproved launcher B; the regression makes `which` answer A then B and asserts exactly one resolution and that A is what executes)* — and *(r20, codex-coder round 17: scrubbing the environment blocks `PYTHONPATH`/`PYTHONHOME` only; a `.pth` or `sitecustomize.py` inside the launcher's own environment can prepend a stale tree while the adjacent dist-info still names the pin — on the proof host that site-packages already carries a `_virtualenv.pth`)* **the process that answers attests itself, on a trusted runtime**: `ship_check` never runs the launcher for the status, **and never runs the tool environment's interpreter either** *(r25, codex-reviewer round 22: `<tool-env>/bin/python` is selected from the same mutable environment, and a wrapper there can ignore `-I -S -B`, read the expected-tree path from `argv`, and print a forged payload — the gate would succeed without executing one pinned byte)*. **TRUST MODEL (r34, stated — codex-reviewer rounds 25–28, codex-coder round 29):** the gate defends against the MUTABLE TOOL ENVIRONMENT (everything an engine install controls), against PATH and environment-variable resolution, and against world-writable or cross-user temporary locations. It does NOT defend against a concurrent process running as the SAME USER on the gate host — such a process can replace the gate's own bytes, its interpreter or its git, so no pathname handoff between the gate and its trusted executables can be bound against it, and none is claimed to be. Within that model every remaining handoff is owned: the temp root is a gate-created 0700 directory under `~/.local/state/coord-fold/tmp` (an inherited `TMPDIR` is never consulted; an existing root that lost its privacy is refused, never repaired); the engine child receives its CLI as an absolute path in `FULCRA_CLI_COMMAND` (which its transport shlex-splits) with an EMPTY PATH — r29's private-bin symlink is gone; every downloaded body is checked for owner, non-link status, its directory's privacy AND its own group/other WRITE bits immediately before it is read *(r36, codex-reviewer round 31: the r34 check covered the directory's mode and the file's owner/type but not the file's own mode; the guarantee is INTEGRITY — nobody else could have modified the body between the CLI's write and the read — so a 0644 body, what any CLI writes under a normal umask, is fine and 0666 is refused)* (synchronized regressions: private dir made world-readable → refused; body replaced by a symlink → refused; body left 0666 → refused; intact → read; measured: a real download through the installed CLI passes). **Trust roots, stated:** the gate's own interpreter (`sys.executable` of the `ship_check` process — the host already trusts it to run the gate) and the operator-stated `git` and `fulcra-api` executables — **absolute paths given on the command line, never discovered through PATH** *(r29, codex-coder round 26: PATH is also how the mutable launcher is found; r25 named `git` a trust root and then found it through PATH)*, resolved by realpath exactly once, refused under the tool environment, and executed by that resolved path in every call; the attestation child's PATH is one EMPTY private directory and the stated `fulcra-api` reaches the engine as an absolute path in `FULCRA_CLI_COMMAND` *(r34 — the r29 symlink is gone; r35 removed the sentence that still described it)*, with the inherited overrides `FULCRA_CLI_COMMAND`, `FULCRA_API_BASE` and `COORD_TRANSPORT_HTTP` scrubbed first. The tool environment supplies **bytes only**, every one verified against the pinned tree before import; it supplies no code that runs unverified. **And the bytes that execute are the bytes that were verified** *(r26, codex-reviewer round 23: r25 hashed pathnames and let the normal importer reopen them, a TOCTOU window of the same class as executable resolution)*: the attestation reads each file once, hashes the bytes it keeps, and installs a meta-path importer that serves `coord_engine` and every submodule from those bytes; the path importer is never consulted for package code, a `coord_engine` name outside the verified tree is an ImportError, and the process refuses unless every loaded `coord_engine*` module came from that importer. *(r27, codex-coder round 24: r26 then put the tool environment's site-packages on `sys.path` "for metadata lookups", so a forged top-level `argparse.py` in that directory could answer for the whole attestation.)* **The tool environment's site-packages is never on `sys.path`.** Package resources (`default_models.json`, read via `importlib.resources`) are served from the verified bytes through the importer's resource reader, and the post-check covers every loaded module: any module whose file is under the tool environment and was not served by the verified importer, or any `sys.path` entry under it, is a refusal. *(r28, codex-reviewer round 25)* **The expected tree travels on the parent-child pipe** (stdin), never through a temp file the child would have to trust; the child echoes a canonical digest of exactly what it received and the parent requires that digest to equal its own, not merely the entry count — a same-cardinality substitution binding tampered bytes to substituted hashes has nowhere to happen. The attestation spawns the gate's interpreter with **`-I -S`** (no environment variables, no user site, no `.pth` processing, no `sitecustomize`/`usercustomize`), installs the verified importer — **the package and its resources are reachable only through `VerifiedImporter`; no tool-environment path is ever on `sys.path`** *(r28, codex-coder round 25: this sentence used to instruct the builder to insert the verified site-packages, which recreates the round-24 bypass with every other gate green)* — imports `coord_engine`, and in that same process reports `coord_engine.__file__`, the `direct_url` commit read by path, and `review status --json` computed in-process by `coord_engine.cli.main`. **Before importing** *(r22–r24)* the attestation requires **exactly one** `coord_engine-*.dist-info` (ambiguity refuses) and binds the executing bytes to something **outside the tool environment that cannot be edited to match** *(r24, codex-reviewer round 21: `RECORD` and `direct_url.json` are both mutable files in the same environment — replace `cli.py`, regenerate its RECORD row, leave `direct_url` naming the pin, and every r23 check passed)*: `ship_check` reads the **pinned commit's own `coord_engine/` tree** from the repository clone it runs in (`git ls-tree -r <PIN>:packages/coord-engine/coord_engine`; a pin whose commit is not in the clone refuses), and the attestation verifies every present non-`__pycache__` package file's **git blob hash** against that tree — no missing files, no extra files. The commit id fixes the tree; nothing in the environment can be regenerated to satisfy it. `RECORD` is no longer load-bearing and `direct_url.json` is reported, not trusted. Measured on the proof host: 50 pinned-tree entries, 50 present, 50 blob matches, none missing, none extra. **Bytecode cannot answer** *(r23, codex-coder: an unchecked-hash `.pyc` beside verified source is executed without consulting the source)*: the attestation runs with `-B` and a fresh, empty `pycache_prefix`, so the import system never looks at `__pycache__` beside the source and compiles the verified source (PEP 552), and any sourceless `.pyc`/`.pyo`/`.pyd`/`.so` present under the package refuses outright. The bytes that answer are the bytes the approved distribution installed. Measured on the proof host: one dist-info, 50 recorded files verified, none mismatched, none unrecorded. It refuses unless the answering module's file lies under that site-packages **and** the in-process commit equals the pin **and** *(r21, both reviewers round 18)* the attestation process exited 0, the in-process `review status` returned 0, and the payload is a dict of the expected shape — a status that says APPROVED while returning rc 3 (UNKNOWN) is a refusal, exactly as r19's `rc == 0` guard had it before the attestation replaced the direct call. Measured on the proof host: the isolated attestation imports through the verified importer (r27: 50/50 files verified, 48 modules served from memory against the installed 2.0.6 engine), reports `985a4be3` (the fleet pin) from `direct_url`, and answers the status. Regressions: a shadow `coord_engine` on `PYTHONPATH` is imported under the inherited environment and not under the scrubbed one; a tool environment whose site-packages holds both a `.pth` and a `sitecustomize.py` prepending a shadow imports the shadow under a normal site-enabled start and the approved package under the attestation; and *(r25)* a forging wrapper installed as `<tool-env>/bin/python` prints a perfect payload when run directly and is **never run by the gate** — the intact package still attests through the gate's interpreter, a tampered one still refuses; and *(r26)* a **synchronized** verify-then-replace-then-import regression: after verification, `cli.py` on disk is replaced with an APPROVED forgery, then the import runs — under the r25 path importer the forgery answers (positive control, the hole demonstrated), under the verified importer the verified rc-3 source answers; *(r27)* a forged top-level `argparse.py` planted in the tool environment answers under the r26 path insertion (positive control) and is never executed by the fixed attestation; and a replaced `default_models.json` on disk is never read — the verified resource bytes answer. `winning` in `review status --json` is then the **supersession fold's** kept shard, under whatever contract the engine register's APPROVED head carries — at round 8 that contract is: any CHANGES not resolved by a later shard dominates regardless of timestamp; an APPROVE lifts a CHANGES only by an edge that binds the target's **content digest** (so an in-place rewrite of a mutable shard un-resolves it) **and** whose target the **store's server-assigned mtime** proves strictly earlier than the superseder (so a predeclared edge to a later-written target never resolves; same minute or unknown fails closed). Rounds 6 and 7 called a name, then a client-written nonce, "causal"; both were wrong, because both were client-controlled — only the store supplies facts the client cannot choose. Self-links, dangling names, digest mismatches, equal keys and unproven causality fail closed to CHANGES and are surfaced as `malformed_supersedes`. **Both authoritative filename forms are accepted** *(P0 two, confirmed live on this very register: `<HEAD>--<reviewer>.md` and `<HEAD>--<reviewer>--<ts>-<digest>.md` coexist on the current head)*: a winning name is valid if it is exactly `<HEAD>--<reviewer>.md` or starts with `<HEAD>--<reviewer>--`; the fold, not the gate, decides which won. Any absence — no `winning`, no fold, a pin not in the approved set, an unreadable shard — is a refusal. Task 14's `cutover-ready` **calls it as `scripts/ship_check.py fulcra <HEAD> --git <abs> --fulcra-api <abs>` and fails closed** *(r32: this sentence was the last bare invocation; Task 0 now refuses a plan text that carries one, and `tests/test_ship_check.py` scans AGENTS.md, README and the script's Usage the same way)* — no cutover without it. `tests/test_ship_check.py` drives the script end to end with real envelope names for every outcome.

| Module | The reviewer confirms, by reading the shipped tree |
|---|---|
| `events.py` | payload construction and parsing happen **here**, as real logic, not as a re-export or a call into another module |
| `transport.py` | the only process launches in the package are here, and both classes do real work against the CLI |
| `channel.py` | channel resolution and its error cases are here |
| `checkpoint.py` | the checkpoint schema, apply, load and save are here; nothing else applies events |
| `fold.py` | the pass — read forward, apply, cursor rule, re-read, contention, remainder — is here, in `run` |
| `cli.py` | **argument parsing, dispatch and printing only**; every handler is a short delegation; no fold, parse, apply or persist logic; nothing duplicated from an owner |
| whole tree | no module is a shim; no owner's responsibility has migrated elsewhere; the tree equals the manifest |

```python
# packages/coord-fold/scripts/ship_check.py
"""Task 16 ship check. Exit 0 only with BOTH required responsibility-distribution approvals on the EXACT
commit — the engine's folded result AND, per reviewer, the exact WINNING shard the fold kept (never a
refold of filenames here), each quoting that commit's tree hash for packages/coord-fold.
Fails closed on any absence, including an engine that does not expose `winning`.
TRUST MODEL (r34, stated after codex-reviewer rounds 25-28): the gate defends against the MUTABLE TOOL ENVIRONMENT
(everything an engine install controls: its files, its interpreter, its bytecode, its metadata), against PATH and
environment-variable resolution, and against world-writable or cross-user temporary locations. It does NOT defend
against a concurrent process running as the SAME USER on the gate host: such a process can replace the gate's own
bytes, its interpreter, or its git, so no pathname handoff between the gate and its trusted executables can be
bound against it and none is claimed to be. Within that model every remaining handoff is owned: the temp root is a
gate-created 0700 directory under the user's state dir (an inherited TMPDIR is ignored), the engine child receives
its CLI as an absolute path in FULCRA_CLI_COMMAND (no link, no PATH lookup), and every downloaded body is checked
for owner, mode and non-link status immediately before it is read.
Usage: python scripts/ship_check.py <team> <40-hex head> --git <abs path> --fulcra-api <abs path>
(both trust roots are REQUIRED and stated as absolute paths — never discovered through PATH; r29/r31)"""
import json
import hashlib
import os
import pathlib
import re
import shutil
import subprocess
import sys

REQUIRED = ("codex-reviewer", "codex-coder")
# Engine heads whose register `review-winning-envelope-e9c0089b` read APPROVED AND whose pin PR shipped.
# EMPTY until a deliberate plan revision adds one; an empty set means ship_check refuses, correctly.
APPROVED_ENGINE_PINS: frozenset = frozenset({
    "e06e69e5d44d92b2b52a09020f53f2bd1ccdc1d5",   # r38, 2026-09-05: the fleet pin moved here (PR #698 merged; store adopt-latest.sh uploaded by Ash);
                                                  # this build carries the review supersession contract (PR #695, APPROVED by both required reviewers)
})


IMPORT_AFFECTING = ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PYTHONUSERBASE", "PYTHONSAFEPATH", "VIRTUAL_ENV", "CONDA_PREFIX")


def engine_env(fulcra_api=None):
    """The environment a child is invoked with: NOTHING that can change which coord_engine imports; PATH is one
    EMPTY private directory (no lookup can succeed); and the engine receives its CLI as an ABSOLUTE PATH in
    FULCRA_CLI_COMMAND, which its transport shlex-splits — no link, no PATH, no pathname the child resolves for
    itself (r34, replacing r29's private-bin symlink: codex-reviewer round 28). The inherited overrides
    (FULCRA_CLI_COMMAND, FULCRA_API_BASE, COORD_TRANSPORT_HTTP) are scrubbed first. (coord-boss 8268376f: a pinned
    launcher answered with the working tree's capabilities because subprocess.run inherited PYTHONPATH.)"""
    import shlex
    env = {k: v for k, v in os.environ.items() if k not in IMPORT_AFFECTING and k not in SCRUBBED_OVERRIDES and not k.startswith("PYTHON")}
    env["PYTHONNOUSERSITE"] = "1"
    env["PATH"] = private_dir("coord-fold-empty-path-")
    if fulcra_api:
        env["FULCRA_CLI_COMMAND"] = shlex.quote(os.path.realpath(fulcra_api))
    return env


def engine_executable():
    """Resolved ONCE, by main, to an absolute path. Nothing else may call `which`: the identity read and
    every invocation receive the SAME path (both reviewers, round 16: two resolutions let approved
    launcher A authorise unapproved launcher B after a PATH swap)."""
    exe = shutil.which("coord-engine")
    return os.path.realpath(exe) if exe else None


TRUST_ROOT_NAMES = ("git", "fulcra-api")
TRUSTED: dict = {}          # name -> realpath, filled ONCE by resolve_trust_roots(); sh() executes from here and nowhere else
SCRUBBED_OVERRIDES = ("FULCRA_CLI_COMMAND", "FULCRA_API_BASE", "COORD_TRANSPORT_HTTP")   # the engine's own command/store overrides


def tool_env_root(exe):
    """The mutable tool environment: the directory two levels above the launcher (<env>/bin/coord-engine)."""
    return str(os.path.realpath(str(pathlib.Path(exe).parent.parent)))


def resolve_trust_roots(stated, env_root):
    """r29 (codex-coder, round 26): the trusted executables are STATED by the operator as absolute paths — never
    discovered through PATH, which is also how the mutable launcher is found (a planted bin/git could bind tampered
    bytes to attacker hashes; a planted bin/fulcra-api could return an approved pin and approving verdicts). Each is
    resolved by realpath exactly once, refused if it or its target lies under the tool environment, and the resolved
    path is what every later call executes. -> (table, None) or (None, why)."""
    root = str(env_root).rstrip(os.sep) + os.sep
    out = {}
    for name in TRUST_ROOT_NAMES:
        p = stated.get(name)
        if not p or not os.path.isabs(p):
            return None, f"trust root {name!r} must be stated as an absolute path (--{name}); it is never discovered through PATH"
        real = os.path.realpath(p)
        if p.startswith(root) or real.startswith(root):
            return None, f"trust root {name!r} resolves under the tool environment {env_root} — refusing"
        if not (os.path.isfile(real) and os.access(real, os.X_OK)):
            return None, f"trust root {name!r} at {real} is not an executable file"
        out[name] = real
    return out, None


def gate_tmp_root():
    """The ONLY place the gate creates temporary state: ~/.local/state/coord-fold/tmp, created 0700 and verified
    (owned by this uid, no group/other bits) on every call. An inherited TMPDIR is never consulted (codex-coder round 29:
    tempfile.mkdtemp under an uncontrolled TMPDIR is a pathname handoff the gate did not own)."""
    import tempfile
    root = os.path.join(os.path.expanduser("~"), ".local", "state", "coord-fold", "tmp")
    if not os.path.isdir(root):
        os.makedirs(root, mode=0o700, exist_ok=True)
        os.chmod(root, 0o700)                       # umask-proof on creation only; an EXISTING root that lost its privacy is refused, never repaired
    st = os.stat(root)
    if st.st_uid != os.getuid() or (st.st_mode & 0o077):
        raise RuntimeError(f"gate temp root {root} is not a private directory of this user (uid {st.st_uid}, mode {oct(st.st_mode & 0o777)})")
    strip_acls(root)                                # r38: an inherited ACL on the root would survive the chmod above
    tempfile.tempdir = root
    return root


def acl_entries(path):
    """ACL entries on a path. r38 (codex-reviewer round 33): on macOS an ACL survives chmod and is INVISIBLE to stat, so a
    directory that reports 0700 can still grant everyone write/delete via an inherited entry. Listed through the OS's own
    /bin/ls (an OS trust root, like /bin/chmod below); on Linux, the POSIX-ACL xattr."""
    # r39 (both reviewers, round 34): an inspection that FAILS is not "no ACL". A failed ls / listxattr refuses.
    if sys.platform == "darwin":
        p = subprocess.run(["/bin/ls", "-led", path], capture_output=True, text=True)
        if p.returncode != 0 or not p.stdout.strip():
            raise PermissionError(f"ACL inspection of {path} failed (rc {p.returncode}): {p.stderr.strip()[:120]}")
        return [ln.strip() for ln in p.stdout.splitlines()[1:] if re.match(r"\s*\d+:\s", ln)]
    try:
        return [x for x in os.listxattr(path) if x.startswith("system.posix_acl")]
    except OSError as exc:
        raise PermissionError(f"ACL inspection of {path} failed: {exc}") from exc


def strip_acls(path):
    """Remove every ACL entry (inherited ones included) from a path the gate just created, then PROVE none remain."""
    # r39 (both reviewers, round 34): a removal that FAILS refuses; it is never "stripped".
    if sys.platform == "darwin":
        p = subprocess.run(["/bin/chmod", "-N", path], capture_output=True, text=True)
        if p.returncode != 0:
            raise RuntimeError(f"ACL removal on {path} failed (rc {p.returncode}): {p.stderr.strip()[:120]}")
    else:
        for x in acl_entries(path):
            try:
                os.removexattr(path, x)
            except OSError as exc:
                raise RuntimeError(f"ACL removal on {path} failed: {exc}") from exc
    left = acl_entries(path)
    if left:
        raise RuntimeError(f"{path} still carries ACL entries after stripping: {left[:2]}")


def private_dir(prefix):
    """A fresh 0700 directory under the gate's own temp root, with no ACL entries (r38)."""
    import tempfile
    d = tempfile.mkdtemp(prefix=prefix, dir=gate_tmp_root())
    os.chmod(d, 0o700)
    strip_acls(d)
    return d


def read_owned_file(path):
    """Read a file the gate expects to own, refusing if the handoff state changed before the read: a symlink, a
    non-regular file, another owner, or a containing directory that is no longer private (r34)."""
    import stat as _stat
    d = os.path.dirname(path)
    ds = os.stat(d)
    if ds.st_uid != os.getuid() or (ds.st_mode & 0o077):
        raise PermissionError(f"the private directory {d} is no longer private (uid {ds.st_uid}, mode {oct(ds.st_mode & 0o777)})")
    if acl_entries(d):
        raise PermissionError(f"the private directory {d} carries ACL entries: {acl_entries(d)[:2]}")      # r38: invisible to stat
    ls = os.lstat(path)
    if _stat.S_ISLNK(ls.st_mode) or not _stat.S_ISREG(ls.st_mode):
        raise PermissionError(f"{path} is not a regular file the gate wrote")
    if ls.st_uid != os.getuid():
        raise PermissionError(f"{path} is owned by uid {ls.st_uid}, not this user")
    if acl_entries(path):
        raise PermissionError(f"{path} carries ACL entries: {acl_entries(path)[:2]}")                          # r38: an ACL can grant write past the mode
    if ls.st_mode & 0o022:                                  # r36 (codex-reviewer round 31): the BODY's own mode, not only the directory's.
        # The guarantee is INTEGRITY (nobody else could have modified the body between the CLI's write and this read),
        # so the group/other WRITE bits are what matter; a 0644 body — what any CLI writes under a normal umask — is fine.
        raise PermissionError(f"{path} is writable by others (mode {oct(ls.st_mode & 0o777)})")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, encoding="utf-8") as fh:
        return fh.read()


def store_read(remote):
    """Read one store file through the trusted fulcra-api into a PRIVATE temp file and return its text.
    r30 (found by the first real measurement of fleet_pin, 2026-09-05): the real CLI validates LOCAL_FILE as a
    readable path and REFUSES /dev/stdout whenever stdout is a pipe. r34: the file lives under the gate's own temp
    root and is checked for owner, mode and non-link status immediately before the read. -> (rc, text, err)."""
    d = private_dir("coord-fold-store-")
    f = os.path.join(d, "body")
    try:
        rc, _, err = sh("fulcra-api", "file", "download", remote, f)
        if rc:
            return rc, "", err
        try:
            return 0, read_owned_file(f), ""
        except (OSError, PermissionError) as exc:
            return 3, "", f"downloaded body refused: {exc}"
    finally:
        shutil.rmtree(d, ignore_errors=True)


def sh(*argv):
    """Runs a TRUSTED executable by its once-resolved absolute path. A bare name never reaches the OS (r29)."""
    name, rest = argv[0], list(argv[1:])
    exe = TRUSTED.get(name)
    if not exe:
        raise RuntimeError(f"sh({name!r}) before trust roots were resolved — a bare name never executes")
    p = subprocess.run([exe, *rest], capture_output=True, text=True, env=engine_env(TRUSTED.get("fulcra-api")))
    return p.returncode, p.stdout.strip(), p.stderr.strip()


ATTEST = r"""
import sys, json, io, contextlib, os, glob, hashlib, importlib.abc, importlib.machinery
def refuse(why):
    print(json.dumps({"refused": why})); sys.exit(2)
def verify_tree(site, expected):
    # r26 (codex-reviewer, round 23): read every file ONCE, hash the bytes we KEEP, and execute those bytes.
    # Hashing a pathname and letting the importer reopen it later is a TOCTOU window; this closes it.
    pkg = os.path.join(site, "coord_engine")
    present = {os.path.relpath(os.path.join(dp, f), pkg) for dp, _, fs in os.walk(pkg) for f in fs if "__pycache__" not in dp}
    sourceless = sorted(f for f in present if f.endswith((".pyc", ".pyo", ".pyd", ".so")))
    if sourceless:
        refuse(f"compiled/sourceless files under coord_engine/ could answer: {sourceless[:3]}")
    missing = sorted(set(expected) - present)
    if missing:
        refuse(f"files in the pinned commit's tree are missing from the installed package: {missing[:3]}")
    extra = sorted(present - set(expected))
    if extra:
        refuse(f"files under coord_engine/ that the pinned commit's tree does not contain: {extra[:3]}")
    blobs = {}
    for rel, want in sorted(expected.items()):
        data = open(os.path.join(pkg, rel), "rb").read()
        got = hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()
        if got != want:
            refuse(f"installed file does not match the pinned commit's blob: {rel}")
        blobs[rel] = data
    return pkg, blobs
class VerifiedResources:
    # importlib.resources reader over the VERIFIED BYTES (r27): package data such as default_models.json is
    # served from memory too, never re-read from disk. resource_path is refused: there is no trusted path.
    def __init__(self, blobs, prefix):
        self.blobs, self.prefix = blobs, prefix
    def open_resource(self, name):
        rel = self.prefix + name
        if rel not in self.blobs:
            raise FileNotFoundError(rel)
        return io.BytesIO(self.blobs[rel])
    def resource_path(self, name):
        raise FileNotFoundError(name)
    def is_resource(self, name):
        return (self.prefix + name) in self.blobs
    def contents(self):
        return [r[len(self.prefix):] for r in self.blobs if r.startswith(self.prefix) and "/" not in r[len(self.prefix):]]
class VerifiedImporter(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    # Serves coord_engine, every submodule AND every package resource from the VERIFIED BYTES. The filesystem
    # is never reopened for package code or data. A coord_engine name outside the verified tree is an
    # ImportError, never a fallback to the path importer.
    def __init__(self, root, blobs):
        self.root, self.blobs, self.loaded = root, blobs, {}
    def get_resource_reader(self, fullname):
        parts = fullname.split(".")[1:]
        return VerifiedResources(self.blobs, "/".join(parts) + "/" if parts else "")
    def find_spec(self, fullname, path=None, target=None):
        parts = fullname.split(".")
        if parts[0] != "coord_engine":
            return None
        sub = "/".join(parts[1:])
        for rel, is_pkg in (((sub + "/" if sub else "") + "__init__.py", True), (sub + ".py", False)):
            if rel in self.blobs:
                origin = os.path.join(self.root, rel)
                spec = importlib.machinery.ModuleSpec(fullname, self, origin=origin, is_package=is_pkg)
                spec.has_location = True
                if is_pkg:
                    spec.submodule_search_locations = [os.path.dirname(origin)]
                return spec
        raise ImportError(f"{fullname} is not in the verified tree")
    def create_module(self, spec):
        return None
    def exec_module(self, module):
        rel = os.path.relpath(module.__spec__.origin, self.root)
        self.loaded[module.__name__] = rel
        exec(compile(self.blobs[rel], module.__spec__.origin, "exec", dont_inherit=True), module.__dict__)
def install_verified_importer(pkg, blobs):
    for n in list(sys.modules):
        if n == "coord_engine" or n.startswith("coord_engine."):
            refuse(f"coord_engine was already imported before the verified importer was installed: {n}")
    imp = VerifiedImporter(pkg, blobs)
    sys.meta_path.insert(0, imp)
    return imp
def run_status(team, slug):
    import coord_engine
    from coord_engine import cli
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cli.main(["review", "status", team, slug, "--json"])
    lines = [l for l in buf.getvalue().splitlines() if l.startswith("{")]
    return rc, (json.loads(lines[-1]) if lines else None), os.path.realpath(coord_engine.__file__)
def rogue_modules(imp, site):
    # r27 (codex-coder, round 24): EVERY loaded module is checked, not only coord_engine names. A module whose
    # file lives under the tool environment and was not served by the verified importer executed unverified code.
    root = os.path.realpath(site) + os.sep
    out = []
    for n, m in list(sys.modules.items()):
        served = getattr(m, "__loader__", None) is imp
        if n == "coord_engine" or n.startswith("coord_engine."):
            if not served:
                out.append(n)
            continue
        f = getattr(m, "__file__", None)
        if f and os.path.realpath(f).startswith(root):
            out.append(n)
    return sorted(out)
def paths_under(site):
    root = os.path.realpath(site) + os.sep
    return [p for p in sys.path if os.path.realpath(p or os.getcwd()).startswith(root) or os.path.realpath(p or os.getcwd()) == root[:-1]]
def canonical_tree_digest(tree):
    return hashlib.sha256(json.dumps(tree, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
def main():
    site, team, slug = sys.argv[1:4]
    dis = sorted(glob.glob(os.path.join(site, "coord_engine-*.dist-info")))
    if len(dis) != 1:
        refuse(f"{len(dis)} coord_engine dist-infos under site-packages; exactly one is required")
    # r28 (codex-reviewer, round 25): the expected tree arrives on STDIN — the parent-child pipe — never via a
    # pathname the child would have to trust. The child echoes a canonical digest of exactly what it received and
    # the parent compares it to its own; a same-count substitution has nowhere to happen.
    try:
        expected = json.loads(sys.stdin.read())
    except ValueError:
        refuse("the expected tree on stdin is not JSON")
    if not isinstance(expected, dict) or not expected or not all(isinstance(k, str) and isinstance(v, str) and len(v) == 40 for k, v in expected.items()):
        refuse("the expected tree on stdin is not a non-empty {relpath: blob-sha1} object")
    tree_digest = canonical_tree_digest(expected)
    pkg, blobs = verify_tree(site, expected)
    if sys.pycache_prefix is None or os.listdir(sys.pycache_prefix):
        refuse("bytecode is not redirected to a fresh empty pycache_prefix; stale __pycache__ could answer")
    try:
        du = json.load(open(os.path.join(dis[0], "direct_url.json")))
    except (OSError, ValueError):
        du = {}
    if paths_under(site):
        refuse(f"the tool environment is on sys.path before attestation: {paths_under(site)[:2]}")
    imp = install_verified_importer(pkg, blobs)
    # r27 (codex-coder, round 24): the tool environment's site-packages is NEVER placed on sys.path. r26 inserted it
    # "for metadata lookups" and thereby let a forged top-level argparse.py in that directory answer for the whole
    # attestation. direct_url.json is read by path above; nothing else from that directory is needed.
    rc, status, file = run_status(team, slug)
    rogue = rogue_modules(imp, site)
    if rogue:
        refuse(f"modules were loaded from the tool environment outside the verified importer: {rogue[:3]}")
    if paths_under(site):
        refuse(f"the tool environment appeared on sys.path during attestation: {paths_under(site)[:2]}")
    print(json.dumps({"file": file, "reported_commit": du.get("vcs_info", {}).get("commit_id"),
                      "tree_verified": len(blobs), "dist_info": os.path.basename(dis[0]),
                      "loader": "verified-bytes", "memory_loaded": len(imp.loaded), "tree_digest": tree_digest,
                      "rc": rc, "status": status}))
    sys.exit(rc)                      # the outer process carries the inner verdict's rc too; both are checked
if __name__ == "__main__":
    main()
"""


def gate_python():
    """THE TRUSTED RUNTIME for the attestation: the interpreter running this gate (a trust root the host
    already relies on, alongside git). NEVER the tool environment's bin/python — codex-reviewer, round 22:
    that file is part of the mutable environment and a wrapper there can forge the whole payload."""
    return sys.executable


def dist_site_packages(exe):
    """The site-packages that holds the coord_engine dist-info beside `exe` — the ONLY path the attestation may import from."""
    root = pathlib.Path(exe).parent.parent
    for di in sorted(root.glob("lib/python*/site-packages/coord_engine-*.dist-info")):
        return os.path.realpath(di.parent)
    return None


def pinned_tree(pin):
    """{relpath: git blob sha1} for coord_engine/** at the PINNED COMMIT, read from the clone ship_check runs in.
    The commit id fixes this tree; nothing inside a tool environment can be regenerated to satisfy it.
    None if the commit is not in the clone (fail closed: fetch it, do not guess)."""
    rc, _, _ = sh("git", "cat-file", "-e", f"{pin}^{{commit}}")
    if rc:
        return None
    rc, out, _ = sh("git", "ls-tree", "-r", "--format=%(objectname) %(path)", f"{pin}:packages/coord-engine/coord_engine")
    if rc or not out:
        return None
    return {path: obj for obj, path in (line.split(" ", 1) for line in out.splitlines() if " " in line)}


def attested_status(exe, team, slug, pin):
    """The status, from a process that PROVES what answered it (codex-coder, round 17): the launcher
    env's interpreter under -I -S (no env, no user site, no .pth, no sitecustomize), NO tool-environment
    path on sys.path at all — the package and its resources are reachable only through VerifiedImporter
    (r28: an earlier version of this docstring instructed the opposite) — the executing bytes verified against the PINNED
    COMMIT's tree (codex-reviewer, round 21), and the fold computed in that same process.
    -> (ok, detail, status_dict_or_None). The verifier is the GATE's interpreter (r25), so the tool
    environment cannot substitute the process that reports on it; and the bytes that EXECUTE are the bytes
    that were VERIFIED (r26): read once, hashed, served by an in-memory importer — the filesystem is never
    reopened for package code, so a replacement after verification cannot answer. The tool environment's
    site-packages is never on sys.path (r27), package resources are served from the verified bytes, and any
    module loaded from that directory outside the verified importer is a refusal."""
    py, site = gate_python(), dist_site_packages(exe)
    if not py or not site:
        return False, f"no site-packages beside {exe} (or no gate interpreter)", None
    tree = pinned_tree(pin)
    if not tree:
        return False, f"the pinned commit {pin} (or its coord_engine tree) is not in this clone — fetch it; not guessing", None
    import tempfile
    fresh_pycache = tempfile.mkdtemp(prefix="coord-fold-attest-pyc-")     # empty: no stale bytecode can be consulted (PEP 552)
    canonical = json.dumps(tree, sort_keys=True, separators=(",", ":"))
    tree_digest = hashlib.sha256(canonical.encode()).hexdigest()
    # r28: the tree goes down the pipe (stdin), never through a file the child would have to trust.
    p = subprocess.run([py, "-I", "-S", "-B", "-X", f"pycache_prefix={fresh_pycache}", "-c", ATTEST, site, team, slug], input=canonical, capture_output=True, text=True, env=engine_env(TRUSTED.get("fulcra-api")))
    try:
        a = json.loads([l for l in p.stdout.splitlines() if l.startswith("{")][-1])
    except (ValueError, IndexError):
        return False, f"attestation did not answer (rc {p.returncode}): {p.stderr.strip()[-200:]}", None
    if not isinstance(a, dict):
        return False, "attestation payload is not an object", None
    if a.get("refused"):
        return False, f"the attestation refused before importing: {a['refused']}", None
    if a.get("tree_verified") != len(tree):
        return False, f"the attestation verified {a.get('tree_verified')!r} files against the pinned tree of {len(tree)}", None
    if a.get("tree_digest") != tree_digest:
        return False, "the attestation verified against a tree whose canonical digest is not the pinned tree's (r28) — a substituted expected-tree, not a count mismatch", None
    if not str(a.get("file", "")).startswith(site + os.sep):
        return False, f"the module that answered lives at {a.get('file')!r}, not under {site} — a startup hook or shadow tree answered", None
    if a.get("loader") != "verified-bytes" or not isinstance(a.get("memory_loaded"), int) or a.get("memory_loaded") < 1:
        return False, "the answering process did not execute the verified bytes through the verified importer (r26)", None
    # BOTH exit codes, before any status is trusted (both reviewers, round 18): a status that
    # prints an APPROVED-shaped tally while returning rc 3 is UNKNOWN, not approval.
    if p.returncode != 0 or a.get("rc") != 0:
        return False, f"the attested review status returned rc {a.get('rc')!r} (process rc {p.returncode}) — UNKNOWN is not approval", None
    status = a.get("status")
    if not isinstance(status, dict) or not isinstance(status.get("state"), str) or not isinstance(status.get("approvals"), list) or not isinstance(status.get("head"), str):
        return False, "the attested status is not a review tally of the expected shape", None
    return True, pin, status                                   # the binding is the tree, not a reported commit


def executing_engine_commit(exe):
    """The build commit of `exe` — the same absolute path that will answer `review status` — from the
    direct_url.json beside its installed dist-info, the identity adopt-latest.sh trusts. None if unprovable."""
    if not exe:
        return None
    root = pathlib.Path(exe).parent.parent                              # <tool-env>/bin/coord-engine -> <tool-env>
    for du in sorted(root.glob("lib/python*/site-packages/coord_engine-*.dist-info/direct_url.json")):
        try:
            commit = json.loads(du.read_text()).get("vcs_info", {}).get("commit_id")
        except (OSError, ValueError):
            return None
        return commit if isinstance(commit, str) and re.fullmatch(r"[0-9a-f]{40}", commit) else None
    return None


def fleet_pin(team: str):
    """The engine pin the fleet runs — from adopt-latest.sh, never from a slug name."""
    rc, body, _ = store_read(f"team/{team}/_coord/bus-v3/adopt-latest.sh")
    m = re.search(r'^PIN="([0-9a-f]{40})"', body, re.M) if rc == 0 else None
    return m.group(1) if m else None


def winning_name_ok(name: str, head: str, reviewer: str) -> bool:
    """Both authoritative forms: the exact-head plain shard, or an append-only envelope."""
    return name == f"{head}--{reviewer}.md" or name.startswith(f"{head}--{reviewer}--")


def main(team: str, head: str, git: str = None, fulcra_api: str = None) -> int:
    if not re.fullmatch(r"[0-9a-f]{40}", head):
        print("ship_check: head must be a 40-hex commit"); return 1
    try:
        gate_tmp_root()                                                 # r34: our own 0700 temp root; TMPDIR is never consulted
    except (RuntimeError, PermissionError) as exc:
        print(f"ship_check: {exc} — refusing"); return 1                # r39: a root that cannot be made/proven private is a refusal, not a crash
    exe = engine_executable()                                           # THE one resolution of the launcher
    if not exe:
        print("ship_check: coord-engine not found on PATH — refusing"); return 1
    table, why = resolve_trust_roots({"git": git, "fulcra-api": fulcra_api}, tool_env_root(exe))   # r29: stated, once, outside the env
    if table is None:
        print(f"ship_check: {why}"); return 1
    TRUSTED.clear(); TRUSTED.update(table)
    pin = fleet_pin(team)
    if pin is None or pin not in APPROVED_ENGINE_PINS:
        print(f"ship_check: fleet engine pin {pin!r} is not an APPROVED+PINNED corrected engine (approved set: {sorted(APPROVED_ENGINE_PINS)}) — refusing; the fold's ordering contract is not proven on this engine"); return 1
    local = executing_engine_commit(exe)
    if local != pin:
        print(f"ship_check: the coord-engine at {exe} is build {local!r}, not the approved pin {pin} — refusing; a lagging host must not trust its own unapproved fold"); return 1
    slug = f"coord-fold-ship-{head}"
    ok, detail, fold = attested_status(exe, team, slug, pin)            # the answering process attests itself against the PINNED tree
    if not ok:
        print(f"ship_check: {detail} — refusing"); return 1
    if detail != pin:
        print(f"ship_check: the process that answered reports build {detail!r}, not the approved pin {pin} — refusing"); return 1
    rc, at, _ = sh("git", "rev-parse", "HEAD")
    if rc or at != head:
        print(f"ship_check: working tree is at {at!r}, not {head}"); return 1
    rc, dirty, _ = sh("git", "status", "--porcelain", "--", "packages/coord-fold")
    if rc or dirty:
        print("ship_check: packages/coord-fold has uncommitted changes — the on-disk tree is not the commit"); return 1
    rc, tree, _ = sh("git", "rev-parse", f"{head}:packages/coord-fold")
    if rc or not re.fullmatch(r"[0-9a-f]{40}", tree):
        print("ship_check: no packages/coord-fold tree at that commit"); return 1
    if not fold:
        print(f"ship_check: no folded review result for {slug}"); return 1
    winning = fold.get("winning")
    if not isinstance(winning, dict):
        print("ship_check: UNKNOWN — this engine does not expose `winning` (needs review-winning-envelope); refusing rather than refolding filenames"); return 1
    ok = True
    if fold.get("state") != "APPROVED" or fold.get("head") != head or set(REQUIRED) - set(fold.get("approvals", [])):
        print(f"ship_check: folded result is {fold.get('state')} on head {fold.get('head')} with approvals {fold.get('approvals')}"); ok = False
    for reviewer in REQUIRED:
        win = winning.get(reviewer) or {}
        name = win.get("name")
        if not name or not winning_name_ok(name, head, reviewer):
            print(f"ship_check: no winning shard from {reviewer} for {head} (fold says {win})"); ok = False; continue
        if win.get("verdict") != "approve":
            print(f"ship_check: {reviewer}'s winning shard {name} is {win.get('verdict')}, not approve"); ok = False
        rc, body, err = store_read(f"team/{team}/review/{slug}/verdicts/{name}")
        if rc:
            print(f"ship_check: cannot read {name} ({err[:80]})"); ok = False; continue
        verdict = re.search(r"^verdict:\s*(\S+)", body, re.M)
        quoted = re.search(r"^\s*tree:\s*([0-9a-f]{40})", body, re.M)
        if not verdict or verdict.group(1) != "approve":
            print(f"ship_check: {name} says {verdict.group(1) if verdict else 'nothing'}, not approve"); ok = False
        if not quoted or quoted.group(1) != tree:
            print(f"ship_check: {reviewer} quotes tree {quoted.group(1) if quoted else 'none'}, the commit's is {tree}"); ok = False
    print("ship_check: OK — folded APPROVED and both winning shards approve this exact head and tree" if ok else "ship_check: REFUSED")
    return 0 if ok else 1


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="coord-fold ship gate")
    ap.add_argument("team"); ap.add_argument("head")
    ap.add_argument("--git", required=True, help="absolute path of the trusted git (never discovered through PATH)")
    ap.add_argument("--fulcra-api", required=True, dest="fulcra_api", help="absolute path of the trusted fulcra-api")
    a = ap.parse_args()
    raise SystemExit(main(a.team, a.head, git=a.git, fulcra_api=a.fulcra_api))
```

```python
# packages/coord-fold/tests/test_ship_check.py
"""End-to-end over REAL append-only envelope names. The script consumes `winning` from the typed
surface and never refolds: a same-second earlier APPROVE with a lexically larger digest cannot beat
the later CHANGES the fold kept (both reviewers, round 14)."""
import importlib.util
import json
import pathlib

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "ship_check.py"
spec = importlib.util.spec_from_file_location("ship_check", SCRIPT)
ship_check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ship_check)

HEAD = "e67ac6474ebb38a93bc747afd422dfd6935998bc"
OTHER = "b6d867d96abf91ef2d82f066bf2e4977429cbe54"
TREE = "1111111111111111111111111111111111111111"
ENV = {r: f"{HEAD}--{r}--2026-09-05T01:00:24Z-626c9635.md" for r in ("codex-reviewer", "codex-coder")}
APPROVE = f"verdict: approve\ntree: {TREE}"
PIN = "f" * 40


def _approve_pin(monkeypatch, local=PIN):
    monkeypatch.setattr(ship_check, "APPROVED_ENGINE_PINS", frozenset({PIN}))
    monkeypatch.setattr(ship_check, "engine_executable", lambda: "/tool/bin/coord-engine")
    monkeypatch.setattr(ship_check, "executing_engine_commit", lambda exe: local)


def world(*, at=HEAD, dirty="", state="APPROVED", approvals=("codex-reviewer", "codex-coder"), fold_head=HEAD,
          winning=None, bodies=None, pin=PIN):
    if winning is None:
        winning = {r: {"name": n, "verdict": "approve", "sort_key": "2026-09-05T01:00:24.000000Z"} for r, n in ENV.items()}
    if bodies is None:
        bodies = {w["name"]: APPROVE for w in winning.values() if w.get("name")} if isinstance(winning, dict) else {}
    def fake_sh(*argv):
        a = list(argv)
        if a[:2] == ["git", "rev-parse"] and a[2] == "HEAD":
            return 0, at, ""
        if a[:2] == ["git", "status"]:
            return 0, dirty, ""
        if a[:2] == ["git", "rev-parse"]:
            return 0, TREE, ""
        if a[:3] == ["fulcra-api", "file", "download"] and a[3].endswith("adopt-latest.sh"):
            pass
        if a[:1] == ["fulcra-api"] and a[1:3] == ["file", "download"] and not a[3].endswith("adopt-latest.sh"):
            pass
        if a[:2] == ["/tool/bin/coord-engine", "review"] or a[:2] == ["coord-engine", "review"]:
            fold = {"state": state, "head": fold_head, "approvals": list(approvals)}
            if winning != "absent":
                fold["winning"] = winning
            return 0, json.dumps(fold), ""
        if a[:3] == ["fulcra-api", "file", "download"]:
            assert len(a) == 5 and a[4] != "/dev/stdout", a                                       # r30: the real CLI refuses /dev/stdout under a pipe
            if a[3].endswith("adopt-latest.sh"):
                body = f'#!/bin/sh\nPIN="{pin}"   # coord-engine\n' if pin else None
            else:
                n = a[3].rsplit("/", 1)[-1]; body = bodies.get(n)
            if body is None:
                return 1, "", "Error: File not found"
            pathlib.Path(a[4]).write_text(body); return 0, "", ""                                   # the body lands in LOCAL_FILE, as the real CLI does
        raise AssertionError(a)
    return fake_sh


def _attest_from(fake, attested_commit=PIN):
    def attested(exe, team, slug, pin):
        rc, out, _ = fake("coord-engine", "review", "status", team, slug, "--json")
        return True, attested_commit, json.loads(out)
    return attested


def _blob(path):
    import hashlib
    data = pathlib.Path(path).read_bytes()
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def _tree_of(site):
    pkg = site / "coord_engine"
    return {str(f.relative_to(pkg)): _blob(f) for f in pkg.rglob("*") if f.is_file() and "__pycache__" not in f.parts}


def run(monkeypatch, local=PIN, attested_commit=PIN, **kw):
    _approve_pin(monkeypatch, local=local)
    fake = world(**kw)
    monkeypatch.setattr(ship_check, "sh", fake)
    monkeypatch.setattr(ship_check, "attested_status", _attest_from(fake, attested_commit))
    import sys
    return ship_check.main("fulcra", HEAD, git=sys.executable, fulcra_api=sys.executable)     # stated roots; sh is faked above


def test_the_answering_process_reporting_another_build_refuses(monkeypatch, capsys):
    assert run(monkeypatch, attested_commit="8d0ed90e000185ca9fc71bc3a95983869d120bbf") == 1
    assert "process that answered reports build" in capsys.readouterr().out


def test_a_pth_or_sitecustomize_shadow_wins_under_site_and_loses_under_the_attestation(tmp_path, monkeypatch):
    """codex-coder round 17: env scrubbing does not stop startup hooks INSIDE the launcher environment.
    Build one with an approved dist-info AND a .pth AND a sitecustomize that prepend a shadow tree."""
    import subprocess, sys
    launcher = _tool_env(tmp_path, APPROVED_CLI)
    site = _site_of(launcher)
    (site / "coord_engine" / "__init__.py").write_text("WHO = 'APPROVED'\n")
    (site / "coord_engine-2.0.6.dist-info" / "RECORD").write_text("\n".join(_record_line(site, r) for r in ("coord_engine/__init__.py", "coord_engine/cli.py")) + "\n")
    shadow = tmp_path / "shadow"; (shadow / "coord_engine").mkdir(parents=True)
    (shadow / "coord_engine" / "__init__.py").write_text("WHO = 'SHADOW'\n")
    (site / "zzz_shadow.pth").write_text(f"import sys; sys.path.insert(0, {str(shadow)!r})\n")
    (site / "sitecustomize.py").write_text(f"import sys; sys.path.insert(0, {str(shadow)!r})\n")
    # the hole: a normal, site-enabled start of the SAME interpreter with this site-packages
    hole = subprocess.run([sys.executable, "-c", f"import site; site.addsitedir({str(site)!r}); import coord_engine; print(coord_engine.WHO)"],
                          capture_output=True, text=True, env=ship_check.engine_env()).stdout.strip()
    assert hole == "SHADOW"
    # the fix: the attestation under -I -S, site-packages NEVER on sys.path, the package reachable only through VerifiedImporter, bytes bound to the pinned tree
    _pin_tree(monkeypatch, launcher)
    ok, commit, status = ship_check.attested_status(str(launcher), "fulcra", f"coord-fold-ship-{HEAD}", PIN)
    assert ok and commit == PIN and status["state"] == "APPROVED"


def _record_line(site, rel):
    import base64, hashlib
    data = (site / rel).read_bytes()
    return f"{rel},sha256={base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b'=').decode()},{len(data)}"


def _tool_env(tmp_path, cli_body):
    """A minimal uv-style tool environment: bin/python -> this interpreter, one fake coord_engine, an approved
    dist-info WITH a RECORD binding the package bytes (r22)."""
    import sys
    env = tmp_path / "coord-engine"; (env / "bin").mkdir(parents=True); (env / "bin" / "python").symlink_to(sys.executable)
    launcher = env / "bin" / "coord-engine"; launcher.write_text("#!/bin/sh\n"); launcher.chmod(0o755)
    site = env / "lib" / "python3.13" / "site-packages"; (site / "coord_engine").mkdir(parents=True)
    (site / "coord_engine" / "__init__.py").write_text("")
    (site / "coord_engine" / "cli.py").write_text(cli_body)
    di = site / "coord_engine-2.0.6.dist-info"; di.mkdir()
    (di / "METADATA").write_text("Metadata-Version: 2.1\nName: coord-engine\nVersion: 2.0.6\n")
    (di / "direct_url.json").write_text(json.dumps({"vcs_info": {"commit_id": PIN}}))
    (di / "RECORD").write_text("\n".join(_record_line(site, r) for r in ("coord_engine/__init__.py", "coord_engine/cli.py")) + "\n")
    return launcher


APPROVED_CLI = "import json\ndef main(argv):\n    print(json.dumps({'state': 'APPROVED', 'head': 'x', 'approvals': ['codex-reviewer', 'codex-coder'], 'winning': {}}))\n    return 0\n"


def _site_of(launcher):
    return launcher.parent.parent / "lib" / "python3.13" / "site-packages"


def _pin_tree(monkeypatch, launcher):
    """The pinned commit's tree is what the distribution installed — modelled from the env BEFORE any tampering."""
    tree = _tree_of(_site_of(launcher))
    monkeypatch.setattr(ship_check, "pinned_tree", lambda pin: dict(tree))
    return tree


def test_replacing_cli_and_regenerating_its_record_row_is_refused_by_the_pinned_tree(tmp_path, monkeypatch):
    """codex-reviewer round 21: RECORD and direct_url are mutable; regenerate the row and r23 passed. The pinned
    commit's tree cannot be regenerated from inside the environment."""
    launcher = _tool_env(tmp_path, "def main(argv):\n    print('{}')\n    return 3\n")
    _pin_tree(monkeypatch, launcher)
    site = _site_of(launcher)
    (site / "coord_engine" / "cli.py").write_text(APPROVED_CLI)                                     # replaced...
    (site / "coord_engine-2.0.6.dist-info" / "RECORD").write_text("\n".join(_record_line(site, r) for r in ("coord_engine/__init__.py", "coord_engine/cli.py")) + "\n")   # ...and RECORD regenerated to match
    ok, detail, _ = ship_check.attested_status(str(launcher), "fulcra", f"coord-fold-ship-{HEAD}", PIN)
    assert not ok and "does not match the pinned commit's blob" in detail


def test_an_extra_or_missing_file_versus_the_pinned_tree_is_refused(tmp_path, monkeypatch):
    launcher = _tool_env(tmp_path, APPROVED_CLI)
    _pin_tree(monkeypatch, launcher)
    site = _site_of(launcher)
    (site / "coord_engine" / "extra.py").write_text("")
    ok, detail, _ = ship_check.attested_status(str(launcher), "fulcra", f"coord-fold-ship-{HEAD}", PIN)
    assert not ok and "does not contain" in detail
    (site / "coord_engine" / "extra.py").unlink(); (site / "coord_engine" / "cli.py").unlink()
    ok, detail, _ = ship_check.attested_status(str(launcher), "fulcra", f"coord-fold-ship-{HEAD}", PIN)
    assert not ok and "missing from the installed package" in detail


def test_duplicate_dist_info_is_refused(tmp_path, monkeypatch):
    launcher = _tool_env(tmp_path, APPROVED_CLI)
    _pin_tree(monkeypatch, launcher)
    (_site_of(launcher) / "coord_engine-2.0.5.dist-info").mkdir()
    ok, detail, _ = ship_check.attested_status(str(launcher), "fulcra", f"coord-fold-ship-{HEAD}", PIN)
    assert not ok and "exactly one is required" in detail


FORGING_WRAPPER = """#!/bin/sh
# a substituted tool-env interpreter: ignores every flag, reads the expected-tree path from argv, forges the payload
TREE=$(for a in "$@"; do :; done; echo "$a")
N=$(python3 -c "import json,sys; print(len(json.load(open(sys.argv[1]))))" "$TREE" 2>/dev/null || echo 2)
SITE=$(dirname "$0")/../lib/python3.13/site-packages
printf '{"file": "%s/coord_engine/__init__.py", "reported_commit": "forged", "tree_verified": %s, "dist_info": "x", "rc": 0, "status": {"state": "APPROVED", "head": "x", "approvals": ["codex-reviewer", "codex-coder"], "winning": {}}}\n' "$(cd "$SITE" && pwd -P)" "$N"
exit 0
"""


def test_a_forging_tool_env_interpreter_is_never_run_by_the_gate(tmp_path, monkeypatch):
    """codex-reviewer round 22: the verifier was <tool-env>/bin/python — part of the mutable environment.
    Install a forging wrapper there: run directly it prints a perfect payload (the hole, asserted); the gate
    runs its OWN interpreter instead, so an intact package still attests and a tampered one still refuses."""
    import subprocess
    launcher = _tool_env(tmp_path, "def main(argv):\n    print('{}')\n    return 3\n")          # installed: refuses (rc 3)
    _pin_tree(monkeypatch, launcher)
    wrapper = launcher.parent / "python"; wrapper.unlink(); wrapper.write_text(FORGING_WRAPPER); wrapper.chmod(0o755)
    forged = subprocess.run([str(wrapper), "-I", "-S", "-c", "x", str(_site_of(launcher)), "fulcra", "slug", "/dev/null"], capture_output=True, text=True).stdout
    assert '"state": "APPROVED"' in forged and '"rc": 0' in forged                                   # the wrapper forges when RUN
    ok, detail, _ = ship_check.attested_status(str(launcher), "fulcra", f"coord-fold-ship-{HEAD}", PIN)
    assert not ok and "rc 3" in detail                                                              # the gate ran its own interpreter: the real source answered rc 3
    (_site_of(launcher) / "coord_engine" / "cli.py").write_text(APPROVED_CLI)                       # tamper the bytes too
    ok, detail, _ = ship_check.attested_status(str(launcher), "fulcra", f"coord-fold-ship-{HEAD}", PIN)
    assert not ok and "does not match the pinned commit's blob" in detail                          # and the tree binding still refuses


def test_the_gate_runtime_is_the_process_interpreter():
    import sys
    assert ship_check.gate_python() == sys.executable


RC3_CLI = "def main(argv):\n    print('{}')\n    return 3\n"
TOCTOU_DRIVER = r"""
import sys, json
site, team, slug, tree_file, attest_file, replace_path, forged_file, mode = sys.argv[1:9]
ns = {"__name__": "attest_lib"}; exec(open(attest_file).read(), ns)      # the attestation as a library; main() does not run
pkg, blobs = ns["verify_tree"](site, json.load(open(tree_file)))         # phase 1: verified
if replace_path != "-":
    open(replace_path, "w").write(open(forged_file).read())              # THE REPLACEMENT: after verification, before import (synchronized, not raced)
if mode == "verified":
    ns["install_verified_importer"](pkg, blobs)                          # r26/r27: site-packages is NOT put on sys.path
else:
    sys.path.insert(0, site)                                             # r25/r26 behaviour: the path importer resolves against the tool environment
rc, status, file = ns["run_status"](team, slug)
print(json.dumps({"rc": rc, "state": status and status.get("state"), "file": file, "out": status and status.get("out")}))
"""


def _driver_run(tmp_path, launcher, tree, replace_path, forged_text, mode, restore=None):
    import subprocess, sys
    site = _site_of(launcher)
    tree_file = tmp_path / "tree.json"; tree_file.write_text(json.dumps(tree))
    attest_file = tmp_path / "attest.py"; attest_file.write_text(ship_check.ATTEST)
    forged = tmp_path / "forged.txt"; forged.write_text(forged_text)
    driver = tmp_path / "driver.py"; driver.write_text(TOCTOU_DRIVER)
    for path, text in (restore or {}).items():
        path.write_text(text)                                                          # restore the verified bytes before each run
    p = subprocess.run([sys.executable, "-I", "-S", "-B", str(driver), str(site), "fulcra", "slug", str(tree_file),
                        str(attest_file), str(replace_path) if replace_path else "-", str(forged), mode],
                       capture_output=True, text=True, env=ship_check.engine_env())
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout.splitlines()[-1])


def test_a_replacement_between_verification_and_import_executes_only_the_verified_bytes(tmp_path, monkeypatch):
    """codex-reviewer round 23 (P0, TOCTOU): r25 hashed pathnames and let SourceFileLoader reopen them.
    Synchronized, not raced: verify, then replace cli.py with an APPROVED forgery, then import. The path
    importer executes the forgery (positive control: the hole is real); the verified importer executes the
    bytes that were hashed, so the rc-3 source answers and the forgery on disk is never read."""
    import os
    launcher = _tool_env(tmp_path, RC3_CLI)
    tree = _pin_tree(monkeypatch, launcher)
    site = _site_of(launcher); cli_path = site / "coord_engine" / "cli.py"
    hole = _driver_run(tmp_path, launcher, tree, cli_path, APPROVED_CLI, "path", restore={cli_path: RC3_CLI})
    assert hole["rc"] == 0 and hole["state"] == "APPROVED"                              # the r25 importer executed the replacement
    closed = _driver_run(tmp_path, launcher, tree, cli_path, APPROVED_CLI, "verified", restore={cli_path: RC3_CLI})
    assert closed["rc"] == 3 and closed["state"] != "APPROVED"                         # r26: the verified bytes answered
    assert closed["file"].startswith(str(site.resolve()) + os.sep)                     # reported path is the site path (reporting only)


FORGED_ARGPARSE = """# a forged top-level module in the tool environment's site-packages: answers for the whole attestation
import sys, json
print(json.dumps({"file": sys.argv[1] + "/coord_engine/__init__.py", "reported_commit": "forged", "tree_verified": 2, "dist_info": "x",
                  "loader": "verified-bytes", "memory_loaded": 1, "rc": 0,
                  "status": {"state": "APPROVED", "head": "x", "approvals": ["codex-reviewer", "codex-coder"], "winning": {}}}))
sys.exit(0)
"""
ARGPARSE_CLI = "import argparse\ndef main(argv):\n    print('{}')\n    return 3\n"


def test_a_forged_top_level_module_in_the_tool_environment_is_never_executed(tmp_path, monkeypatch):
    """codex-coder round 24 (P0): r26 put site-packages on sys.path 'for metadata lookups'; a planted argparse.py
    there ran on the pinned cli's first stdlib import and printed a perfect verified-bytes APPROVED payload.
    Positive control under the r26 path insertion: the forgery answers and exits 0. Fixed attestation: the
    directory is never on sys.path, so the stdlib argparse loads and the verified rc-3 source answers."""
    launcher = _tool_env(tmp_path, ARGPARSE_CLI)
    tree = _pin_tree(monkeypatch, launcher)
    site = _site_of(launcher); (site / "argparse.py").write_text(FORGED_ARGPARSE)     # outside coord_engine/: the tree binding cannot see it
    hole = _driver_run(tmp_path, launcher, tree, None, "", "path")
    assert hole.get("loader") == "verified-bytes" and hole["status"]["state"] == "APPROVED" and hole["rc"] == 0   # the forgery answered
    closed = _driver_run(tmp_path, launcher, tree, None, "", "verified")
    assert closed["rc"] == 3 and closed["state"] != "APPROVED" and "loader" not in closed                          # stdlib argparse; verified cli answered


RESOURCE_CLI = ("from importlib.resources import files\n"
                "def main(argv):\n    import json\n    print(json.dumps({'out': files('coord_engine').joinpath('default_models.json').read_text()}))\n    return 3\n")


def test_package_resources_are_served_from_the_verified_bytes(tmp_path, monkeypatch):
    """Package DATA has the same TOCTOU as package code: the engine reads default_models.json through
    importlib.resources. Replace it on disk after verification: the path importer's reader returns the
    replacement (positive control); the verified importer's resource reader returns the hashed bytes."""
    launcher = _tool_env(tmp_path, RESOURCE_CLI)
    site = _site_of(launcher); res = site / "coord_engine" / "default_models.json"; res.write_text('{"verified": true}')
    tree = _pin_tree(monkeypatch, launcher)                                            # tree includes the resource
    hole = _driver_run(tmp_path, launcher, tree, res, '{"replaced": true}', "path", restore={res: '{"verified": true}'})
    assert json.loads(hole["out"]) == {"replaced": True}
    closed = _driver_run(tmp_path, launcher, tree, res, '{"replaced": true}', "verified", restore={res: '{"verified": true}'})
    assert json.loads(closed["out"]) == {"verified": True} and closed["rc"] == 3


def test_no_instruction_anywhere_puts_the_tool_environment_on_sys_path():
    """codex-coder round 25 (P0 plan contradiction): prose, docstring and a test comment still instructed the builder
    to insert the verified site-packages on sys.path, recreating the round-24 bypass with every gate green.
    Static: ATTEST mutates sys.path nowhere; ship_check's source and this file's source carry no such instruction;
    the attestation docstring names the only reachability path."""
    import ast, pathlib
    tree = ast.parse(ship_check.ATTEST)
    def is_sys_path(node):
        return isinstance(node, ast.Attribute) and node.attr == "path" and isinstance(node.value, ast.Name) and node.value.id == "sys"
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and is_sys_path(node.func.value):
            raise AssertionError(f"ATTEST mutates sys.path: sys.path.{node.func.attr}(...) at line {node.lineno}")
        if isinstance(node, (ast.Assign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            assert not any(is_sys_path(t) or (isinstance(t, ast.Subscript) and is_sys_path(t.value)) for t in targets), f"ATTEST assigns sys.path at line {node.lineno}"
    stale = ("exactly one path " + "on sys.path", "exactly the verified " + "site-packages", "inserts exactly " + "the verified")
    for src_path in (pathlib.Path(ship_check.__file__), pathlib.Path(__file__)):
        text = src_path.read_text()
        for phrase in stale:
            assert phrase not in text, f"{src_path.name} still carries the stale instruction {phrase!r}"
    assert "VerifiedImporter" in ship_check.attested_status.__doc__ and "sys.path.insert" not in pathlib.Path(ship_check.__file__).read_text()


def test_the_expected_tree_travels_on_stdin_and_its_digest_is_compared_exactly(monkeypatch, tmp_path):
    """codex-reviewer round 25 (P0): the tree was a mutable temp file the child trusted by name and the parent checked
    by COUNT. Now: the parent sends the canonical tree on stdin (no filename in argv) and refuses unless the child's
    echoed canonical digest equals its own."""
    import hashlib, subprocess, types
    launcher = _tool_env(tmp_path, APPROVED_CLI); tree = _pin_tree(monkeypatch, launcher); site = _site_of(launcher)
    calls = []
    real_run = subprocess.run
    def spy(cmd, **kw):
        calls.append((cmd, kw)); return real_run(cmd, **kw)
    monkeypatch.setattr(ship_check.subprocess, "run", spy)
    ok, commit, status = ship_check.attested_status(str(launcher), "fulcra", f"coord-fold-ship-{HEAD}", PIN)
    assert ok and status["state"] == "APPROVED"
    cmd, kw = calls[-1]
    assert cmd[cmd.index("-c") + 2:] == [str(site), "fulcra", f"coord-fold-ship-{HEAD}"]                # no tree filename in argv
    canonical = json.dumps(tree, sort_keys=True, separators=(",", ":"))
    assert kw.get("input") == canonical                                                                 # the tree went down the pipe
    # a child that echoes the right COUNT but a different digest (a substituted tree of equal size) is refused
    forged = json.dumps({"file": str(site / "coord_engine" / "__init__.py"), "reported_commit": PIN, "tree_verified": len(tree), "dist_info": "x",
                         "loader": "verified-bytes", "memory_loaded": 1, "tree_digest": hashlib.sha256(b"other").hexdigest(), "rc": 0,
                         "status": {"state": "APPROVED", "head": HEAD, "approvals": ["codex-reviewer", "codex-coder"]}})
    monkeypatch.setattr(ship_check.subprocess, "run", lambda cmd, **kw: types.SimpleNamespace(returncode=0, stdout=forged + "\n", stderr=""))
    ok, detail, _ = ship_check.attested_status(str(launcher), "fulcra", f"coord-fold-ship-{HEAD}", PIN)
    assert not ok and "canonical digest" in detail


def _shadow_path(tmp_path, names):
    d = tmp_path / "shadow-bin"; d.mkdir(exist_ok=True)
    for n in names:
        f = d / n; f.write_text("#!/bin/sh\necho FORGED-" + n + "\nexit 0\n"); f.chmod(0o755)
    return d


def test_trust_roots_are_stated_not_discovered_and_a_path_shadow_never_executes(tmp_path, monkeypatch):
    """codex-coder round 26 (P0): ship_check ran bare git and bare fulcra-api through PATH — the same PATH that finds
    the mutable launcher. Positive control: a shadow dir first on PATH hands out forgeries for both. Fixed: nothing
    stated -> refused (PATH never consulted); stated absolute paths -> resolved once; every call executes that path."""
    import os, shutil, subprocess
    shadow = _shadow_path(tmp_path, ("git", "fulcra-api"))
    monkeypatch.setenv("PATH", f"{shadow}{os.pathsep}{os.environ.get('PATH', '')}")
    for n in ("git", "fulcra-api"):
        assert shutil.which(n) == str(shadow / n)
        assert subprocess.run([shutil.which(n), "rev-parse"], capture_output=True, text=True).stdout.strip() == f"FORGED-{n}"
    table, why = ship_check.resolve_trust_roots({}, "/tool")
    assert table is None and "absolute path" in why
    trusted = tmp_path / "trusted"; trusted.mkdir()
    for n in ("git", "fulcra-api"):
        f = trusted / n; f.write_text("#!/bin/sh\necho TRUSTED-" + n + "\n"); f.chmod(0o755)
    table, why = ship_check.resolve_trust_roots({"git": str(trusted / "git"), "fulcra-api": str(trusted / "fulcra-api")}, "/tool")
    assert why is None and table == {n: os.path.realpath(trusted / n) for n in ("git", "fulcra-api")}
    monkeypatch.setattr(ship_check, "TRUSTED", dict(table))
    calls = []; real = subprocess.run
    monkeypatch.setattr(ship_check.subprocess, "run", lambda cmd, **kw: (calls.append(list(cmd)), real(cmd, **kw))[1])
    assert ship_check.sh("git", "rev-parse", "HEAD")[1] == "TRUSTED-git"
    assert ship_check.sh("fulcra-api", "file", "download", "x", "/dev/stdout")[1] == "TRUSTED-fulcra-api"
    OS_ROOTS = ("/bin/chmod", "/bin/ls")                                                       # r38: the ACL helpers call the OS's own chmod/ls by absolute path (OS trust roots, like the interpreter)
    assert [c[0] for c in calls if c[0] not in OS_ROOTS] == [table["git"], table["fulcra-api"]]  # every OTHER call is an absolute trusted path
    assert all(c[0].startswith("/") for c in calls)                                              # and nothing is ever resolved through PATH
    monkeypatch.setattr(ship_check, "TRUSTED", {})
    import pytest
    with pytest.raises(RuntimeError, match="bare name never executes"):
        ship_check.sh("git", "rev-parse", "HEAD")


def test_a_trust_root_under_the_tool_environment_is_refused(tmp_path):
    env = tmp_path / "coord-engine"; (env / "bin").mkdir(parents=True)
    g = env / "bin" / "git"; g.write_text("#!/bin/sh\n"); g.chmod(0o755)
    outside = tmp_path / "outside"; outside.mkdir(); link = outside / "git"; link.symlink_to(g)   # OUTSIDE, pointing INSIDE
    fa = tmp_path / "fulcra-api"; fa.write_text("#!/bin/sh\n"); fa.chmod(0o755)
    for stated in (str(g), str(link)):
        table, why = ship_check.resolve_trust_roots({"git": stated, "fulcra-api": str(fa)}, str(env))
        assert table is None and "under the tool environment" in why
    table, why = ship_check.resolve_trust_roots({"git": "git", "fulcra-api": str(fa)}, str(env))
    assert table is None and "absolute path" in why                                             # a bare name is not a statement
    table, why = ship_check.resolve_trust_roots({"git": str(tmp_path / "missing"), "fulcra-api": str(fa)}, str(env))
    assert table is None and "not an executable file" in why


def test_a_swap_after_resolution_does_not_change_what_executes(tmp_path, monkeypatch):
    import os
    a = tmp_path / "A"; a.write_text("#!/bin/sh\necho A\n"); a.chmod(0o755)
    b = tmp_path / "B"; b.write_text("#!/bin/sh\necho B\n"); b.chmod(0o755)
    link = tmp_path / "git"; link.symlink_to(a)
    fa = tmp_path / "fulcra-api"; fa.write_text("#!/bin/sh\n"); fa.chmod(0o755)
    table, why = ship_check.resolve_trust_roots({"git": str(link), "fulcra-api": str(fa)}, "/tool")
    assert why is None and table["git"] == os.path.realpath(a)
    monkeypatch.setattr(ship_check, "TRUSTED", dict(table))
    link.unlink(); link.symlink_to(b)                                                            # THE SWAP, after resolution
    monkeypatch.setenv("PATH", str(tmp_path))                                                   # and PATH now hands out B too
    assert ship_check.sh("git", "anything")[1] == "A"                                            # the once-resolved realpath executes


def test_the_attestation_child_can_reach_only_the_stated_fulcra_api(tmp_path, monkeypatch):
    import os, shutil
    shadow = _shadow_path(tmp_path, ("fulcra-api",))
    monkeypatch.setenv("PATH", f"{shadow}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("FULCRA_CLI_COMMAND", "evil"); monkeypatch.setenv("FULCRA_API_BASE", "https://evil.invalid"); monkeypatch.setenv("COORD_TRANSPORT_HTTP", "0")
    trusted = tmp_path / "trusted-fulcra-api"; trusted.write_text("#!/bin/sh\n"); trusted.chmod(0o755)
    assert shutil.which("fulcra-api") == str(shadow / "fulcra-api")                              # positive control: inherited PATH hands out the forgery
    env = ship_check.engine_env(str(trusted))
    entries = env["PATH"].split(os.pathsep)
    assert len(entries) == 1 and os.listdir(entries[0]) == []                                   # r34: NO link at all; PATH resolves nothing
    assert shutil.which("fulcra-api", path=env["PATH"]) is None and shutil.which("sh", path=env["PATH"]) is None
    import shlex
    assert shlex.split(env["FULCRA_CLI_COMMAND"]) == [os.path.realpath(trusted)]                # the engine is HANDED the absolute path (its transport shlex-splits)
    for k in ("FULCRA_API_BASE", "COORD_TRANSPORT_HTTP"):
        assert k not in env
    assert "FULCRA_CLI_COMMAND" not in ship_check.engine_env() and os.listdir(ship_check.engine_env()["PATH"]) == []   # nothing stated -> nothing reachable; inherited 'evil' scrubbed


def test_main_refuses_without_stated_trust_roots_before_touching_the_store(monkeypatch, capsys):
    _approve_pin(monkeypatch)
    touched = []
    monkeypatch.setattr(ship_check, "sh", lambda *a: (touched.append(a), (1, "", ""))[1])
    assert ship_check.main("fulcra", HEAD) == 1
    assert "never discovered through PATH" in capsys.readouterr().out and touched == []


REAL_CLI_REFUSAL = "Error: Invalid value for '[LOCAL_FILE]': Path '/dev/stdout' is not readable."
FAKE_FULCRA_API = """#!/bin/sh
# behaves like the real fulcra-api file download (measured 2026-09-05): refuses /dev/stdout, writes LOCAL_FILE
if [ "$1" != "file" ] || [ "$2" != "download" ]; then echo "unexpected: $*" >&2; exit 64; fi
if [ "$4" = "/dev/stdout" ]; then echo "REAL_CLI_REFUSAL" >&2; exit 2; fi
case "$3" in
  */adopt-latest.sh) printf '#!/bin/sh\nPIN="PIN_VALUE"   # coord-engine\n' > "$4" ;;
  *) printf 'verdict: approve\ntree: TREE_VALUE\n' > "$4" ;;
esac
"""


def test_store_reads_go_through_a_private_file_because_the_real_cli_refuses_dev_stdout(tmp_path, monkeypatch):
    """r30. Positive control: a fulcra-api that behaves like the real one refuses `download ... /dev/stdout` (rc 2) —
    which is what every revision before r30 did, unmeasured. The fix reads through a private temp file."""
    import os
    fa = tmp_path / "fulcra-api"; fa.write_text(FAKE_FULCRA_API.replace("REAL_CLI_REFUSAL", REAL_CLI_REFUSAL).replace("PIN_VALUE", PIN).replace("TREE_VALUE", TREE)); fa.chmod(0o755)
    g = tmp_path / "git"; g.write_text("#!/bin/sh\n"); g.chmod(0o755)
    table, why = ship_check.resolve_trust_roots({"git": str(g), "fulcra-api": str(fa)}, "/tool"); assert why is None
    monkeypatch.setattr(ship_check, "TRUSTED", dict(table))
    rc, out, err = ship_check.sh("fulcra-api", "file", "download", "team/fulcra/_coord/bus-v3/adopt-latest.sh", "/dev/stdout")
    assert rc == 2 and "not readable" in err and out == ""                                        # the hole: nothing ever came back
    assert ship_check.fleet_pin("fulcra") == PIN                                                   # the fix: read through a file
    rc, body, _ = ship_check.store_read("team/fulcra/review/x/verdicts/y.md")
    assert rc == 0 and f"tree: {TREE}" in body
    assert not [d for d in os.listdir(tempfile_dir()) if d.startswith("coord-fold-store-")]        # nothing left behind


def tempfile_dir():
    import tempfile
    return tempfile.gettempdir()


def test_the_real_cli_accepts_the_runbook_invocation_and_refuses_the_bare_form(tmp_path):
    """codex-coder round 27: Task 14/16 runbooks still invoked `ship_check.py fulcra HEAD` while argparse required
    --git/--fulcra-api, so the mandatory gate always exited at argument parsing. End-to-end through the REAL CLI, so a
    main()-level unit call cannot mask drift: the bare form exits 2 at parsing (positive control); the runbook form
    parses, resolves the stated roots, reads the fleet pin, and reaches the pin check (rc 1, 'not an APPROVED+PINNED'
    under the shipped empty approved set)."""
    import os, subprocess, sys
    fa = tmp_path / "fulcra-api"; fa.write_text(FAKE_FULCRA_API.replace("REAL_CLI_REFUSAL", REAL_CLI_REFUSAL).replace("PIN_VALUE", PIN).replace("TREE_VALUE", TREE)); fa.chmod(0o755)
    g = tmp_path / "git"; g.write_text("#!/bin/sh\necho " + HEAD + "\n"); g.chmod(0o755)
    launcher_dir = tmp_path / "env" / "bin"; launcher_dir.mkdir(parents=True); (launcher_dir / "coord-engine").write_text("#!/bin/sh\n"); (launcher_dir / "coord-engine").chmod(0o755)
    env = {**os.environ, "PATH": str(launcher_dir)}                                                # the launcher is discovered (env root = tmp/env); the roots are stated OUTSIDE it
    bare = subprocess.run([sys.executable, str(SCRIPT), "fulcra", HEAD], capture_output=True, text=True, env=env)
    assert bare.returncode == 2 and "--git" in bare.stderr and "required" in bare.stderr             # the old runbook form: dead at parsing
    run = subprocess.run([sys.executable, str(SCRIPT), "fulcra", HEAD, "--git", str(g), "--fulcra-api", str(fa)], capture_output=True, text=True, env=env)
    assert run.returncode == 1 and "not an APPROVED+PINNED" in run.stdout, (run.stdout, run.stderr)  # parsed, roots resolved, pin read, refused on the empty approved set


def test_a_same_count_tree_substitution_cannot_bind_tampered_bytes(tmp_path, monkeypatch):
    """Synchronized, not raced. Positive control (the r27 hole as a library flow): tamper cli.py, build the attacker's
    tree of the SAME SIZE from the tampered site, substitute it for the real tree file after the parent wrote it and
    before the child read it — verify_tree accepts and the count matches. Fixed gate: the real tree goes down the
    pipe, so the tampered bytes fail the blob check; the substituted file, wherever it is planted, is never read."""
    launcher = _tool_env(tmp_path, RC3_CLI); real_tree = _pin_tree(monkeypatch, launcher); site = _site_of(launcher)
    (site / "coord_engine" / "cli.py").write_text(APPROVED_CLI)                                        # tampered bytes
    attacker_tree = _tree_of(site)
    assert len(attacker_tree) == len(real_tree) and attacker_tree != real_tree
    tree_file = tmp_path / "attest.tree.json"; tree_file.write_text(json.dumps(real_tree))            # the parent's write
    tree_file.write_text(json.dumps(attacker_tree))                                                    # THE SUBSTITUTION, before the child's read
    ns = {"__name__": "attest_lib"}; exec(ship_check.ATTEST, ns)
    _, blobs = ns["verify_tree"](str(site), json.load(open(tree_file)))                                # r27 child: trusts the pathname
    assert len(blobs) == len(real_tree)                                                                # r27 parent: count matches -> accepted (the hole)
    ok, detail, _ = ship_check.attested_status(str(launcher), "fulcra", f"coord-fold-ship-{HEAD}", PIN)
    assert not ok and "does not match the pinned commit's blob" in detail                            # r28: the real tree came down the pipe


def test_the_attestation_refuses_when_the_tool_environment_is_on_sys_path(tmp_path, monkeypatch):
    launcher = _tool_env(tmp_path, RC3_CLI); _pin_tree(monkeypatch, launcher); site = _site_of(launcher)
    import subprocess, sys
    probe = tmp_path / "probe.py"; probe.write_text(
        "import sys\nsite = sys.argv[1]\nns = {'__name__': 'attest_lib'}; exec(open(sys.argv[2]).read(), ns)\n"
        "sys.path.insert(0, site)\nprint('under', ns['paths_under'](site) == [site])\n")
    attest_file = tmp_path / "attest.py"; attest_file.write_text(ship_check.ATTEST)
    p = subprocess.run([sys.executable, "-I", "-S", "-B", str(probe), str(site), str(attest_file)], capture_output=True, text=True)
    assert "under True" in p.stdout, p.stderr


def test_the_attestation_refuses_a_coord_engine_module_loaded_outside_the_verified_importer(tmp_path, monkeypatch):
    """The positive post-check: if any coord_engine* module came from another loader, the process refuses."""
    import subprocess, sys
    launcher = _tool_env(tmp_path, RC3_CLI); _pin_tree(monkeypatch, launcher); site = _site_of(launcher)
    probe = tmp_path / "probe.py"; probe.write_text(
        "import sys, json\nsite = sys.argv[1]\nns = {'__name__': 'attest_lib'}; exec(open(sys.argv[2]).read(), ns)\n"
        "sys.path.insert(0, site); import coord_engine\n"                                   # loaded by the path importer FIRST
        "try:\n    ns['install_verified_importer'](site + '/coord_engine', {})\nexcept SystemExit as e:\n    print('exit', e.code)\n")
    attest_file = tmp_path / "attest.py"; attest_file.write_text(ship_check.ATTEST)
    p = subprocess.run([sys.executable, "-I", "-S", "-B", str(probe), str(site), str(attest_file)], capture_output=True, text=True)
    assert "already imported before the verified importer" in p.stdout and "exit 2" in p.stdout


def test_a_pin_not_in_the_clone_refuses(tmp_path, monkeypatch):
    launcher = _tool_env(tmp_path, APPROVED_CLI)
    monkeypatch.setattr(ship_check, "pinned_tree", lambda pin: None)
    ok, detail, _ = ship_check.attested_status(str(launcher), "fulcra", f"coord-fold-ship-{HEAD}", PIN)
    assert not ok and "not in this clone" in detail


def test_pinned_tree_reads_the_commit_from_a_real_git_clone_and_the_intact_package_attests(tmp_path, monkeypatch):
    """The positive path through real git: commit the package, read its tree, verify the installed copy."""
    import subprocess, os
    launcher = _tool_env(tmp_path, APPROVED_CLI)
    site = _site_of(launcher)
    repo = tmp_path / "repo"; (repo / "packages" / "coord-engine").mkdir(parents=True)
    import shutil as _sh
    _sh.copytree(site / "coord_engine", repo / "packages" / "coord-engine" / "coord_engine")
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t.invalid", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t.invalid"}
    for cmd in (["git", "init", "-q"], ["git", "add", "-A"], ["git", "commit", "-q", "-m", "pin"]):
        subprocess.run(cmd, cwd=repo, check=True, env=env, capture_output=True)
    pin = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()
    monkeypatch.chdir(repo)
    import shutil, sys
    monkeypatch.setattr(ship_check, "TRUSTED", {"git": os.path.realpath(shutil.which("git")), "fulcra-api": sys.executable})   # r29: stated by the test, as the operator would
    tree = ship_check.pinned_tree(pin)
    assert tree == _tree_of(site)
    ok, commit, status = ship_check.attested_status(str(launcher), "fulcra", f"coord-fold-ship-{HEAD}", pin)
    assert ok and commit == pin and status["state"] == "APPROVED"


def test_a_replaced_cli_with_any_record_content_is_refused_by_the_pinned_tree(tmp_path, monkeypatch):
    """Round 20's hashless-row case and round 21's regenerated-row case are the same case under r24: RECORD is
    not consulted; the pinned tree is."""
    launcher = _tool_env(tmp_path, "def main(argv):\n    print('{}')\n    return 3\n")
    _pin_tree(monkeypatch, launcher)
    site = _site_of(launcher)
    (site / "coord_engine" / "cli.py").write_text(APPROVED_CLI)
    (site / "coord_engine-2.0.6.dist-info" / "RECORD").write_text(_record_line(site, "coord_engine/__init__.py") + "\ncoord_engine/cli.py,,\n")
    ok, detail, _ = ship_check.attested_status(str(launcher), "fulcra", f"coord-fold-ship-{HEAD}", PIN)
    assert not ok and "does not match the pinned commit's blob" in detail


def test_stale_unchecked_hash_bytecode_answers_under_a_normal_import_and_never_under_the_attestation(tmp_path, monkeypatch):
    """codex-coder, round 20: __pycache__ was excluded from the check, and an unchecked-hash .pyc is executed
    without consulting the source. The verified source returns rc 3; a stale pyc compiled from APPROVED_CLI sits
    beside it. Normal import: the pyc answers APPROVED. Attestation (-B, fresh pycache_prefix): the source answers."""
    import importlib.util, py_compile, subprocess, sys
    launcher = _tool_env(tmp_path, "def main(argv):\n    print('{\"state\": \"APPROVED\", \"head\": \"x\", \"approvals\": [], \"winning\": {}}')\n    return 3\n")   # what RECORD verifies
    site = _site_of(launcher)
    stale_src = tmp_path / "stale_cli.py"; stale_src.write_text(APPROVED_CLI)
    pyc = pathlib.Path(importlib.util.cache_from_source(str(site / "coord_engine" / "cli.py")))
    pyc.parent.mkdir(parents=True, exist_ok=True)
    py_compile.compile(str(stale_src), cfile=str(pyc), dfile=str(site / "coord_engine" / "cli.py"), invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH)
    hole = subprocess.run([sys.executable, "-S", "-c", f"import site; site.addsitedir({str(site)!r}); from coord_engine import cli; print(cli.main(['x','x','x','x']))"],
                          capture_output=True, text=True, env=ship_check.engine_env()).stdout.strip().splitlines()
    assert hole and hole[-1] == "0", hole                              # the stale bytecode answered rc 0 (the hole, asserted)
    _pin_tree(monkeypatch, launcher)
    ok, detail, _ = ship_check.attested_status(str(launcher), "fulcra", f"coord-fold-ship-{HEAD}", PIN)
    assert not ok and "rc 3" in detail                                 # the verified SOURCE answered (rc 3) — bytecode never consulted


def test_a_sourceless_pyc_under_the_package_is_refused(tmp_path, monkeypatch):
    launcher = _tool_env(tmp_path, APPROVED_CLI)
    _pin_tree(monkeypatch, launcher)
    (_site_of(launcher) / "coord_engine" / "helper.pyc").write_bytes(b"\x00")
    ok, detail, _ = ship_check.attested_status(str(launcher), "fulcra", f"coord-fold-ship-{HEAD}", PIN)
    assert not ok and ("could answer" in detail or "does not contain" in detail)


def test_a_recorded_intact_package_attests_and_answers(tmp_path, monkeypatch):
    launcher = _tool_env(tmp_path, APPROVED_CLI)
    _pin_tree(monkeypatch, launcher)
    ok, commit, status = ship_check.attested_status(str(launcher), "fulcra", f"coord-fold-ship-{HEAD}", PIN)
    assert ok and commit == PIN and status["state"] == "APPROVED"


def test_an_approved_shaped_status_that_returns_rc_3_is_refused(tmp_path, monkeypatch):
    """Both reviewers, round 18: the inner verdict's rc was recorded and never checked."""
    launcher = _tool_env(tmp_path, "import json\ndef main(argv):\n    print(json.dumps({'state': 'APPROVED', 'head': 'x', 'approvals': ['codex-reviewer', 'codex-coder'], 'winning': {}}))\n    return 3\n")
    _pin_tree(monkeypatch, launcher)
    ok, detail, status = ship_check.attested_status(str(launcher), "fulcra", f"coord-fold-ship-{HEAD}", PIN)
    assert not ok and "rc 3" in detail and status is None


def test_a_status_of_the_wrong_shape_is_refused(tmp_path, monkeypatch):
    launcher = _tool_env(tmp_path, "def main(argv):\n    print('[1, 2]')\n    return 0\n")
    _pin_tree(monkeypatch, launcher)
    ok, detail, _ = ship_check.attested_status(str(launcher), "fulcra", f"coord-fold-ship-{HEAD}", PIN)
    assert not ok and "expected shape" in detail


def test_the_attestation_refuses_a_module_answering_from_outside_the_verified_site(monkeypatch, tmp_path):
    import subprocess, sys
    env = tmp_path / "coord-engine"; (env / "bin").mkdir(parents=True); (env / "bin" / "python").symlink_to(sys.executable)
    launcher = env / "bin" / "coord-engine"; launcher.write_text("#!/bin/sh\n"); launcher.chmod(0o755)
    site = env / "lib" / "python3.13" / "site-packages"; (site / "coord_engine-2.0.6.dist-info").mkdir(parents=True)
    (site / "coord_engine-2.0.6.dist-info" / "METADATA").write_text("Metadata-Version: 2.1\nName: coord-engine\nVersion: 2.0.6\n")
    (site / "coord_engine-2.0.6.dist-info" / "direct_url.json").write_text(json.dumps({"vcs_info": {"commit_id": PIN}}))
    (site / "coord_engine-2.0.6.dist-info" / "RECORD").write_text("coord_engine-2.0.6.dist-info/METADATA,,\n")
    elsewhere = tmp_path / "elsewhere"; (elsewhere / "coord_engine").mkdir(parents=True)
    (elsewhere / "coord_engine" / "__init__.py").write_text("")
    (elsewhere / "coord_engine" / "cli.py").write_text("def main(argv):\n    print('{}')\n    return 0\n")
    # no coord_engine package under site; a matching tree exists ELSEWHERE. r28 (codex-coder round 25): the r27 form of
    # this test monkeypatched a sys.path.insert that no longer existed and passed vacuously. Now: the pinned tree is the
    # elsewhere tree; nothing under site can satisfy it, and nothing elsewhere can answer (no sys.path, no importer entry).
    monkeypatch.setattr(ship_check, "pinned_tree", lambda pin: _tree_of(elsewhere))
    ok, detail, _ = ship_check.attested_status(str(launcher), "fulcra", f"coord-fold-ship-{HEAD}", PIN)
    assert not ok and "missing from the installed package" in detail


def test_the_executable_is_resolved_exactly_once_and_that_path_is_what_executes(monkeypatch):
    """Both reviewers, round 16 (TOCTOU): `which` answers approved A first and stale B afterwards.
    The gate must resolve once and execute A; B must never be consumed."""
    answers = iter(["/tool-A/bin/coord-engine", "/tool-B/bin/coord-engine"])
    resolutions = []
    def which(name):
        r = next(answers); resolutions.append(r); return r
    monkeypatch.setattr(ship_check.shutil, "which", which)
    monkeypatch.setattr(ship_check.os.path, "realpath", lambda p: p)
    monkeypatch.setattr(ship_check, "APPROVED_ENGINE_PINS", frozenset({PIN}))
    identity_reads, executed = [], []
    def commit(exe):
        identity_reads.append(exe); return PIN
    monkeypatch.setattr(ship_check, "executing_engine_commit", commit)
    fake = world()
    monkeypatch.setattr(ship_check, "sh", fake)
    def attested(exe, team, slug, pin):
        executed.append(exe); rc, out, _ = fake("coord-engine", "review", "status", team, slug, "--json"); return True, PIN, json.loads(out)
    monkeypatch.setattr(ship_check, "attested_status", attested)
    import sys
    assert ship_check.main("fulcra", HEAD, git=sys.executable, fulcra_api=sys.executable) == 0
    assert resolutions == ["/tool-A/bin/coord-engine"]                      # exactly one resolution
    assert identity_reads == ["/tool-A/bin/coord-engine"] and executed == ["/tool-A/bin/coord-engine"]   # A verified, A executed, B never touched


def test_remote_pin_approved_but_the_executing_engine_is_another_build_refuses(monkeypatch, capsys):
    """codex-reviewer round 14: a lagging host names the approved pin while PATH still runs an older engine."""
    assert run(monkeypatch, local="8d0ed90e000185ca9fc71bc3a95983869d120bbf") == 1
    assert "not the approved pin" in capsys.readouterr().out


def test_an_unprovable_executing_engine_refuses(monkeypatch):
    assert run(monkeypatch, local=None) == 1


def test_engine_env_strips_every_import_affecting_variable(monkeypatch):
    for k in ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PYTHONWARNINGS", "VIRTUAL_ENV", "CONDA_PREFIX"):
        monkeypatch.setenv(k, "x")
    env = ship_check.engine_env()
    assert not any(k.startswith("PYTHON") for k in env if k != "PYTHONNOUSERSITE")
    assert env["PYTHONNOUSERSITE"] == "1" and "VIRTUAL_ENV" not in env and "CONDA_PREFIX" not in env


def test_a_shadow_coord_engine_on_pythonpath_is_not_imported_under_the_scrubbed_env(tmp_path, monkeypatch):
    """coord-boss's reproduction, as a test: same launcher, same interpreter — PYTHONPATH decides which
    coord_engine answers. (-S on the child: a uv workspace venv has the REAL coord_engine installed and it would
    answer first; PYTHONPATH is still honoured under -S, so the shadow still wins when inherited.) Original text:
    coord_engine answers. Under ship_check's env the site-packages build (the one the identity check proved) wins."""
    import subprocess, sys
    site = tmp_path / "env" / "lib" / "python3.13" / "site-packages"; (site / "coord_engine").mkdir(parents=True)
    (site / "coord_engine" / "__init__.py").write_text("WHO = 'PINNED'\n")
    shadow = tmp_path / "shadow"; (shadow / "coord_engine").mkdir(parents=True)
    (shadow / "coord_engine" / "__init__.py").write_text("WHO = 'SHADOW'\n")
    launcher = tmp_path / "env" / "bin" / "coord-engine"; launcher.parent.mkdir(parents=True)
    launcher.write_text(f"#!{sys.executable} -S\nimport sys, site\nsite.addsitedir({str(site)!r})\nimport coord_engine\nprint(coord_engine.WHO)\n"); launcher.chmod(0o755)
    monkeypatch.setenv("PYTHONPATH", str(shadow))
    inherited = subprocess.run([str(launcher)], capture_output=True, text=True).stdout.strip()
    assert inherited == "SHADOW"                                                    # the hole, reproduced
    fixed = subprocess.run([str(launcher)], capture_output=True, text=True, env=ship_check.engine_env()).stdout.strip()
    assert fixed == "PINNED"                                                        # the env fix (necessary, not sufficient: see the .pth test)


def test_executing_engine_commit_reads_direct_url_beside_the_dist_info(tmp_path, monkeypatch):
    env = tmp_path / "coord-engine"; (env / "bin").mkdir(parents=True)
    exe = env / "bin" / "coord-engine"; exe.write_text("#!/bin/sh\n"); exe.chmod(0o755)
    di = env / "lib" / "python3.13" / "site-packages" / "coord_engine-2.0.6.dist-info"; di.mkdir(parents=True)
    (di / "direct_url.json").write_text(json.dumps({"url": "https://github.com/ashfulcra/fulcra-tools", "vcs_info": {"vcs": "git", "commit_id": PIN}, "subdirectory": "packages/coord-engine"}))
    assert ship_check.executing_engine_commit(str(exe)) == PIN
    (di / "direct_url.json").unlink()
    assert ship_check.executing_engine_commit(str(exe)) is None


def test_the_shipped_approved_set_is_exactly_the_adopted_fleet_pin_and_any_other_pin_refuses(monkeypatch, capsys):
    """r38: the fleet pin moved to e06e69e5 (PR #698 + store upload, 2026-09-05), the build that carries the approved
    supersession contract (#695). The approved set names exactly it; a fleet pin outside it still refuses."""
    assert ship_check.APPROVED_ENGINE_PINS == frozenset({"e06e69e5d44d92b2b52a09020f53f2bd1ccdc1d5"})
    monkeypatch.setattr(ship_check, "sh", world(pin="0" * 40))
    monkeypatch.setattr(ship_check, "engine_executable", lambda: "/tool/bin/coord-engine")
    import sys
    assert ship_check.main("fulcra", HEAD, git=sys.executable, fulcra_api=sys.executable) == 1 and "not an APPROVED+PINNED" in capsys.readouterr().out
def test_a_pin_outside_the_approved_set_refuses(monkeypatch):
    assert run(monkeypatch, pin="e" * 40) == 1


def test_a_missing_adopt_latest_refuses(monkeypatch):
    assert run(monkeypatch, pin=None) == 1


def test_the_plain_exact_head_form_is_an_accepted_winning_name(monkeypatch):
    plain = {r: f"{HEAD}--{r}.md" for r in ENV}
    winning = {r: {"name": n, "verdict": "approve", "sort_key": "x"} for r, n in plain.items()}
    assert run(monkeypatch, winning=winning, bodies={n: APPROVE for n in plain.values()}) == 0


def test_both_exact_head_and_tree_approvals_pass(monkeypatch, capsys):
    assert run(monkeypatch) == 0 and "OK" in capsys.readouterr().out


def test_same_second_earlier_approve_with_larger_digest_does_not_beat_the_winning_changes(monkeypatch, capsys):
    """The round-14 hole. The fold (engine) kept the later CHANGES (digest 058ddb93) and says so in
    `winning`; the earlier APPROVE (feb86aee) exists too. The script must read winning, never max(name)."""
    later_changes = f"{HEAD}--codex-coder--2026-09-05T01:32:10Z-058ddb93.md"
    earlier_approve = f"{HEAD}--codex-coder--2026-09-05T01:32:10Z-feb86aee.md"
    winning = {"codex-reviewer": {"name": ENV["codex-reviewer"], "verdict": "approve", "sort_key": "2026-09-05T01:00:24.000000Z"},
               "codex-coder": {"name": later_changes, "verdict": "changes", "sort_key": "2026-09-05T01:32:10.900000Z"}}
    bodies = {ENV["codex-reviewer"]: APPROVE, later_changes: f"verdict: changes\ntree: {TREE}", earlier_approve: APPROVE}
    assert run(monkeypatch, winning=winning, bodies=bodies, state="CHANGES", approvals=("codex-reviewer",)) == 1
    assert "058ddb93" in capsys.readouterr().out


def test_engine_without_winning_is_unknown_and_refuses(monkeypatch, capsys):
    assert run(monkeypatch, winning="absent") == 1 and "does not expose `winning`" in capsys.readouterr().out


def test_winning_shard_on_another_head_refuses(monkeypatch):
    winning = {r: {"name": n.replace(HEAD, OTHER), "verdict": "approve", "sort_key": "x"} for r, n in ENV.items()}
    assert run(monkeypatch, winning=winning, bodies={w["name"]: APPROVE for w in winning.values()}, fold_head=OTHER) == 1


def test_changes_verdict_refuses(monkeypatch):
    winning = {"codex-reviewer": {"name": ENV["codex-reviewer"], "verdict": "approve", "sort_key": "x"},
               "codex-coder": {"name": ENV["codex-coder"], "verdict": "changes", "sort_key": "x"}}
    bodies = {ENV["codex-reviewer"]: APPROVE, ENV["codex-coder"]: f"verdict: changes\ntree: {TREE}"}
    assert run(monkeypatch, winning=winning, bodies=bodies, state="CHANGES", approvals=("codex-reviewer",)) == 1


def test_missing_reviewer_refuses(monkeypatch):
    winning = {"codex-reviewer": {"name": ENV["codex-reviewer"], "verdict": "approve", "sort_key": "x"}}
    assert run(monkeypatch, winning=winning, approvals=("codex-reviewer",), state="PENDING") == 1


def test_dirty_package_refuses(monkeypatch):
    assert run(monkeypatch, dirty=" M packages/coord-fold/coord_fold/fold.py") == 1


def test_tree_mismatch_refuses(monkeypatch):
    bodies = {n: "verdict: approve\ntree: 2222222222222222222222222222222222222222" for n in ENV.values()}
    assert run(monkeypatch, bodies=bodies) == 1


def test_winning_says_approve_but_the_shard_body_does_not_refuses(monkeypatch):
    bodies = {ENV["codex-reviewer"]: APPROVE, ENV["codex-coder"]: f"verdict: changes\ntree: {TREE}"}
    assert run(monkeypatch, bodies=bodies) == 1


def test_no_repo_prose_invokes_ship_check_without_stated_trust_roots():
    """codex-coder rounds 27/28: the prose contract drifted from argparse twice (Task 14, then Task 16). Every
    invocation written in repo prose — AGENTS.md, the package README, the script's own Usage — must carry the
    stated trust roots on the same line. (The plan document itself is checked by Task 0's materialize_plan.)"""
    import importlib.util
    mp_spec = importlib.util.spec_from_file_location("materialize_plan", SCRIPT.parent / "materialize_plan.py"); mp = importlib.util.module_from_spec(mp_spec); mp_spec.loader.exec_module(mp)
    bare = mp.bare_invocations                                                                    # ONE parser, shared with the plan gate (r34: the r33 regex checked only --git)
    here = pathlib.Path(__file__).resolve()
    files = [f for f in (here.parents[3] / "AGENTS.md", here.parents[1] / "README.md", SCRIPT) if f.exists()]   # a materialized plan tree has no repo AGENTS.md
    assert len(files) >= 2, files
    for f in files:
        for i, ln in enumerate(f.read_text().splitlines(), 1):
            assert not bare(ln), f"{f.name}:{i}: {bare(ln)}"
    assert bare("`scripts/ship_check.py fulcra <HEAD>` and fails closed")                          # the sentence that drifted
    assert bare("scripts/ship_check.py fulcra <HEAD> --git /usr/bin/git") == ["missing --fulcra-api: scripts/ship_check.py fulcra <HEAD> --git /usr/bin/git"]   # codex-coder round 29 (r39: the match carries the path)
    assert bare("scripts/ship_check.py fulcra <HEAD> --fulcra-api /x")[0].startswith("missing --git")
    assert bare("scripts/ship_check.py fulcra <HEAD> --git --fulcra-api /x")[0].startswith("--git has no value")                      # codex-coder round 30
    assert not bare("`scripts/ship_check.py fulcra <HEAD> --git /x --fulcra-api /y`")


def test_the_gate_temp_root_is_its_own_0700_directory_and_TMPDIR_is_ignored(tmp_path, monkeypatch):
    """codex-coder round 29: mkdtemp under an uncontrolled TMPDIR is a pathname handoff the gate did not own."""
    import os, stat, tempfile
    home = tmp_path / "home"; home.mkdir(); monkeypatch.setenv("HOME", str(home))
    world = tmp_path / "world"; world.mkdir(); world.chmod(0o777); monkeypatch.setenv("TMPDIR", str(world)); tempfile.tempdir = None
    root = ship_check.gate_tmp_root()
    assert root == str(home / ".local" / "state" / "coord-fold" / "tmp") and stat.S_IMODE(os.stat(root).st_mode) == 0o700
    d = ship_check.private_dir("x-"); assert d.startswith(root + os.sep) and not d.startswith(str(world))
    os.chmod(root, 0o755)
    import pytest
    with pytest.raises(RuntimeError, match="not a private directory"):
        ship_check.gate_tmp_root()                                                                  # a root that lost its privacy is refused, never reused
    os.chmod(root, 0o700); tempfile.tempdir = None


def test_store_read_refuses_a_body_whose_handoff_state_changed_before_the_read(tmp_path, monkeypatch):
    """Synchronized, not raced (codex-reviewer round 28): the fake CLI writes the body and then — before the gate reads —
    (a) makes the private dir world-readable, (b) replaces the body with a symlink, (c) keeps it intact. Only (c) is read."""
    import os, tempfile
    home = tmp_path / "home"; home.mkdir(); monkeypatch.setenv("HOME", str(home)); tempfile.tempdir = None
    g = tmp_path / "git"; g.write_text("#!/bin/sh\n"); g.chmod(0o755)
    secret = tmp_path / "secret.txt"; secret.write_text("PIN=\"deadbeef\"\n")
    def fake(mode):
        fa = tmp_path / f"fulcra-api-{mode}"
        # absolute tool paths: the gate hands the CLI an EMPTY PATH, so a bare `chmod` would silently not run (measured)
        body = {"chmod": 'printf "ok" > "$4"; /bin/chmod 755 "$(/usr/bin/dirname "$4")"', "link": f'printf "ok" > "$4"; /bin/rm "$4"; /bin/ln -s {secret} "$4"',
                "bodymode": 'printf "ok" > "$4"; /bin/chmod 666 "$4"',                                        # codex-reviewer round 31: the BODY left world-writable
                "intact": 'printf "PIN=x" > "$4"'}[mode]
        fa.write_text("#!/bin/sh\n" + body + "\nexit 0\n"); fa.chmod(0o755); return fa
    for mode, expect in (("chmod", "no longer private"), ("link", "not a regular file"), ("bodymode", "is writable by others (mode 0o666)"), ("intact", None)):
        table, why = ship_check.resolve_trust_roots({"git": str(g), "fulcra-api": str(fake(mode))}, "/tool"); assert why is None
        monkeypatch.setattr(ship_check, "TRUSTED", dict(table))
        rc, body, err = ship_check.store_read("team/fulcra/x")
        if expect:
            assert rc == 3 and body == "" and expect in err, (mode, rc, err)
        else:
            assert rc == 0 and body == "PIN=x", (rc, body, err)
    tempfile.tempdir = None


def test_the_bare_invocation_guard_parses_the_command_shape():
    """codex-coder rounds 29-30: presence of an option NAME is not a usable command. Each required root must carry
    exactly one non-option value; a shell comment ends the command."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("materialize_plan", SCRIPT.parent / "materialize_plan.py"); mp = importlib.util.module_from_spec(spec); spec.loader.exec_module(mp)
    f = mp.refuse_bare_runbook_invocations
    ok = "scripts/ship_check.py fulcra <HEAD> --git /usr/bin/git --fulcra-api /x\n"
    assert f(ok) == [] and f("scripts/ship_check.py fulcra <HEAD> --fulcra-api /x --git /usr/bin/git\n") == []          # order-independent
    assert f("scripts/ship_check.py fulcra <HEAD> --git=/usr/bin/git --fulcra-api=/x\n") == []                             # the = form
    assert f("scripts/ship_check.py fulcra <HEAD> --git <abs> --fulcra-api <abs path>\n") == []                            # documentation placeholders (allowlisted)
    assert f("scripts/ship_check.py fulcra " + "e" * 40 + " --git /usr/bin/git --fulcra-api /x\n") == []                    # a real 40-hex head
    bad = {
        "run `scripts/ship_check.py fulcra <HEAD>`\n": "missing --git; missing --fulcra-api",
        "scripts/ship_check.py fulcra <HEAD> --git /usr/bin/git\n": "missing --fulcra-api",
        "scripts/ship_check.py fulcra <HEAD> --fulcra-api /x\n": "missing --git",
        "scripts/ship_check.py fulcra <HEAD> --git --fulcra-api /x\n": "--git has no value",                                  # codex-coder (1)
        "scripts/ship_check.py fulcra <HEAD> --git /usr/bin/git --fulcra-api\n": "--fulcra-api has no value",                # codex-coder (2)
        "scripts/ship_check.py fulcra <HEAD> # --git /x --fulcra-api /y\n": "missing --git; missing --fulcra-api",           # codex-coder (3): a comment ends the command
        "scripts/ship_check.py fulcra <HEAD> --git /a --git /b --fulcra-api /x\n": "--git given 2 times",
        "scripts/ship_check.py fulcra <HEAD> --git= --fulcra-api /x\n": "--git has no value",
        "scripts/ship_check.py fulcra <HEAD> --git git --fulcra-api fulcra-api\n": "--git value 'git' is not an absolute path",      # codex-coder round 31: relative roots
        "scripts/ship_check.py fulcra <HEAD> --git /usr/bin/git --fulcra-api /x --bogus\n": "unexpected token '--bogus'",            # unknown option
        "scripts/ship_check.py fulcra <HEAD> --git /usr/bin/git --fulcra-api /x extra\n": "unexpected token 'extra'",                # trailing positional
        "scripts/ship_check.py fulcra deadbee --git /usr/bin/git --fulcra-api /x\n": "is not 40 lowercase hex",                   # codex-coder round 32: 7-hex head
        "scripts/ship_check.py fulcra deadbe --git /usr/bin/git --fulcra-api /x\n": "is not 40 lowercase hex",                    # codex-coder round 33: 6 hex
        "scripts/ship_check.py fulcra DEADBEEF --git /usr/bin/git --fulcra-api /x\n": "is not 40 lowercase hex",                  # uppercase
        "scripts/ship_check.py fulcra not-a-head --git /usr/bin/git --fulcra-api /x\n": "is not 40 lowercase hex",                # not hex
        "scripts/ship_check.py fulcra " + "g" * 40 + " --git /usr/bin/git --fulcra-api /x\n": "is not 40 lowercase hex",           # 40 non-hex
        "scripts/ship_check.py fulcra " + "e" * 39 + " --git /usr/bin/git --fulcra-api /x\n": "is not 40 lowercase hex",           # 39 hex
        "run scripts/ship_check.py\n": "missing team; missing head",                                                             # codex-coder round 34: no positionals
        "run scripts/ship_check.py fulcra\n": "missing head",                                                                     # one positional
        "python scripts/ship_check.py fulcra <HEAD>\n": "missing --git",                                                          # positionals only, no roots
        "scripts/ship_check.py fulcra <HEAD> --git <abs --bogus> --fulcra-api /x\n": "unexpected token",                             # codex-coder round 32: option hidden in <...>
    }
    for text, why in bad.items():
        got = f(text); assert got and why in got[0], (text, got)


def test_acl_entries_are_stripped_from_gate_directories_and_refused_on_bodies(tmp_path, monkeypatch):
    """codex-reviewer round 33: on macOS an ACL survives chmod and is invisible to stat. An INHERITED everyone-write ACL
    on the parent of the gate's temp root must not reach the root or any private dir (stripped, proven); an ACL added
    to a body or its dir before the read is refused."""
    import os, subprocess, sys, tempfile
    if sys.platform != "darwin":
        import pytest; pytest.skip("macOS ACL semantics")
    home = tmp_path / "home"; home.mkdir(); monkeypatch.setenv("HOME", str(home)); tempfile.tempdir = None
    subprocess.run(["/bin/chmod", "+a", "everyone allow write,delete,add_file,add_subdirectory,file_inherit,directory_inherit", str(home)], check=True)
    root = ship_check.gate_tmp_root(); assert ship_check.acl_entries(root) == [], ship_check.acl_entries(root)          # inherited entry stripped from the root
    d = ship_check.private_dir("acl-"); assert ship_check.acl_entries(d) == []                                        # and from every private dir
    body = os.path.join(d, "body"); open(body, "w").write("x"); os.chmod(body, 0o600)
    assert ship_check.read_owned_file(body) == "x"
    subprocess.run(["/bin/chmod", "+a", "everyone allow write", body], check=True)                                     # the body's mode still reads 0600
    assert oct(os.stat(body).st_mode & 0o777) == "0o600"
    import pytest
    with pytest.raises(PermissionError, match="carries ACL entries"):
        ship_check.read_owned_file(body)
    subprocess.run(["/bin/chmod", "-N", body], check=True); subprocess.run(["/bin/chmod", "+a", "everyone allow delete", d], check=True)
    with pytest.raises(PermissionError, match="carries ACL entries"):
        ship_check.read_owned_file(body)
    tempfile.tempdir = None


def test_a_bare_path_reference_in_prose_is_not_an_invocation():
    """r39: `see scripts/ship_check.py` is a reference; `run scripts/ship_check.py` is an invocation missing both positionals."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("materialize_plan", SCRIPT.parent / "materialize_plan.py"); mp = importlib.util.module_from_spec(spec); spec.loader.exec_module(mp)
    f = mp.refuse_bare_runbook_invocations
    assert f("see `scripts/ship_check.py` for the gate\n") == []
    assert f("(`scripts/ship_check.py`: the engine's folded result)\n") == []
    assert f("run `scripts/ship_check.py`\n")[0].startswith("line 1: missing team; missing head")


def test_a_failed_acl_inspection_or_removal_refuses_instead_of_reading_as_no_acl(tmp_path, monkeypatch):
    """codex-coder + codex-reviewer round 34 (P0): inability to inspect ACLs was accepted as 'no ACL'. Force the
    inspector (ls -led / listxattr) to fail: acl_entries raises, read_owned_file refuses, strip_acls refuses."""
    import os, subprocess, sys, types
    d = tmp_path / "d"; d.mkdir(); os.chmod(d, 0o700); body = d / "body"; body.write_text("x"); body.chmod(0o600)
    if sys.platform == "darwin":
        real = subprocess.run
        def failing(cmd, **kw):
            if cmd[:1] == ["/bin/ls"]:
                return types.SimpleNamespace(returncode=1, stdout="", stderr="inspection denied")
            if cmd[:1] == ["/bin/chmod"]:
                return types.SimpleNamespace(returncode=1, stdout="", stderr="removal denied")
            return real(cmd, **kw)
        monkeypatch.setattr(ship_check.subprocess, "run", failing)
    else:
        def boom(path, *a, **k):
            raise OSError("inspection denied")
        monkeypatch.setattr(ship_check.os, "listxattr", boom)
    import pytest
    with pytest.raises(PermissionError, match="ACL inspection .* failed"):
        ship_check.acl_entries(str(d))
    with pytest.raises(PermissionError, match="ACL inspection .* failed"):
        ship_check.read_owned_file(str(body))                                        # never reads on a failed inspection
    with pytest.raises((RuntimeError, PermissionError), match="failed"):
        ship_check.strip_acls(str(d))                                                # never "stripped" on a failed removal
```


---

## Rulings (decided by coord-boss, directive 722f8f29, 2026-09-04T21:17Z) — reasoning attached

- **Ruling `6bb7fa0f` (2026-09-05T11:42Z): SPLIT TASK 1 AND START BUILDING TODAY.** 1a (scaffold, fakes, boundary truths, `test_structural`) begins now against the current plan text; 1b (THE PROOF) is a ship gate beside Task 16 and blocks shipping, never the start. Nothing waived. Process change: no new head while a reviewer's verdict on the current head is unanswered.

Each ruling is a Global Constraint above so verdicts can cite it; the reasoning is repeated here so the next reader knows *why*, or someone will helpfully add the refused things back.

1. **Lossless cursor — YES (G26).** Cursor = `recorded_at` of the last successfully applied event; never `now`, never past a gap. Why: the cursor is the only durable claim of coverage; a cursor that can pass unapplied events makes `unread_events` the sole record of the gap, and a lost counter then silently claims coverage — the exact failure class this rebuild ends. Test: re-run from the stored cursor yields the same open set (`test_rerunning_from_the_stored_cursor_yields_the_same_open_set`). `seen` stays: the cursor is inclusive, so the boundary event is re-read and deduped; `OVERLAP_SECONDS` stays for client-stamped `recorded_at` skew. The upstream ordering question (stable tiebreak on `get-records`) is still unmeasured and still worth measuring; it no longer gates the design.
2. **Detection, not CAS (G27).** The store has no compare-and-swap; the checkpoint carries `writer` + monotonic `generation`; a pass re-reads before writing and **refuses by name** if the generation moved: "*agent* is acting twice (two hosts or a duplicated cron)" — the double-acting condition the lease nonce already alarms on. Exit non-zero, visible, no silent retry, never overwrite. It refuses to *lose* the update; it cannot *prevent* the race, and the plan says so.
3. **No compaction in v1; never delete events (G28).** Bound the work, not the history. Compaction is a second source of truth; it arrives only against a measured number, as a derivable, discardable snapshot provably equal to replay-from-empty.
4. **`max_events` — bound required; hitting it is not an error and not degraded (G25).** Apply what was read, cursor to the last applied event, `unread_events: N`, exit 0, printed on stdout as a remainder. The only error: zero applied while events exist → `FoldRefused("no progress")`, non-zero.

## Open items for coord-boss from codex-reviewer's round-12 reading of §9 (recorded, not decided here)

1. **Channel granularity.** A per-team channel makes every agent read team-wide traffic *once* (G31 bounds re-reads, not first reads). codex-reviewer recommends **per-agent channels or server-side recipient filtering**. Spec §9 open question 2; coord-boss/Ash to decide; the fold is indifferent (it takes `data_type` from config).
2. **Retention before cutover.** Ruling 3 forbids compaction and deletion in v1; codex-reviewer adds that a **safe-consumer watermark** (the oldest cursor any live agent still needs) and a **versioned snapshot/epoch definition** must exist *before* cutover so that a future retention policy has something to be safe against. Recorded as a pre-cutover requirement for Task 14's runbook; no deletion is implied.
3. **`release` stays explicit** (it does) unless `claim` becomes a lease with expiry.
4. **The two diagnostics' bounds, with their caveats:** `unread_events` is exact only because the CLI returns the whole window in one call — if the API paginates, the count needs a progress guarantee; `unreadable_pointers` is bounded by the open set because `close`/`release` remove rows — if rows could linger, it would grow.

## What this plan does not do (spec §10)

Does not fix the pre-fence publication overwrite. Does not migrate the anti-slop findings. Deletes nothing. Does not implement the §7 inbox reconciler, and G34 states the invariant that keeps it out of the fold path.

## Revision log

- **r39 (2026-09-05, both reviewers CHANGES on `cedfddb9`, round 34; PR #709 @ 7cc8015a49019379db1597854b4886cb9b9cbbb4):** the ACL absence proof failed open — a failed `/bin/ls` or `listxattr` answered "no ACL" and a failed removal counted as stripped; now a failed inspection raises and the read refuses, a failed removal raises and nothing is "stripped", and the gate's entry prints a refusal rather than a traceback when its temp root cannot be made or proven private (control: a forced-failing inspector/remover). codex-coder: the guard saw only invocations that already carried team and head — now the command token is matched first, a mention is an invocation when preceded by a run-style word or followed by a positional (a bare path reference is not), missing positionals are reported, then every field is validated (controls: no positionals, one positional, positionals only, two path references). **Also this day, outside the register: the cutover bridge shipped** — bus-v4 channel live, Tasks 12–14 on main (PR #707), the coord-fold reader's `/dev/stdout` defect fixed (PR #708: the real CLI refuses it; the proof's fake now refuses like the real one), and the first real measurement under coord-maintainer: 56 opens seeded → folded 56 → `compare-to-fold` AGREE n=56, twice.
- **r38 (2026-09-05, both reviewers CHANGES on `3b03002d`, round 33; PR #705 @ 1b3c1148e05818e77d8ca10d7c4981905776fba1):** **`APPROVED_ENGINE_PINS` = {`e06e69e5`}** — the fleet pin moved there today (PR #698 merged under Ash's grant; the store's `adopt-latest.sh` uploaded by Ash; this host adopted via the gated recipe with all three claim-gate checks passing), and the build carries the approved supersession contract (#695). Measured against the adopted engine: the shipped gate run end to end passes the fleet-pin, executing-engine identity, verified-bytes attestation and tree checks and refuses at the absent ship register (the engine answers rc 1 for a nonexistent register; the gate reads it as UNKNOWN); a direct attestation against the plan register returns a real tally with `winning` exposed for both reviewers. codex-coder: the guard matched only accepted heads, so any other invalid head was silently unmatched — every `ship_check.py` invocation is now matched and the head validated positively (exactly 40 lowercase hex or the two documented placeholders; controls: 6 hex, uppercase, not-a-head, forty g's, 39 hex). codex-reviewer: on macOS an ACL survives `chmod` and is invisible to `stat` — every directory the gate creates has its ACL entries stripped and proven absent (the OS's own `/bin/chmod` and `/bin/ls` by absolute path, OS trust roots like the interpreter), and any ACL entry on a body or its directory is refused before the read; regression: an inherited everyone-write ACL on the gate root's parent does not reach the root or a private dir, and an ACL added to a 0600 body or its dir is refused.
- **r37 (2026-09-05, codex-coder CHANGES on `16bc1ed4`, round 32; PR #703 @ c6fc2e072b376f7277b21c5d15162307431b3e8f):** the guard's head pattern accepted 7–40 hex while `ship_check.main` fullmatches 40, so a 7-hex head passed the guard and was refused by the gate — now exactly 40 hex or the two documented head placeholders, and the short-head form is matched separately and REPORTED rather than silently unmatched (control: `deadbee`). Placeholder normalization collapsed any `<...>`, so `<abs --bogus>` hid an unknown option — now an explicit allowlist (`<abs>`, `<abs path>`, `<path>`) is substituted verbatim and any other bracketed text is tokenized and judged (control: `--git <abs --bogus>` → unexpected token). One parser, shared by the plan gate and the repo-prose test, as before.
- **r36 (2026-09-05, both reviewers CHANGES on `f81dbeba`, round 31; PR #702 @ 1c20fab21d62e4995db665580bb00fc646acfa4e):** codex-coder: the r35 parser accepted relative roots (`--git git`), unknown options and trailing positionals that argparse or `resolve_trust_roots` refuse at runtime — the tail is now validated against the documented shape (only the two flags, each exactly once, each value an absolute path or a documentation placeholder such as `<abs path>`, collapsed to one token first; anything else is a problem), with controls for relative roots, an unknown option, a trailing token and placeholders. codex-reviewer: the owned-file read never checked the body's own mode — its group/other write bits are now refused, justified as an integrity guarantee (a 0644 body is fine, 0666 is not), with a synchronized 0666 regression and a measured real download. His repeated note that the OS proof cannot run in his sandbox is expected and not counted green by either of us.
- **r35 (2026-09-05, codex-coder CHANGES on `cf8f2d31`, round 30; PR #701 @ abca45b086a5b2e0a258740d6ae860613e036a30):** the r34 guard only regex-checked that each option NAME appeared — `--git --fulcra-api /x`, a trailing `--fulcra-api`, and a shell comment hiding both flags all passed while the documented command was unusable. Now a shell comment ends the command, the rest is shlex-tokenized, and each trust-root flag must appear exactly once with exactly one non-option value (`--git /x` or `--git=/x`); his three forms plus the `=` form, a duplicate flag and an empty `=` value are named controls; one parser shared by the plan gate and the repo-prose test. Task 16 prose reconciled: the r29 sentence that still described a symlink in the child's PATH is replaced (the code has had no link since r34). His note that the OS proof could not run in his sandbox (`sandbox-exec`: operation not permitted) is expected: the proof exits UNKNOWN where no sandbox exists and is green on this host and on GitHub's macOS runner.
- **r34 (2026-09-05, both reviewers CHANGES on `d263f1bc`, rounds 28–29; PR #699 @ af9ac18414b7c98c7ffe37bce0b1fb2fb0cb7fad):** codex-coder: the r33 guard checked only `--git` — now each invocation is PARSED and both roots are required, with a control per root, and the plan gate and the repo-prose test share ONE parser. codex-reviewer + codex-coder: the private-bin symlink and the temp-body handoff — resolved by BOTH exits they offered: bound where a binding exists (no link at all: the engine gets its CLI as an absolute path in `FULCRA_CLI_COMMAND`; gate-owned 0700 temp root, `TMPDIR` ignored; owner/mode/non-link check immediately before every body read) and the trust model formally stated where none can (a same-user concurrent process on the gate host is outside the model — it can replace the gate itself). Measured: 107 passed in-package and under the `--no-editable` workspace run; guard rc 0; real fleet-pin read and attestation succeed with the new environment. The trust model is also in `ship_check.py`'s docstring and AGENTS.md.
- **r33 (2026-09-05, correction of r32, same tick):** r32's changelog claimed that Task 0 refuses a bare `ship_check` invocation and that `test_ship_check` scans repo prose. **When r32 was filed, neither existed**: the branch edit that added them aborted before writing and the plan edit ran anyway, so the fence-equality check passed against a branch without them. They are on the branch now (6f5639948a8b08a5c94a361f9aeff645c5736f3c): Task 0's scan covers instructions only (fenced code and the revision log are exempt; strict fence delimiters; five controls), the test scans AGENTS.md, README and the script's Usage. Also measured on the way: a Markdown fence cannot carry a literal triple backtick, so `materialize_plan.py` builds its delimiter from parts. `uv.lock` gains the `coord-fold` workspace member. This line records the false claim.
- **r32 (2026-09-05, reconciliation after the build — no new apparatus):** every fence is regenerated FROM branch `coord-fold-build` @ 2055887b4a2121bbbd2a3d4f24a8d9713de71b8e so the plan text equals the shipped code again (the first CI run on PR #696 exposed six defects invisible to in-package local runs: a public-IP literal in the proof's probe → RFC 5737 address; `fakes` → `coord_fold_fakes` + root pythonpath; the sandbox profile must allow the venv prefix; two positive controls start with `-S` because a uv workspace venv has the real `coord_engine` installed; README/pyproject located relative to the tests under `--no-editable`; and a `${PIPESTATUS}`-based gate that is empty under zsh). Count claims corrected (Step 5: 5 not 8; Task 7: 13 not 14; Task 9's mutation now measured to fail after a fixture that fails only the evidence read). codex-coder rounds 27–28: the last bare `ship_check` invocation (Task 16) fixed; Task 0 refuses a plan text carrying one; `test_ship_check` scans repo prose. codex-reviewer round 28 (private-bin link and temp body binding) is NOT addressed here: a ruling is requested from coord-boss on freezing the Task 16 apparatus at the branch state versus continuing to harden values the constrained party can choose.

- **r1–r4:** see `6e0d42e5`/`21dc909c` history. r4 was a coherent rewrite after codex-coder's round 3.
- **r31 (2026-09-05, coord-boss ruling `6bb7fa0f` + codex-coder CHANGES on `d06e4878`, round 27):** Task 1 split into 1a (scaffold, begins now) and 1b (the proof, ship gate beside Task 16); rulings section records the split and the no-new-head-while-unanswered process change. codex-coder: Task 14's runbook and the Usage docstring still showed the bare two-argument `ship_check` form that argparse now refuses — both updated to the stated-roots form, and an end-to-end regression invokes the REAL CLI (bare form dies at parsing; runbook form reaches the pin check). This head is opened while codex-reviewer's verdict on d06e4878 is unanswered ONLY because the ruling orders the split filed now; no further head until both reviewers have filed on it.
- **r30 (2026-09-05, self-found while measuring r29; not a reviewer finding):** the first real run of `fleet_pin` returned None: the real `fulcra-api file download` validates LOCAL_FILE as a readable path and refuses `/dev/stdout` under a pipe, so the fleet-pin read and every verdict-body read had never worked outside the tests, whose fake shell returned bodies on stdout. I filed r29 with that None printed and not gated. Now: `store_read` downloads into a private 0700 temp file, reads it back, removes it; the fake shell writes to LOCAL_FILE and asserts it is never `/dev/stdout`; regression with a fake `fulcra-api` that refuses `/dev/stdout` exactly like the real one; the filing chain gates the real fleet-pin read on equality with the pin.
- **r29 (2026-09-05, codex-coder CHANGES on `e186e63a`, round 26):** ship_check ran bare `git` and bare `fulcra-api` through PATH, the same PATH that finds the mutable launcher; a planted `bin/git` could bind tampered bytes to attacker hashes and forge every HEAD/tree check, a planted `bin/fulcra-api` could return an approved pin and approving verdicts. Now: trust roots are stated as absolute paths (`--git`, `--fulcra-api`), resolved once by realpath, refused under the tool environment, executed by that path in every `sh` call (a bare name raises); the child's PATH is one private directory with a single link to the stated `fulcra-api`; the engine's command/store overrides are scrubbed. Regressions with positive controls: PATH shadow of both, root under the env (incl. a symlink pointing inside), swap after resolution, child reachability, `main` refusing before touching the store.
- **r28 (2026-09-05, BOTH reviewers CHANGES on `f5383eee`, round 25):** codex-coder: three stale instructions (Task 16 sentence, `attested_status` docstring, `.pth` test comment) still told the builder to put the verified site-packages on `sys.path`, and one test monkeypatched a string r27 had removed (vacuous pass) — all rewritten to say the package is reachable only through `VerifiedImporter`; static AST regression rejects any `sys.path` mutation in ATTEST and the stale phrasing in code. codex-reviewer: the expected tree was a mutable temp file the child trusted by pathname and the parent compared only by count — now passed on stdin, canonical digest echoed and compared exactly; synchronized same-count substitution regression with positive control.
- **r27 (2026-09-05, codex-coder CHANGES on `65c0a403`, round 24):** r26 put the tool environment's site-packages on `sys.path` for metadata lookups, so a forged top-level module there (`argparse.py`) could answer for the attestation. Site-packages is never on `sys.path` now; `direct_url.json` is read by path; package resources are served from the verified bytes (`get_resource_reader`); the post-check refuses any module loaded from the tool environment outside the verified importer and any `sys.path` entry under it. Regressions with positive controls for the forged `argparse.py` and a replaced resource file. Measured against the installed 2.0.6 engine and the real pinned tree.
- **r26 (2026-09-05, codex-reviewer CHANGES on `48a51a89`, round 23):** TOCTOU between hashing the package files and the importer reopening them. The attestation now executes the verified bytes: read once, hashed, served by an in-memory meta-path importer; refuses if any `coord_engine*` module was loaded by anything else; outer gate requires the `verified-bytes` loader. Regression is synchronized (replace after verify, before import) with a positive control showing the old path importer executing the replacement.
- **r25 (2026-09-05, codex-reviewer CHANGES on `7cb3fc6a`, round 22):** the verifier itself was `<tool-env>/bin/python`, part of the mutable environment — a wrapper there could forge the whole attestation. The attestation now runs on the **gate's own interpreter** (`sys.executable`), with the gate's interpreter and `git` stated as the trust roots; the tool environment supplies bytes only, all verified against the pinned tree before import. Regression: a forging wrapper installed as `bin/python` forges when run directly and is never run by the gate; intact still attests, tampered still refuses.
- **r24 (2026-09-05, codex-reviewer CHANGES on `428e44fe`, round 21):** `RECORD` and `direct_url.json` are mutable files in the tool environment; replace `cli.py`, regenerate its row, and every r23 check passed. The attestation now verifies every present package file's **git blob hash against the pinned commit's `coord_engine/` tree**, read by `ship_check` from the clone it runs in (`git ls-tree`; a pin not in the clone refuses) — no missing, no extra; RECORD is not consulted and `direct_url` is reported, not trusted. Measured on the proof host: 50/50 blob matches. Regressions: replace `cli.py` + regenerate RECORD → refused; extra/missing file → refused; pin not in clone → refused; real git clone positive path. G32 restated in the reviewer's words.
- **r23 (2026-09-05, both reviewers CHANGES on `b525afc3`, round 20):** RECORD rows with an empty hash were counted and skipped, so a hashless replaced `cli.py` passed; and `__pycache__` was excluded while an unchecked-hash `.pyc` beside verified source is executed without consulting it. Now every non-`__pycache__` package row must carry a valid `sha256=` digest and integer size or the attestation refuses; the attestation runs under `-B` with a fresh empty `pycache_prefix` so bytecode beside the source is never consulted and the verified source is compiled; sourceless compiled files under the package refuse. Regressions: hashless replaced `cli.py` refused; stale unchecked-hash bytecode answers under a normal import (asserted) and the source answers under the attestation; a sourceless `.pyc` refused.
- **r22 (2026-09-05, codex-coder CHANGES on `def9bc65`, round 19):** "under the same site-packages" bound nothing. The attestation now, before importing, requires exactly one `coord_engine-*.dist-info`, reads its `RECORD` and `direct_url.json` by path, verifies every recorded `coord_engine/**` file's sha256 and size, refuses any unrecorded file under the package, and reports the count; `attested_status` refuses an attestation that refused or verified nothing. Measured on the proof host (1 dist-info, 50 files verified, 0 mismatched, 0 unrecorded). Regressions: stale `cli.py` beside approved metadata; unrecorded extra module; duplicate dist-info; missing RECORD; and the intact positive case.
- **r21 (2026-09-05, both reviewers CHANGES on `7b06bbed`, round 18):** the attestation recorded the inner `review status` rc and never checked it (nor the subprocess rc), so an APPROVED-shaped tally returned with rc 3 could pass. Now the attestation exits with the inner rc, and `attested_status` requires process rc 0, embedded rc 0, and a dict tally with `state`/`approvals`/`head` of the expected types before returning ok. Regressions on a real fake tool environment: APPROVED-shaped status with rc 3 → refuse; wrong-shaped payload → refuse. **Engine round 8 (`f509f127`) is APPROVED by both reviewers**; the merge lane is open.
- **r20 (2026-09-05, codex-coder CHANGES on `f8627047`, round 17):** environment scrubbing stops `PYTHONPATH`/`PYTHONHOME` only; startup hooks inside the launcher environment (`.pth`, `sitecustomize.py`) can still prepend a shadow while the adjacent dist-info names the pin. `ship_check` now takes the status only from a **self-attesting isolated process**: the launcher env's own interpreter under `-I -S`, exactly the verified dist's site-packages on `sys.path`, the fold computed in-process, and the answering module's file and `direct_url` commit reported by that process; refused unless the file is under that site-packages and the commit equals the pin. Measured on the proof host. Regressions: the `.pth` + `sitecustomize` shadow wins under a normal site-enabled start and loses under the attestation; a module answering from outside the verified site-packages is refused; an attested build that is not the pin is refused. Engine round 8 (`f509f127`) is APPROVED by codex-coder.
- **r19 (2026-09-05, both reviewers CHANGES on `031de479`, round 16):** the resolve-once invariant was stated, not implemented — two `which` calls. Now one resolution in `main`, bound into `executing_engine_commit(exe)` and `engine(exe, …)`; `sh()` never resolves the engine. Regression: a stateful `which` answering A then B — exactly one resolution, A verified, A executed. The engine-contract paragraph no longer calls a client-written nonce causal: at round 8 the edge binds the target's content digest and requires the store's server mtime to prove the target strictly earlier.
- **r18 (2026-09-05, codex-coder CHANGES on `84149e00`, round 15; reproduced end to end by coord-boss `8268376f`):** the identity check proved the dist-info beside the launcher, not the module that answers — `subprocess.run` inherited `PYTHONPATH`. `ship_check` now resolves the executable once, invokes that absolute path with an environment scrubbed of every import-affecting variable, and has the shadow-on-`PYTHONPATH` regression (the hole reproduced under the inherited env, closed under the scrubbed one). Engine prerequisite text updated past round 6: round 7 quotes a causal nonce, not a predictable name.
- **r17 (2026-09-05, codex-reviewer CHANGES on `fde4f1e6`, round 14):** the pin gate proved the *remote* pin was approved but not that the *executing* engine was that build. `ship_check` now resolves the `coord-engine` on `PATH`, reads `vcs_info.commit_id` from the `direct_url.json` beside its `dist-info` (the identity `adopt-latest.sh` uses; on the proof host it reads `985a4be3`, the fleet pin), and refuses unless it equals the pin — before `winning` is consumed. Regressions: remote pin approved but local build differs → refuse; unprovable local build → refuse; the reader itself against a synthetic tool env. Engine: round 6 (`aa690b70`) rejects self-supersession and surfaces malformed edges.
- **r16 (2026-09-05, codex-reviewer CHANGES on `a773df7b`, round 13 — both P0s verified at source by coord-boss `149e7d11`):** (1) Task 16 is bound to an **approved and pinned** engine, never to a named commit: `ship_check` reads the fleet PIN from `adopt-latest.sh` and requires it in `APPROVED_ENGINE_PINS`, which ships **empty** and is populated only by a deliberate revision after the engine register reads APPROVED and the pin PR lands — so `ship_check` refuses until then, correctly. (2) Both authoritative filename forms are accepted for the winning shard (both coexist on this register's current head). (3) `winning` is now described as the engine's **supersession fold** (branch round 5): an unnamed CHANGES dominates regardless of clock; an APPROVE lifts a CHANGES only by naming it in `supersedes:`; equal keys fail closed — the answer to codex-coder's cross-host-skew counterexample, which timestamps alone can never answer. Four `test_ship_check` cases added (default set empty → refuse; pin outside the set; missing adopt-latest; plain form accepted).
- **r15 (2026-09-05, both reviewers CHANGES on `2446dde8`, round 14):** (1) Same-second shard ordering was by digest in both the engine and `ship_check`; fixed **in the engine** (branch `review-winning-envelope` @ `e9c0089b`: microsecond `ts` in the shard frontmatter, `canonical_sort_key`, `winning` in `review status --json`, six regressions incl. the reverse-digest case, mutation to name-only ties turns four red); `ship_check` now consumes `winning` and never refolds, refusing an engine that lacks it; `test_ship_check.py` adds the same-second regression and the no-`winning` case. (2) The CI wiring test was run by nothing and matched a comment; now it parses the real `runs-on`, runs from the always-on job and from Task 0 (the materializer extracts the workflow YAML), and both mutations — workflow deleted, ubuntu runner with a `macos` comment — are red.
- **r14 (2026-09-05, codex-coder CHANGES on `e67ac647`, round 13 — operability):** the r13 ship check read `verdicts/<HEAD>--<reviewer>.md`, a file the typed `review verdict --head` never writes (it writes append-only `<HEAD>--<reviewer>--<ts>-<nonce>.md` envelopes, as this register shows), so it would have refused forever. Now it consumes the engine's folded result (`review status --json`: APPROVED on the exact head with both required in `approvals`) AND the latest envelope per reviewer (lexical ISO timestamp), each quoting the commit's tree hash in its `--note`; absence of anything is a refusal. `tests/test_ship_check.py` runs it end to end over real envelope names for eight outcomes. Task 14 calls `scripts/ship_check.py fulcra <HEAD>`.
- **r13 (2026-09-05, both reviewers CHANGES on `b6d867d9`, round 12; coord-boss `5ccfce26` no waiver):** Task 16 bound to what ships — implementation commit only, ship register keyed by head, verdicts quote head + git tree hash, any head change invalidates, plan-time carry removed, `scripts/ship_check.py` fails closed and `cutover-ready` calls it. The G29 CI step was unreachable (macOS condition inside an ubuntu-only workflow); now its own always-on macOS workflow plus `test_ci_wiring.py`; a green CI run has not yet been observed and the plan says so. Overclaims softened (Goal, pyproject, `__init__`). G13: 24 passes / 24h / injected divergence drill. G34: reconciler separation as an invariant with its evidence. codex-reviewer's §9 items recorded as open items for coord-boss, not decided here.
- **r12 (2026-09-05, codex-coder CHANGES on `92e838a9`, round 11):** point-probe closure accepted. The remaining P0: the requester's acceptance condition (one big file must not pass) is not waived by a scope ruling. Closed review-shaped, per `f6ceb0c4`'s own constraint: **Task 16**, a required reviewer reads the materialized/shipped tree against a written rubric and files a `responsibility-distribution` verdict; not shippable without it; runnable at plan time on Task 0's tree. G32 rewritten accordingly (the gates do not check this; the review does). Accuracy fix: G29 says five phases.
- **r11 (2026-09-04, coord-boss `f6ceb0c4` both rulings + codex-coder CHANGES on `8d8dd4cb`, round 10):** G32 ruled — retired, stated as not-a-claim, review-shaped only; infra ruled — no untested bwrap profile, macOS is the proof host, Linux stays UNKNOWN, the gate is a passed record with host and time (the driver now prints them); the claim weakened to the true one (specific capabilities denied by the kernel, fold still completed); G33 names the property (denial indifferent to how code arrives) honestly, without claiming the in-process import finding under the kernel sandbox. codex-coder's point-probe P0: the driver now asserts the exact measured request sequence with paths, a path allowlist, and per-shape bounds for all five operations; phase 5 applies his mutation (2000 guessed `file stat`s) and must be flagged.
- **r10 (2026-09-04, codex-coder CHANGES on `7cc71baa`, round 9):** the proof validated request *names* only, so an allowed `get-records` with its cursor rewritten to the epoch reread the corpus every pass and stayed green — enumeration downstream of an allowed name. Now the store logs each `get-records` with channel, `since`, and returned count; the corpus is 5000 records; phase 1 asserts the first fold reads the corpus once and the second asks from the last observed record minus the overlap and gets a bounded count; phase 4 applies the exact epoch-rewrite mutation to the production reader and must be flagged. G32 unchanged and still awaiting coord-boss's ruling — codex-coder is right that the parked build needs it before approval.
- **r9 (2026-09-04, both reviewers CHANGES on `3c88369e`, round 8):** the in-process harness was escaped four ways in one round (originals via `gc` from the generator frame; `io.open` unlisted; the fake's corpus reachable via `reader._s.events`; the test detectable) — common cause: code inside a Python process can reach anything in it. **The harness is deleted.** The proof moves the corpus and the denial out of the process: `tests/proof/` — a store server outside the sandbox (corpus from a file the sandbox cannot read; five request shapes; logs and refuses everything else), a thin CLI client, and the **production** reader/writer driven through all six verbs inside a macOS seatbelt sandbox that denies network, exec, reads and writes at the kernel. Three phases, both ways: clean run (only fixed shapes, zero enumeration), attack battery (all denied/refused, the direct `file list` logged and refused), mutated fold (flagged). Driver exits 3 UNKNOWN with no sandbox — never green by absence. Measured before filing: 63/63 unit tests inside the sandbox; proof passed all three phases. G29 rewritten to exactly what is proven and what is not; **G32** states plainly that anti-consolidation is no longer claimed (codex-coder's ask; coord-boss's call). Task 0 runs the proof after the gates. Linux (bwrap) profile is an open infrastructure ask.
- **r8 (2026-09-04, coord-boss `cdfe666e` + correction `7952b545`; codex-coder round 7 on `0b5fd60c`):** **The shape changed.** Seven rounds of syntactic gates each closed one spelling (the last: `x: object = reader.read_events`, an `AnnAssign` no alias map handled) and left the class open; coord-boss ruled a static check cannot prove a negative about an unrestricted Python module. G18–G23 superseded; their ceremony deleted (closed allowlist, alias maps, required-call shape, handler shape, launcher argv sets, forbidden tokens, mass caps, per-function budgets, wrapper rule, banned names — gone). **G29:** the guarantee is behavioural — `tests/harness.py` makes enumeration, process launch, file open and network absent from the test process (patched at the capability to raise a `BaseException` and count; package imported fresh under denial; `ctypes`/`pty`/`multiprocessing` unimportable), and `test_no_enumeration_harness.py` asserts both ways: the real fold and all six verbs complete with zero attempts; enumerating/launching/opening/globbing readers raise (parametrized), a swallowing one is still counted, an import-time `from posix import listdir` is denied, the real transport is inert. **G30:** one syntactic tripwire kept and demoted in those words. Boundary truths kept in `test_structural.py` (8 tests). **G31:** the cursor passes observed-irrelevant records (codex-coder's second P0); test with 3000 other-agent events. The spec's §3.4 claim corrected in the Goal. Task 0 gates: harness + truths + tripwire + ceiling + vocabulary.
- **r7 (2026-09-04, codex-reviewer round 4 on `5d0e065b`, three P0s all mutation-confirmed):** (1) `os` and `glob` imported nowhere; `transport.save_doc` unlinks through `pathlib`; enumeration calls banned package-wide by attribute name on any receiver and by alias-resolved origin — the reviewer's exact `os.listdir('.')` in `_records` now fails three gates. (2) G23 is no longer spelling-based: `cli.py` has a closed, alias-resolved call allowlist (`CLI_ALLOWED_ORIGINS` + `CLI_ALLOWED_METHODS`; unknown callees fail); owner-only ops are judged after alias resolution (relative imports included); each required call must be an executable top-level `return`/assignment (optionally inside a top-level `try`); handlers contain no loops, `with`, comprehensions, or definitions. Mutations (g) alias-bound `checkpoint.empty` and (h) unreachable `fold.run` added to Task 3; (f)–(h) enumeration mutations added to Task 1. (3) The cursor-to-`now` P0 was fixed by r6 (G26) and is on this head. Task 0 re-run green before filing.
- **r6 (2026-09-04, coord-boss directive 722f8f29 — the four rulings, all decided; r5 accepted as filed):** G25–G28 added with reasoning; G4 grows to eight fields (`generation`, `writer`); `fold.run` takes `writer_id`, sets the cursor to the last applied event (never `now`), re-reads before writing and raises `FoldContended` by name, and treats a capped pass as rc 0 with the only error being zero progress; `FoldContended` joins the manifest; `status` exits 3 only on `unreadable_pointers`; `test_cli_fold` grows from 9 to 13 tests (cursor-never-now, rerun-idempotence, capped-pass-is-a-remainder, zero-progress, concurrent-writer-refused-by-name); mutation (e)'s target line tracks the new `fold.run` call. Task 0 re-run green before filing.
- **r5 (2026-09-04, after round-4 CHANGES from both reviewers @ `21dc909c`):** (1) **Materialized the plan against itself** (Task 0) and fixed what actually failed — six failures, four more than predicted: a `/dev/stdout` constant the argv test did not allow; tuple-unpacked `RC_*` the definition scanner could not see; a `lambda` in `_render_open` (now `_row_sort_key`, a manifest name); `getattr` in `main` (now a shared parent parser, so `args.now`/`args.at` always exist); the wrapper rule flagging same-module one-liners (now scoped to cross-module delegation); and a `cli.py` node ceiling of 900 against a *measured* 1456 (now 1800, with the guarantee moved to G23). (2) **Launcher-alias bypass closed** (G22): launcher imports confined to `transport.py` per-module; launcher calls detected by resolving import *and assignment* aliases in the AST; forbidden subcommand tokens scanned in every module; mutation (b) in Task 1 is the exact `from subprocess import run as launch` form. (3) **Required call edges and owner-only operations** (G23): each handler must call its named owner operation and `cli.py` may never apply/empty/save a checkpoint, parse an event, or read events; mutation (e) in Task 3 inlines the fold into `cmd_fold`. (4) `cli.py` is a single tagged block; "add to cli.py" prose blocks are gone because a gate cannot see them. (5) G11 and Ruling 2 reworded to what is true: bounded per observed state, and detection rather than CAS. (6) **Found by Task 0 on r5 itself, before filing:** the materializer's fence regex spelled a literal fence marker and so truncated its own block (now built as `"`" * 3`); `_owed_row` returned one `None` for both *not owed* and *checkpoint unreadable*, so `claim/release/close` would have said *refused* on an UNKNOWN — it now returns `(row, load_state)` and the handlers exit 3 on `error`; `build_parser` exceeded the per-function budgets and is data-driven; the three budgets are set from the measured wiring (see G21).

## Self-review

1. **Spec coverage.** §3.1→T4. §3.2→G2, T7/T9 never list. §3.3→T6/T7. §3.4→T1 (corrected: the harness, not the type, is the proof). §4→T7/T10/T11. §5→T12–T14 + runbook. §6→verb table, T9 (`release`). §7→T1 DAG. §8→G9/G10, T11. §9→rulings + `release`. §1a→T0/T1/T2/T3.
2. **Placeholder scan.** The golden key set in T5 names its source file and line. Tasks 12–14 reference r3's code by commit rather than repeating it; they are old-side and outside Task 0's gate, and the plan says so. The proof's Linux profile is an open ask, named as such in G29 and the CI step.
3. **Type consistency.** `reader`/`writer` everywhere; `fold.run(..., now=, writer_id=, max_events=, verify_pointers=)` identical in T7's code and tests; `read_classified → (str|None, ReadState)` in T1/T5/T6/T7/T9; `write_event(cfg, payload, *, sender)` in T1/T7/T8; `checkpoint.path/empty/apply/load/save` identical in T6/T7; exit codes 0/2/3 in every verb; T3's manifest names exactly what T1/T4–T7 define, including `_row_sort_key`.
4. **Self-gate.** Task 0 was run against this file before filing; the output is in the filing note.
