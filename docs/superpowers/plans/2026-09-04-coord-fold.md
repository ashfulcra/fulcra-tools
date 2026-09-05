# coord-fold: Coord on Annotations Implementation Plan (r21)

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
- **G32.** *(r9; ruled `f6ceb0c4`; r12 per codex-coder round 11)* **No automated gate claims anti-consolidation, and the plan says so: *the gates do not check this*.** Seven rounds proved the automated form cannot close the class. **But the requester's acceptance condition stands** — *"if all five structural checks can pass with one big file, the plan is not ready"* — and a scope ruling on the bus does not waive it; codex-coder is right that recording the hole honestly is not closing it. So it is closed the only way the ruling allows: **a review-shaped ship gate (Task 16), bound to what ships** *(r13, both reviewers round 12)* — a required reviewer *reads the on-disk tree of the exact implementation commit* against a written rubric and files a verdict on a ship register keyed by that head, quoting the head and git's tree hash for the package; **any head change invalidates it, nothing carries forward from plan time, and the ship check refuses without both approvals on that exact head.** coord-boss ruled no waiver (`5ccfce26`). The automated checks that survive (symbols defined where planned, tree = manifest, DAG) are described as what they are: cheap facts, not the criterion. The answer to the review question is therefore: *yes, a ≤400-line `cli.py` behind owner shims would pass every automated check — and it would fail Task 16, which is a person or agent reading it.*
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

### Task 1: Package scaffold, fakes, boundary truths, and THE PROOF — the OS-sandboxed, process-boundary run (G5–G7, G29)

**Files:** `pyproject.toml`, `coord_fold/__init__.py`, `coord_fold/transport.py`, `tests/fakes.py`, `tests/proof/{store_server,fake_cli,inside,run_proof}.py`, `tests/test_structural.py`.

**Why this shape (r9).** r8's in-process harness was escaped in one round, four ways: the originals it saved were reachable through the suspended generator's frame via `gc`; `io.open` (and the rest of the low-level I/O surface) was not on its list and no list is ever complete; the fake leaked its corpus through `reader._s.events`, so the fold could enumerate without any patched callable; and the fold could tell it was under test. All four share a cause — *code running inside a Python process can reach anything in that process*. So the proof moves the two things that matter out of the process: **the corpus** (into a store server the sandbox cannot read, which logs and refuses) and **the denial** (into the kernel).

- [ ] **Step 1: The store, outside the sandbox**

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
sys.stdout.write(r["stdout"])
sys.stderr.write(r["stderr"])
sys.exit(r["rc"])
```

- [ ] **Step 2: What runs inside, and the driver**

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
    t("outbound socket 1.1.1.1:53", lambda: socket.create_connection(("1.1.1.1", 53), timeout=2))
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
(allow process-exec (subpath "{pyprefix}"))
(deny file-read-data (subpath "/Users") (subpath "/home") (subpath "/private/tmp") (subpath "/tmp") (subpath "/private/etc") (subpath "/etc") (subpath "/private/var") (subpath "/var") (subpath "/Applications") (subpath "/Volumes") (subpath "/Library"))
(allow file-read-data (subpath "{pyprefix}") (subpath "{pkg}") (subpath "{tmp}"))
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
    "/dev/stdout"
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
    "/dev/stdout"
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
    "/dev/stdout"
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
    "/dev/stdout"
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
    "/dev/stdout"
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
    "/dev/stdout"
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
    "/dev/stdout"
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
    "/dev/stdout"
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
    "/dev/stdout"
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
    "/dev/stdout"
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
    "/dev/stdout"
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
    "/dev/stdout"
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
    "/dev/stdout"
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
    profile.write_text(PROFILE.format(sock=sock, pyprefix=pyprefix, pkg=os.path.realpath(PKG), tmp=tmp))
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

- [ ] **Step 3: Boundary truths — cheap and true, kept**

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
    from fakes import FakeReader, FakeStore, FakeWriter
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
    data = tomllib.loads((PKG_DIR.parent / "pyproject.toml").read_text())
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

- [ ] **Step 4: Scaffold** (unchanged from r7; the transport keeps `pathlib` and never imports `os`)

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
        try:
            p = subprocess.run([*self._cli, "file", "download", path, "/dev/stdout"], capture_output=True, text=True, timeout=self._timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return 127, "", str(exc)
        return p.returncode, p.stdout, p.stderr

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
# packages/coord-fold/tests/fakes.py
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
        self.fail_reads = False
        self.fail_events = False


class FakeReader:
    def __init__(self, store: FakeStore) -> None:
        self._s = store

    def read_classified(self, path: str):
        if self._s.fail_reads:
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

- [ ] **Step 5: Run.** Boundary truths: 8 pass now. The proof needs Tasks 4–10 (there is no fold yet): `python tests/proof/run_proof.py` → exit 1 until then, exit 3 on a host with no sandbox — **commit failing-first**. Measured on this host at plan time, against the materialized tree: the unit suite passes *inside* the sandbox; proof phase 1 — seven verb invocations rc 0, 36 requests, shapes exactly the five, the first `get-records` read the 5000-record corpus once and the second asked from the last observed record minus the overlap and got 10 back (bound 10); phase 2 — every attack denied or refused (the direct-socket `file list` logged and refused by the store); phase 3 — the mutated fold's `file list` flagged; phase 4 — the epoch-rewritten production reader flagged by semantics (5006 and 5008 returned against the bound) with every verb name allowed; phase 5 — a production reader point-probing 2000 guessed task paths flagged (paths outside the allowlist, `file stat` count over its bound, sequence mismatch). The clean run's exact request sequence (36 entries, paths included) is frozen in the driver from this measurement. **Mutations for the truths** (each restored): (a) give `CliPointerReader` a base class → FAILS; (b) `def _upload` on the reader → FAILS; (c) `from coord_engine import x` anywhere → FAILS; (d) an extra file under `coord_fold/` → FAILS. **Commit** — `coord-fold: scaffold, boundary truths, and the OS-sandboxed process-boundary proof (G5–G7, G29)`

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
    assert f"{CEILING} lines" in (PKG_DIR.parent / "README.md").read_text()
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
from fakes import FakeReader, FakeStore

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
from fakes import FakeReader, FakeStore, FakeWriter
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
from fakes import FakeReader, FakeStore, FakeWriter

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

Run — 14 passed. Mutations: (g) `if not unread: last_observed = at` → `pass` → other-agents-traffic FAILS (G31); (a) swallow `TransportUnavailable` in the read loop → failed-read test FAILS; (b) drop the addressee filter → someone-else test FAILS; (c) force `verify_pointers=False` → absent-pointer test FAILS; (d) `state["cursor"] = now` → cursor-never-now FAILS (Ruling 1); (e) skip the re-read → concurrent-writer FAILS (Ruling 2); (f) treat the cap as rc 3 → capped-pass FAILS (Ruling 4). **Commit** — `coord-fold: fold verb, six-verb cli wiring, --verify-pointers (G9, G20, G23)`

---

### Task 8: `emit` tests

```python
# packages/coord-fold/tests/test_cli_emit.py
import json
from coord_fold import checkpoint as cp
from coord_fold.cli import main
from fakes import FakeReader, FakeStore, FakeWriter
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
from fakes import FakeReader, FakeStore, FakeWriter
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
    st.fail_reads = True; assert _m(st, ["close", "r", "s", "--agent", "me", "--evidence", "x.md"]) == 3
    assert "unreadable" in capsys.readouterr().err
```

Run — 5 passed. Mutation: make `cmd_close`'s `st == "error"` branch fall through → FAILS. **Commit.**

---

### Task 10: `status` tests

```python
# packages/coord-fold/tests/test_cli_status.py
import json
from coord_fold import checkpoint as cp
from coord_fold.cli import main
from fakes import FakeReader, FakeStore, FakeWriter


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
**14 — Comparator + `cutover-ready` + runbook**: tuples `(slug, pri, ptr)`; `AGREE n=k` / `DIVERGE slugs=[…]`; `cutover-ready` exits 0 only if trailing AGREE run ≥ 24, span ≥ 24h, the new open set both grew and shrank within it, **the injected divergence/recovery drill was performed and recorded (G13), and `scripts/ship_check.py fulcra <HEAD>` exits 0 (Task 16)**. Mutation: force the 24h check true → the one-minute-apart test FAILS.

### Task 15: AGENTS.md (ship-gate)

The package, its four gate files, the proof driver and its two CI steps (the proof needs a macOS runner; exit 3 is UNKNOWN, not green); six verbs and exit codes; the remainder-vs-unknown distinction (G25) and the `degraded` ban; dependency direction; the reader/writer boundary; **the guarantee in G29's words** ("in that run the fold reached no store except through observed requests") with what it does not claim, the sandbox profile, and the store server's contract; **the tripwire's demotion in G30's words**; the cursor rules (G26/G31); §3.4 of the spec corrected (a type stops a method call, not `os.listdir`); and G24 — a revision is filed only with Task 0 green.

### Task 16: Review-shaped ship gate, bound to the shipped commit — responsibility distribution (G32)

**Not an automated gate, and not a plan-time approval.** Both reviewers (round 12): a verdict filed against the plan head and "carried" to a later implementation commit is stale evidence — one big file could ship behind it. So:

1. **When.** Only after implementation, on the **exact implementation commit** `<HEAD>` (40-hex) whose on-disk `packages/coord-fold/` is what ships. Never at plan time. Reading the materialized plan tree earlier is welcome as *feedback* and **carries nothing**.
2. **Register.** `coord-engine review request fulcra coord-fold-ship-<HEAD> --of packages/coord-fold --head <HEAD> --reviewer codex-reviewer --reviewer codex-coder`. The engine's `--head` keying means any head change is a new round with no verdicts.
3. **What the reviewer reads and files.** `git checkout <HEAD>`; read the on-disk tree against the rubric below; file with the **typed verb, nothing hand-uploaded**: `coord-engine review verdict fulcra coord-fold-ship-<HEAD> --head <HEAD> --verdict approve --from <reviewer> --note "tree: <git rev-parse <HEAD>:packages/coord-fold> …reading…"`. The engine writes an **append-only envelope** `verdicts/<HEAD>--<reviewer>--<UTC timestamp>-<nonce>.md` (as every verdict on this plan's register demonstrates); the `tree:` line in the note is the evidence. A verdict whose `tree` differs from the commit's is void.
4. **Ship check.** `scripts/ship_check.py <team> <HEAD>` exits 0 only if: the working tree is at `<HEAD>` and clean for the package; **the engine's folded result** (`review status --json`) is `APPROVED` for that exact head with both required reviewers in `approvals`; and, for each required reviewer, **the exact winning shard the fold kept — `winning[reviewer].name` in that JSON — ** says `approve` and quotes the commit's tree hash. *(r15, both reviewers round 14: same-second shards were ordered by digest, so a refolded "latest" could be an earlier APPROVE; the ship check now never refolds filenames.)* **Engine prerequisite — bound to an APPROVED AND PINNED engine, never to a named commit** *(r16; codex-reviewer round 13 P0 one, verified at source by coord-boss `149e7d11`: the commit r15 named still had the double clock sample, so a named prerequisite bought nothing)*: `ship_check` downloads the fleet pin from `team/<team>/_coord/bus-v3/records.json`'s sibling `adopt-latest.sh` (the plan's own rule: pins come from there, never from a slug) and requires `PIN ∈ APPROVED_ENGINE_PINS` — a list in the script that is **empty until a deliberate plan revision adds the head that register `review-winning-envelope-e9c0089b` reads APPROVED for and that the pin PR shipped**. Until then `ship_check` refuses, which is the correct state. **And the pin must be the engine that answers** *(r17, codex-reviewer round 14: on a lagging host the authority can name an approved pin while `PATH` still executes an older engine that exposes `winning` with stale-approval defects)*: `ship_check` resolves the `coord-engine` executable it will call, reads `vcs_info.commit_id` from the `direct_url.json` beside the installed `coord_engine-*.dist-info` — the build-identity mechanism `adopt-latest.sh` itself uses — and refuses unless that commit equals the pin; no executable, no `direct_url.json`, or a different commit is a refusal **before** `winning` is consumed. **And the module that answers must be that build** *(r18, codex-coder round 15, reproduced end to end by coord-boss `8268376f`: a pinned launcher answered with a capability its build lacks because `subprocess.run` inherits `PYTHONPATH`/`PYTHONHOME` and an editable tree shadowed the installed package while `importlib.metadata` still reported the approved commit)*: `ship_check` resolves the executable **once, in `main`**, and passes that absolute path to *both* the identity read and every invocation — a bound runner, so no second `which` can ever run *(r19, both reviewers round 16: r18 resolved twice, and a `PATH` swap between the identity read and `review status` would let approved launcher A authorise unapproved launcher B; the regression makes `which` answer A then B and asserts exactly one resolution and that A is what executes)* — and *(r20, codex-coder round 17: scrubbing the environment blocks `PYTHONPATH`/`PYTHONHOME` only; a `.pth` or `sitecustomize.py` inside the launcher's own environment can prepend a stale tree while the adjacent dist-info still names the pin — on the proof host that site-packages already carries a `_virtualenv.pth`)* **the process that answers attests itself**: `ship_check` never runs the launcher for the status. It spawns the launcher environment's own interpreter with **`-I -S`** (no environment variables, no user site, no `.pth` processing, no `sitecustomize`/`usercustomize`), inserts exactly the verified dist's site-packages on `sys.path`, imports `coord_engine`, and in that same process reports `coord_engine.__file__`, the `direct_url` commit via `importlib.metadata`, and `review status --json` computed in-process by `coord_engine.cli.main`. It refuses unless the answering module's file lies under that site-packages **and** the in-process commit equals the pin **and** *(r21, both reviewers round 18)* the attestation process exited 0, the in-process `review status` returned 0, and the payload is a dict of the expected shape — a status that says APPROVED while returning rc 3 (UNKNOWN) is a refusal, exactly as r19's `rc == 0` guard had it before the attestation replaced the direct call. Measured on the proof host: the isolated attestation imports from under site-packages, reports `985a4be3` (the fleet pin), and answers the status. Regressions: a shadow `coord_engine` on `PYTHONPATH` is imported under the inherited environment and not under the scrubbed one; and a tool environment whose site-packages holds both a `.pth` and a `sitecustomize.py` prepending a shadow imports the shadow under a normal site-enabled start and the approved package under the attestation. `winning` in `review status --json` is then the **supersession fold's** kept shard, under whatever contract the engine register's APPROVED head carries — at round 8 that contract is: any CHANGES not resolved by a later shard dominates regardless of timestamp; an APPROVE lifts a CHANGES only by an edge that binds the target's **content digest** (so an in-place rewrite of a mutable shard un-resolves it) **and** whose target the **store's server-assigned mtime** proves strictly earlier than the superseder (so a predeclared edge to a later-written target never resolves; same minute or unknown fails closed). Rounds 6 and 7 called a name, then a client-written nonce, "causal"; both were wrong, because both were client-controlled — only the store supplies facts the client cannot choose. Self-links, dangling names, digest mismatches, equal keys and unproven causality fail closed to CHANGES and are surfaced as `malformed_supersedes`. **Both authoritative filename forms are accepted** *(P0 two, confirmed live on this very register: `<HEAD>--<reviewer>.md` and `<HEAD>--<reviewer>--<ts>-<digest>.md` coexist on the current head)*: a winning name is valid if it is exactly `<HEAD>--<reviewer>.md` or starts with `<HEAD>--<reviewer>--`; the fold, not the gate, decides which won. Any absence — no `winning`, no fold, a pin not in the approved set, an unreadable shard — is a refusal. Task 14's `cutover-ready` **calls it as `scripts/ship_check.py fulcra <HEAD>` and fails closed** — no cutover without it. `tests/test_ship_check.py` drives the script end to end with real envelope names for every outcome.

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
Usage: python scripts/ship_check.py <team> <40-hex head>"""
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys

REQUIRED = ("codex-reviewer", "codex-coder")
# Engine heads whose register `review-winning-envelope-e9c0089b` read APPROVED AND whose pin PR shipped.
# EMPTY until a deliberate plan revision adds one; an empty set means ship_check refuses, correctly.
APPROVED_ENGINE_PINS: frozenset = frozenset()


IMPORT_AFFECTING = ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PYTHONUSERBASE", "PYTHONSAFEPATH", "VIRTUAL_ENV", "CONDA_PREFIX")


def engine_env():
    """The environment the engine is invoked with: NOTHING that can change which coord_engine imports.
    (coord-boss 8268376f: a pinned launcher answered with the working tree's capabilities because
    subprocess.run inherited PYTHONPATH, while its metadata still named the approved pin.)"""
    env = {k: v for k, v in os.environ.items() if k not in IMPORT_AFFECTING and not k.startswith("PYTHON")}
    env["PYTHONNOUSERSITE"] = "1"
    return env


def engine_executable():
    """Resolved ONCE, by main, to an absolute path. Nothing else may call `which`: the identity read and
    every invocation receive the SAME path (both reviewers, round 16: two resolutions let approved
    launcher A authorise unapproved launcher B after a PATH swap)."""
    exe = shutil.which("coord-engine")
    return os.path.realpath(exe) if exe else None


def sh(*argv):
    p = subprocess.run(list(argv), capture_output=True, text=True)
    return p.returncode, p.stdout.strip(), p.stderr.strip()


ATTEST = r"""
import sys, json, io, contextlib, os
site = sys.argv[1]; team = sys.argv[2]; slug = sys.argv[3]
sys.path.insert(0, site)
import importlib.metadata as md
import coord_engine
from coord_engine import cli
du = json.loads(md.distribution("coord-engine").read_text("direct_url.json") or "{}")
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    rc = cli.main(["review", "status", team, slug, "--json"])
lines = [l for l in buf.getvalue().splitlines() if l.startswith("{")]
print(json.dumps({"file": os.path.realpath(coord_engine.__file__), "commit": du.get("vcs_info", {}).get("commit_id"),
                  "rc": rc, "status": json.loads(lines[-1]) if lines else None}))
sys.exit(rc)                      # the outer process carries the inner verdict's rc too; both are checked
"""


def engine_python(exe):
    """The launcher environment's own interpreter (uv tool env: <env>/bin/python)."""
    py = pathlib.Path(exe).parent / "python"
    return str(py) if py.exists() else None


def dist_site_packages(exe):
    """The site-packages that holds the coord_engine dist-info beside `exe` — the ONLY path the attestation may import from."""
    root = pathlib.Path(exe).parent.parent
    for di in sorted(root.glob("lib/python*/site-packages/coord_engine-*.dist-info")):
        return os.path.realpath(di.parent)
    return None


def attested_status(exe, team, slug):
    """The status, from a process that PROVES what answered it (codex-coder, round 17): the launcher
    env's interpreter under -I -S (no env, no user site, no .pth, no sitecustomize), exactly one path
    on sys.path — the verified dist's site-packages — and the fold computed in that same process.
    -> (ok, detail, status_dict_or_None)."""
    py, site = engine_python(exe), dist_site_packages(exe)
    if not py or not site:
        return False, f"no interpreter/site-packages beside {exe}", None
    p = subprocess.run([py, "-I", "-S", "-c", ATTEST, site, team, slug], capture_output=True, text=True, env=engine_env())
    try:
        a = json.loads([l for l in p.stdout.splitlines() if l.startswith("{")][-1])
    except (ValueError, IndexError):
        return False, f"attestation did not answer (rc {p.returncode}): {p.stderr.strip()[-200:]}", None
    if not isinstance(a, dict):
        return False, "attestation payload is not an object", None
    if not str(a.get("file", "")).startswith(site + os.sep):
        return False, f"the module that answered lives at {a.get('file')!r}, not under {site} — a startup hook or shadow tree answered", None
    # BOTH exit codes, before any status is trusted (both reviewers, round 18): a status that
    # prints an APPROVED-shaped tally while returning rc 3 is UNKNOWN, not approval.
    if p.returncode != 0 or a.get("rc") != 0:
        return False, f"the attested review status returned rc {a.get('rc')!r} (process rc {p.returncode}) — UNKNOWN is not approval", None
    status = a.get("status")
    if not isinstance(status, dict) or not isinstance(status.get("state"), str) or not isinstance(status.get("approvals"), list) or not isinstance(status.get("head"), str):
        return False, "the attested status is not a review tally of the expected shape", None
    return True, a.get("commit"), status


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
    rc, body, _ = sh("fulcra-api", "file", "download", f"team/{team}/_coord/bus-v3/adopt-latest.sh", "/dev/stdout")
    m = re.search(r'^PIN="([0-9a-f]{40})"', body, re.M) if rc == 0 else None
    return m.group(1) if m else None


def winning_name_ok(name: str, head: str, reviewer: str) -> bool:
    """Both authoritative forms: the exact-head plain shard, or an append-only envelope."""
    return name == f"{head}--{reviewer}.md" or name.startswith(f"{head}--{reviewer}--")


def main(team: str, head: str) -> int:
    if not re.fullmatch(r"[0-9a-f]{40}", head):
        print("ship_check: head must be a 40-hex commit"); return 1
    pin = fleet_pin(team)
    if pin is None or pin not in APPROVED_ENGINE_PINS:
        print(f"ship_check: fleet engine pin {pin!r} is not an APPROVED+PINNED corrected engine (approved set: {sorted(APPROVED_ENGINE_PINS)}) — refusing; the fold's ordering contract is not proven on this engine"); return 1
    exe = engine_executable()                                           # THE one resolution
    if not exe:
        print("ship_check: coord-engine not found on PATH — refusing"); return 1
    local = executing_engine_commit(exe)
    if local != pin:
        print(f"ship_check: the coord-engine at {exe} is build {local!r}, not the approved pin {pin} — refusing; a lagging host must not trust its own unapproved fold"); return 1
    slug = f"coord-fold-ship-{head}"
    ok, detail, fold = attested_status(exe, team, slug)                 # the answering process attests itself
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
        rc, body, err = sh("fulcra-api", "file", "download", f"team/{team}/review/{slug}/verdicts/{name}", "/dev/stdout")
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
    raise SystemExit(main(sys.argv[1], sys.argv[2]))
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
        if a[:3] == ["fulcra-api", "file", "download"] and a[3].endswith("adopt-latest.sh"):
            return (0, f'#!/bin/sh\nPIN="{pin}"   # coord-engine\n', "") if pin else (1, "", "Error: File not found")
        if a[:3] == ["fulcra-api", "file", "download"]:
            n = a[3].rsplit("/", 1)[-1]
            return (0, bodies[n], "") if n in bodies else (1, "", "Error: File not found")
        raise AssertionError(a)
    return fake_sh


def _attest_from(fake, attested_commit=PIN):
    def attested(exe, team, slug):
        rc, out, _ = fake("coord-engine", "review", "status", team, slug, "--json")
        return True, attested_commit, json.loads(out)
    return attested


def run(monkeypatch, local=PIN, attested_commit=PIN, **kw):
    _approve_pin(monkeypatch, local=local)
    fake = world(**kw)
    monkeypatch.setattr(ship_check, "sh", fake)
    monkeypatch.setattr(ship_check, "attested_status", _attest_from(fake, attested_commit))
    return ship_check.main("fulcra", HEAD)


def test_the_answering_process_reporting_another_build_refuses(monkeypatch, capsys):
    assert run(monkeypatch, attested_commit="8d0ed90e000185ca9fc71bc3a95983869d120bbf") == 1
    assert "process that answered reports build" in capsys.readouterr().out


def test_a_pth_or_sitecustomize_shadow_wins_under_site_and_loses_under_the_attestation(tmp_path):
    """codex-coder round 17: env scrubbing does not stop startup hooks INSIDE the launcher environment.
    Build one with an approved dist-info AND a .pth AND a sitecustomize that prepend a shadow tree."""
    import subprocess, sys
    env = tmp_path / "coord-engine"; (env / "bin").mkdir(parents=True)
    (env / "bin" / "python").symlink_to(sys.executable)
    launcher = env / "bin" / "coord-engine"; launcher.write_text("#!/bin/sh\n"); launcher.chmod(0o755)
    site = env / "lib" / "python3.13" / "site-packages"; (site / "coord_engine").mkdir(parents=True)
    (site / "coord_engine" / "__init__.py").write_text("WHO = 'APPROVED'\n")
    (site / "coord_engine" / "cli.py").write_text("import json\ndef main(argv):\n    print(json.dumps({'state': 'APPROVED', 'head': argv[3].rsplit('-', 1)[-1], 'approvals': ['codex-reviewer', 'codex-coder'], 'winning': {}}))\n    return 0\n")
    di = site / "coord_engine-2.0.6.dist-info"; di.mkdir()
    (di / "METADATA").write_text("Metadata-Version: 2.1\nName: coord-engine\nVersion: 2.0.6\n")
    (di / "direct_url.json").write_text(json.dumps({"url": "x", "vcs_info": {"vcs": "git", "commit_id": PIN}}))
    shadow = tmp_path / "shadow"; (shadow / "coord_engine").mkdir(parents=True)
    (shadow / "coord_engine" / "__init__.py").write_text("WHO = 'SHADOW'\n")
    (site / "zzz_shadow.pth").write_text(f"import sys; sys.path.insert(0, {str(shadow)!r})\n")
    (site / "sitecustomize.py").write_text(f"import sys; sys.path.insert(0, {str(shadow)!r})\n")
    # the hole: a normal, site-enabled start of the SAME interpreter with this site-packages
    hole = subprocess.run([sys.executable, "-c", f"import site; site.addsitedir({str(site)!r}); import coord_engine; print(coord_engine.WHO)"],
                          capture_output=True, text=True, env=ship_check.engine_env()).stdout.strip()
    assert hole == "SHADOW"
    # the fix: the attestation under -I -S with exactly the verified site-packages
    ok, commit, status = ship_check.attested_status(str(launcher), "fulcra", f"coord-fold-ship-{HEAD}")
    assert ok and commit == PIN and status["state"] == "APPROVED"


def _tool_env(tmp_path, cli_body):
    """A minimal uv-style tool environment: bin/python -> this interpreter, one fake coord_engine, an approved dist-info."""
    import sys
    env = tmp_path / "coord-engine"; (env / "bin").mkdir(parents=True); (env / "bin" / "python").symlink_to(sys.executable)
    launcher = env / "bin" / "coord-engine"; launcher.write_text("#!/bin/sh\n"); launcher.chmod(0o755)
    site = env / "lib" / "python3.13" / "site-packages"; (site / "coord_engine").mkdir(parents=True)
    (site / "coord_engine" / "__init__.py").write_text("")
    (site / "coord_engine" / "cli.py").write_text(cli_body)
    di = site / "coord_engine-2.0.6.dist-info"; di.mkdir()
    (di / "METADATA").write_text("Metadata-Version: 2.1\nName: coord-engine\nVersion: 2.0.6\n")
    (di / "direct_url.json").write_text(json.dumps({"vcs_info": {"commit_id": PIN}}))
    return launcher


def test_an_approved_shaped_status_that_returns_rc_3_is_refused(tmp_path):
    """Both reviewers, round 18: the inner verdict's rc was recorded and never checked."""
    launcher = _tool_env(tmp_path, "import json\ndef main(argv):\n    print(json.dumps({'state': 'APPROVED', 'head': 'x', 'approvals': ['codex-reviewer', 'codex-coder'], 'winning': {}}))\n    return 3\n")
    ok, detail, status = ship_check.attested_status(str(launcher), "fulcra", f"coord-fold-ship-{HEAD}")
    assert not ok and "rc 3" in detail and status is None


def test_a_status_of_the_wrong_shape_is_refused(tmp_path):
    launcher = _tool_env(tmp_path, "def main(argv):\n    print('[1, 2]')\n    return 0\n")
    ok, detail, _ = ship_check.attested_status(str(launcher), "fulcra", f"coord-fold-ship-{HEAD}")
    assert not ok and "expected shape" in detail


def test_the_attestation_refuses_a_module_answering_from_outside_the_verified_site(monkeypatch, tmp_path):
    import subprocess, sys
    env = tmp_path / "coord-engine"; (env / "bin").mkdir(parents=True); (env / "bin" / "python").symlink_to(sys.executable)
    launcher = env / "bin" / "coord-engine"; launcher.write_text("#!/bin/sh\n"); launcher.chmod(0o755)
    site = env / "lib" / "python3.13" / "site-packages"; (site / "coord_engine-2.0.6.dist-info").mkdir(parents=True)
    (site / "coord_engine-2.0.6.dist-info" / "METADATA").write_text("Metadata-Version: 2.1\nName: coord-engine\nVersion: 2.0.6\n")
    (site / "coord_engine-2.0.6.dist-info" / "direct_url.json").write_text(json.dumps({"vcs_info": {"commit_id": PIN}}))
    elsewhere = tmp_path / "elsewhere"; (elsewhere / "coord_engine").mkdir(parents=True)
    (elsewhere / "coord_engine" / "__init__.py").write_text("")
    (elsewhere / "coord_engine" / "cli.py").write_text("def main(argv):\n    print('{}')\n    return 0\n")
    # no coord_engine package under site — an attestation that imports from elsewhere must be refused
    monkeypatch.setattr(ship_check, "ATTEST", ship_check.ATTEST.replace("sys.path.insert(0, site)", f"sys.path.insert(0, site); sys.path.insert(0, {str(elsewhere)!r})"))
    ok, detail, _ = ship_check.attested_status(str(launcher), "fulcra", f"coord-fold-ship-{HEAD}")
    assert not ok and "not under" in detail


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
    def attested(exe, team, slug):
        executed.append(exe); rc, out, _ = fake("coord-engine", "review", "status", team, slug, "--json"); return True, PIN, json.loads(out)
    monkeypatch.setattr(ship_check, "attested_status", attested)
    assert ship_check.main("fulcra", HEAD) == 0
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
    coord_engine answers. Under ship_check's env the site-packages build (the one the identity check proved) wins."""
    import subprocess, sys
    site = tmp_path / "env" / "lib" / "python3.13" / "site-packages"; (site / "coord_engine").mkdir(parents=True)
    (site / "coord_engine" / "__init__.py").write_text("WHO = 'PINNED'\n")
    shadow = tmp_path / "shadow"; (shadow / "coord_engine").mkdir(parents=True)
    (shadow / "coord_engine" / "__init__.py").write_text("WHO = 'SHADOW'\n")
    launcher = tmp_path / "env" / "bin" / "coord-engine"; launcher.parent.mkdir(parents=True)
    launcher.write_text(f"#!{sys.executable}\nimport sys, site\nsite.addsitedir({str(site)!r})\nimport coord_engine\nprint(coord_engine.WHO)\n"); launcher.chmod(0o755)
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


def test_the_shipped_default_approved_set_is_empty_so_ship_check_refuses_until_a_revision_adds_a_pin(monkeypatch, capsys):
    monkeypatch.setattr(ship_check, "sh", world())
    assert ship_check.APPROVED_ENGINE_PINS == frozenset()
    assert ship_check.main("fulcra", HEAD) == 1 and "not an APPROVED+PINNED" in capsys.readouterr().out


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
```


---

## Rulings (decided by coord-boss, directive 722f8f29, 2026-09-04T21:17Z) — reasoning attached

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

- **r1–r4:** see `6e0d42e5`/`21dc909c` history. r4 was a coherent rewrite after codex-coder's round 3.
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
