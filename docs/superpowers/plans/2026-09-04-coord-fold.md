# coord-fold: Coord on Annotations Implementation Plan (r8)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Spec:** `docs/superpowers/specs/2026-09-04-coord-annotation-bus-design.md` (branch `claude/coord-boss-handoff-resume-60sjua` @ `3a4687b0`). Read it whole first; §1 and §1a are why every structural requirement below exists.

**Directive:** coord-boss `65761fbd` (P0). **Reviewers (both required):** codex-reviewer, codex-coder. **Implementer:** coord-maintainer.

**r5 is gated against itself before it is filed.** Round-4 verdicts (both reviewers) found the plan's structural suite failed the plan's own code. Task 0 below is a script that materializes every path-tagged code block in this document into a scratch tree and runs the structural gates against it; r5 was filed only after that run was green, and the run's output is in the filing note. Every code block that lands in the package is tagged `# packages/coord-fold/<path>`; a block that is not tagged does not exist as far as the gate is concerned, so it is not allowed to matter.

**Goal:** A separate `coord-fold` package whose fold engine is *proven not to enumerate* — **we do not claim enumeration is impossible to write; we claim a fold that enumerates fails its tests** (G29) — running in parallel with the old bus until a comparator proves agreement, then cut over. Seven rounds of syntactic gates (r2–r7) each closed one spelling and left the class open; coord-boss ruled (`cdfe666e`) that a static check cannot prove a negative about an unrestricted Python module, and the spec's §3.4 claim that a transport type *prevents* enumeration was wrong: a type stops a method call, not `os.listdir` or a subprocess. The guarantee is now behavioural.

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
- **G13.** Parallel bus proven then cut over: seed, dual-emit, shadow, cut over after N agreeing passes spanning ≥24h with observed transitions, freeze.
- **G14.** coord-boss alone first, then one agent at a time.
- **G15.** Never hardcode the channel; resolve `data_type` from `team/<team>/_coord/bus-v4/records.json`.
- **G16.** No secrets. **G17.** Commits authored as `114089064+ashfulcra@users.noreply.github.com`.
- **G18–G23.** *(r2–r7)* **SUPERSEDED by G29** (coord-boss `cdfe666e`, 2026-09-04): ownership manifest, import DAG, closed call allowlist, alias-resolved launcher/enumeration/owner-op bans, required-call shape, mass ceilings. Each closed a specific counterexample (r2 shim modules, r3 one-statement wrappers, r4 `from subprocess import run as launch`, r5 `os.listdir` inside the allowlisted module + alias-bound owner ops + unreachable `fold.run`, r7 `x: object = reader.read_events` — `AnnAssign`, which no alias map handled) and left the class open. The cheap, *true* structural facts among them survive as boundary truths in Task 1 (G5–G7, ownership-defined-where-planned, DAG, tree = manifest); the rest are deleted, not kept as ceremony. Numbers retained so old verdicts still resolve.
- **G24.** *(r5)* **The plan is gated against itself.** Task 0's script materializes every path-tagged block and runs Tasks 1–3's tests; a revision is filed only with that run green, and the filing note carries the output.

- **G29.** *(r8, coord-boss `cdfe666e`)* **THE GUARANTEE IS BEHAVIOURAL.** Every fold test (G9) runs inside a harness where enumeration, process launch, file open and network are **absent from the test process** — `os`/`posix` listdir/scandir/walk/exec*/spawn*/fork, `subprocess.*`, `_posixsubprocess.fork_exec`, `pathlib.Path.iterdir/glob/rglob/walk/open`, `glob.*`, `tempfile.*`, `builtins.open`, `socket.socket` are patched to **raise a `BaseException` and count**, `ctypes`/`pty`/`multiprocessing` cannot be imported, and the package is **imported fresh under denial** so a reference bound at import time (`from posix import listdir`) binds the stub. The store fake serves reads only. Asserted both ways: the real fold and all six verbs complete with **zero attempts**; a deliberately enumerating, launching, opening, or globbing reader **raises** and is counted — including one that swallows `Exception` (the raise is a `BaseException`) and one spelled with an annotated assignment (codex-coder's r7 bypass). Found while building it: **the import machinery itself enumerates** (`FileFinder._fill_cache` → `posix.listdir`), so under denial a module whose directory is not already cached cannot be imported at all — a generated module (r2's "code from data" bypass) is closed by construction, and the harness pre-warms the package's finder caches before patching so the result does not depend on test order. No alias, annotation, generated module, or spelling defeats it, because the capability is absent rather than unnamed. **Reviewers are invited to attack the harness**: the open question is whether the sandbox is escapable, a far smaller surface than every way to spell a call.
- **G30.** *(r8)* **One syntactic tripwire stays, demoted.** `tests/test_tripwire.py` scans identifiers for fast feedback in review. **It is not the guarantee and no reader may cite it as proof**; it approximates behaviour and can be walked past by any spelling the AST does not resolve. G29 is the guarantee. The plan says this in those words so no future reader mistakes it.
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
    fakes.py                          FakeStore / FakeReader / FakeWriter (reads only from the store)
    harness.py                        denied(): the capability-absent test process; fresh(): import under denial   (G29)
    early_binder.py                   a module that binds `from posix import listdir` at import time      (G29 fixture)
    test_no_enumeration_harness.py    THE GUARANTEE, both ways                                           (G29)
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

TAG = re.compile(r"#\s*(packages/coord-fold/\S+)")
TICKS = "`" * 3                                  # never spelled literally: this file lives inside a fence
FENCE = re.compile(TICKS + r"(python|toml)\n(.*?)" + TICKS, re.S)
GATES = ["tests/test_no_enumeration_harness.py", "tests/test_structural.py", "tests/test_tripwire.py",
         "tests/test_file_size_ceiling.py", "tests/test_no_degraded_vocabulary.py"]


def materialize(plan: str, out: pathlib.Path) -> tuple[list[str], list[str]]:
    written, untagged = [], []
    for _lang, body in FENCE.findall(plan):
        first, _, rest = body.partition("\n")
        m = TAG.match(first)
        if not m:
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
    rc = subprocess.run([sys.executable, "-m", "pytest", *GATES, "-q"], cwd=pkg,
                        env={"PYTHONPATH": ".:tests", "PATH": "/usr/bin:/bin"}).returncode
    return rc


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
```

- [ ] **Step 2: Run it against this plan.** `python packages/coord-fold/scripts/materialize_plan.py docs/superpowers/plans/2026-09-04-coord-fold.md /tmp/coord-fold-gate` — expected: `untagged: 0` and the four gate files pass. **This is the filing precondition for every revision.** (The script lives under `scripts/`, outside `coord_fold/`, so the recursive artifact scan does not count it.)

---

### Task 1: Package scaffold, fakes, boundary truths, and THE GUARANTEE — the capability-absent harness (G5–G7, G29)

**Files:** `pyproject.toml`, `coord_fold/__init__.py`, `coord_fold/transport.py`, `tests/fakes.py`, `tests/harness.py`, `tests/early_binder.py`, `tests/test_no_enumeration_harness.py`, `tests/test_structural.py`.

- [ ] **Step 1: The harness — the guarantee lives here**

```python
# packages/coord-fold/tests/harness.py
"""G29: the guarantee is behavioural.

Inside `denied()`, enumeration, process launch, file open and network are ABSENT from the test
process: patched at the capability to raise a BaseException and count. `fresh()` imports a module
UNDER denial so a reference bound at import time binds the stub. We do not claim enumeration is
impossible to write; we claim a fold that enumerates fails its tests.

Reviewers: attack THIS. The question is whether the sandbox is escapable — a far smaller surface
than every way to spell a call.

Found while building it: the import machinery itself enumerates (`FileFinder._fill_cache` calls
`posix.listdir`), so under denial a module whose directory finder is not already cached CANNOT BE
IMPORTED — a generated module (the r2 "code from data" bypass) is closed by construction. `denied()`
therefore pre-warms the finder caches for the package and the tests directory BEFORE patching, so
`fresh()` is independent of test order.
"""
from __future__ import annotations

import builtins
import contextlib
import glob
import importlib
import os
import pathlib
import socket
import subprocess
import sys
import tempfile

try:
    import posix as _posix
except ImportError:  # pragma: no cover
    _posix = None
try:
    import _posixsubprocess as _ps
except ImportError:  # pragma: no cover
    _ps = None


class CapabilityAbsent(BaseException):
    """BaseException on purpose: `except Exception` in the code under test cannot swallow it."""


OS_NAMES = ("listdir", "scandir", "walk", "fwalk", "listxattr", "system", "popen", "fdopen", "fork", "forkpty",
            "execl", "execle", "execlp", "execlpe", "execv", "execve", "execvp", "execvpe",
            "spawnl", "spawnle", "spawnlp", "spawnlpe", "spawnv", "spawnve", "spawnvp", "spawnvpe", "posix_spawn", "posix_spawnp")
SUBPROCESS_NAMES = ("run", "Popen", "call", "check_call", "check_output", "getoutput", "getstatusoutput")
PATH_NAMES = ("iterdir", "glob", "rglob", "walk", "open")
GLOB_NAMES = ("glob", "iglob")
TEMPFILE_NAMES = ("NamedTemporaryFile", "TemporaryFile", "SpooledTemporaryFile", "TemporaryDirectory", "mkstemp", "mkdtemp")
SOCKET_NAMES = ("socket", "create_connection")
BLOCKED_MODULES = ("ctypes", "pty", "multiprocessing")
PACKAGE = "coord_fold"


class Denial:
    def __init__(self) -> None:
        self.attempts: list[str] = []

    def stub(self, label: str):
        def _absent(*a, **k):
            self.attempts.append(label)
            raise CapabilityAbsent(f"{label} is absent in this run")
        return _absent


def _patch(target, names, denial, saved) -> None:
    owner = getattr(target, "__name__", type(target).__name__)
    for n in names:
        if hasattr(target, n):
            saved.append((target, n, getattr(target, n)))
            setattr(target, n, denial.stub(f"{owner}.{n}"))


def _purge_package() -> dict:
    return {k: sys.modules.pop(k) for k in list(sys.modules) if k == PACKAGE or k.startswith(PACKAGE + ".")}


def _prewarm() -> None:
    """Fill the finder caches for the package and the tests directory while enumeration still exists."""
    importlib.import_module(PACKAGE + ".cli")
    importlib.import_module("early_binder")


@contextlib.contextmanager
def denied():
    denial, saved, blocked = Denial(), [], {}
    _prewarm()
    _patch(os, OS_NAMES, denial, saved)
    if _posix is not None:
        _patch(_posix, OS_NAMES, denial, saved)
    _patch(subprocess, SUBPROCESS_NAMES, denial, saved)
    if _ps is not None:
        _patch(_ps, ("fork_exec",), denial, saved)
    _patch(pathlib.Path, PATH_NAMES, denial, saved)
    _patch(glob, GLOB_NAMES, denial, saved)
    _patch(tempfile, TEMPFILE_NAMES, denial, saved)
    _patch(builtins, ("open",), denial, saved)
    _patch(socket, SOCKET_NAMES, denial, saved)
    for m in BLOCKED_MODULES:
        blocked[m] = sys.modules.get(m)
        sys.modules[m] = None  # a fresh `import ctypes` raises ImportError
    before = _purge_package()
    try:
        yield denial
    finally:
        for target, n, v in reversed(saved):
            setattr(target, n, v)
        for m, v in blocked.items():
            if v is None:
                sys.modules.pop(m, None)
            else:
                sys.modules[m] = v
        _purge_package()
        sys.modules.update(before)


def fresh(module: str):
    """Import `module` now, under whatever denial is active, discarding any cached copy."""
    sys.modules.pop(module, None)
    return importlib.import_module(module)
```

```python
# packages/coord-fold/tests/early_binder.py
"""A module that binds enumeration AT IMPORT TIME. Imported fresh under denial, it binds the stub."""
from posix import listdir as _l


def scan():
    return _l(".")
```

```python
# packages/coord-fold/tests/test_no_enumeration_harness.py
"""G29 — THE GUARANTEE, both ways. These tests cannot be satisfied syntactically."""
import json
import os
import pathlib
import subprocess

import pytest
from fakes import FakeReader, FakeStore, FakeWriter
from harness import CapabilityAbsent, denied, fresh

CFG = "team/r/_coord/bus-v4/records.json"
CFG_DOC = json.dumps({"data_type": "MomentAnnotation/x", "api_version": "v1alpha1"})
CKPT = "team/r/member/me/fold/checkpoint.json"


def _rec(kind, slug, at, rid, to="me"):
    p = {"v": 1, "at": at, "from": "boss", "to": to, "kind": kind, "slug": slug, "pri": "P1", "ptr": f"team/r/task/{slug}.md"}
    return {"id": rid, "recorded_at": at, "note": json.dumps(p)}


def _store(n=50):
    return FakeStore({CFG: CFG_DOC}, [_rec("open", f"s{i}", f"2026-09-04T10:{i % 60:02d}:00Z", str(i)) for i in range(n)])


def test_the_real_fold_completes_with_enumeration_absent():
    st = _store()
    with denied() as d:
        cli = fresh("coord_fold.cli")
        assert cli.main(["fold", "r", "--agent", "me", "--now", "2026-09-04T11:00:00Z"], reader=FakeReader(st), writer=FakeWriter(st)) == 0
    assert d.attempts == [] and len(json.loads(st.saved[CKPT])["open"]) == 50


def test_every_verb_completes_with_enumeration_absent():
    st = _store(3); st.docs["team/r/_coord/responses/s0/reply.md"] = "done"
    with denied() as d:
        cli = fresh("coord_fold.cli")
        def m(argv):
            return cli.main(argv, reader=FakeReader(st), writer=FakeWriter(st))
        assert m(["fold", "r", "--agent", "me", "--now", "2026-09-04T11:00:00Z"]) == 0
        assert m(["status", "r", "--agent", "me"]) == 0
        assert m(["emit", "r", "--from", "me", "--to", "boss", "--kind", "note", "--slug", "s0", "--pri", "P3", "--at", "2026-09-04T11:01:00Z"]) == 0
        assert m(["claim", "r", "s1", "--agent", "me", "--at", "2026-09-04T11:02:00Z"]) == 0
        assert m(["release", "r", "s2", "--agent", "me", "--at", "2026-09-04T11:03:00Z"]) == 0
        assert m(["close", "r", "s0", "--agent", "me", "--evidence", "team/r/_coord/responses/s0/reply.md", "--at", "2026-09-04T11:04:00Z"]) == 0
    assert d.attempts == []


class _Enumerating(FakeReader):
    def read_events(self, channel, since):
        os.listdir(".")
        yield from super().read_events(channel, since)


class _Swallowing(FakeReader):
    def read_events(self, channel, since):
        try:
            os.listdir(".")
        except Exception:
            pass
        yield from super().read_events(channel, since)


class _Launching(FakeReader):
    def read_classified(self, path):
        subprocess.run(["ls", path])
        return super().read_classified(path)


class _Opening(FakeReader):
    def read_classified(self, path):
        open(path).read()
        return super().read_classified(path)


class _Globbing(FakeReader):
    def read_events(self, channel, since):
        scan: object = pathlib.Path(".").iterdir   # codex-coder round 7: the AnnAssign spelling
        list(scan())
        yield from super().read_events(channel, since)


@pytest.mark.parametrize("reader_cls", [_Enumerating, _Launching, _Opening, _Globbing])
def test_a_deliberately_enumerating_fold_raises_and_is_counted(reader_cls):
    """THE mutation. The capability is absent, not unnamed: no spelling passes."""
    st = _store(3)
    with denied() as d:
        cli = fresh("coord_fold.cli")
        with pytest.raises(CapabilityAbsent):
            cli.main(["fold", "r", "--agent", "me", "--now", "2026-09-04T11:00:00Z"], reader=reader_cls(st), writer=FakeWriter(st))
    assert len(d.attempts) == 1 and CKPT not in st.saved


def test_a_swallowing_enumerator_is_still_caught():
    st = _store(3)
    with denied() as d:
        cli = fresh("coord_fold.cli")
        with pytest.raises(CapabilityAbsent):
            cli.main(["fold", "r", "--agent", "me", "--now", "2026-09-04T11:00:00Z"], reader=_Swallowing(st), writer=FakeWriter(st))
    assert d.attempts == ["os.listdir"]


def test_an_import_time_binding_is_denied():
    with denied() as d:
        early = fresh("early_binder")
        with pytest.raises(CapabilityAbsent):
            early.scan()
    assert d.attempts == ["posix.listdir"]


def test_the_package_imports_fresh_under_denial_and_the_real_transport_is_inert():
    with denied() as d:
        tr = fresh("coord_fold.transport")
        with pytest.raises(CapabilityAbsent):
            tr.CliPointerReader(cli=["fulcra-api"]).read_classified("x")
        with pytest.raises(CapabilityAbsent):
            tr.CliPointerWriter(cli=["fulcra-api"]).save_doc("x", "y")
    assert d.attempts == ["subprocess.run", "tempfile.NamedTemporaryFile"]


def test_blocked_modules_cannot_be_imported_under_denial():
    import importlib
    with denied():
        for m in ("ctypes", "pty", "multiprocessing"):
            with pytest.raises(ImportError):
                importlib.import_module(m)


def test_an_uncached_module_cannot_even_be_imported_under_denial():
    """The import machinery enumerates directories; with enumeration absent, a module whose directory
    finder is not cached cannot be imported. A generated module is closed by construction."""
    import importlib, sys
    with denied() as d:
        sys.path_importer_cache.pop(str(pathlib.Path(__file__).parent), None)
        sys.modules.pop("early_binder", None)
        with pytest.raises(CapabilityAbsent):
            importlib.import_module("early_binder")
    assert d.attempts == ["posix.listdir"]
```

- [ ] **Step 2: Boundary truths — cheap and true, kept**

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

- [ ] **Step 3: Scaffold** (unchanged from r7; the transport keeps `pathlib` and never imports `os`)

```toml
# packages/coord-fold/pyproject.toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "coord-fold"
version = "0.1.0"
description = "Coord on annotations: a fold engine that cannot enumerate."
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
"""coord-fold — the fold engine that cannot enumerate."""
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

README (not gate-relevant beyond the ceiling sentence): six verbs, the guarantee in G29's words, the tripwire's demotion in G30's words, and the sentence `every module under **400 lines**`.

- [ ] **Step 4: Run.** Boundary truths: 8 pass now. Harness tests: fail until Task 7 exists (there is no fold to run) — **commit failing-first**; from Task 7 on they are the gate. **Mutations for the truths** (each restored): (a) give `CliPointerReader` a base class → FAILS; (b) `def _upload` on the reader → FAILS; (c) `from coord_engine import x` anywhere → FAILS; (d) an extra file under `coord_fold/` → FAILS. **The harness's mutations are its own parametrized tests** — an enumerating, launching, opening, or globbing reader raises — and they cannot be satisfied by any spelling, which is the point. **Commit** — `coord-fold: scaffold, boundary truths, and the capability-absent harness (G5–G7, G29)`

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
            packages/coord-fold/tests/test_no_enumeration_harness.py \
            packages/coord-fold/tests/test_structural.py \
            packages/coord-fold/tests/test_tripwire.py \
            packages/coord-fold/tests/test_file_size_ceiling.py \
            packages/coord-fold/tests/test_no_degraded_vocabulary.py -q
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

- [ ] **Run — 1 passed. Mutations:** a plain `os.listdir(".")` anywhere → FAILS (that is what it is for). `scan = getattr(pathlib.Path("."), "iter" + "dir"); list(scan())` in `fold.run` → the tripwire **PASSES** (no suspect identifier exists in the source) — and the harness **FAILS** three tests (the real fold, every verb, the swallowing reader), because the capability is absent regardless of spelling. Record both results in the commit message: that pair is why G30 is not the guarantee. (The annotated-assignment spelling `scan: object = pathlib.Path(".").iterdir` trips the identifier scan too; the tripwire is useful exactly that far.) **Commit** — `coord-fold: syntactic tripwire, demoted (G30)`

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

Run — 3 passed; the harness gate (Task 1) is now green end-to-end. **Commit** — `coord-fold: status tests; G29 harness green`

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
**14 — Comparator + `cutover-ready` + runbook**: tuples `(slug, pri, ptr)`; `AGREE n=k` / `DIVERGE slugs=[…]`; `cutover-ready` exits 0 only if trailing AGREE run ≥ N, span ≥ 24h, and the new open set both grew and shrank within it. Mutation: force the 24h check true → the one-minute-apart test FAILS.

### Task 15: AGENTS.md (ship-gate)

The package, its five gate files and CI step; six verbs and exit codes; the remainder-vs-unknown distinction (G25) and the `degraded` ban; dependency direction; the reader/writer boundary; **the guarantee in G29's words** ("we do not claim enumeration is impossible to write; we claim a fold that enumerates fails its tests") and the harness's denial list; **the tripwire's demotion in G30's words**; the cursor rules (G26/G31); §3.4 of the spec corrected (a type stops a method call, not `os.listdir`); and G24 — a revision is filed only with Task 0 green.

---

## Rulings (decided by coord-boss, directive 722f8f29, 2026-09-04T21:17Z) — reasoning attached

Each ruling is a Global Constraint above so verdicts can cite it; the reasoning is repeated here so the next reader knows *why*, or someone will helpfully add the refused things back.

1. **Lossless cursor — YES (G26).** Cursor = `recorded_at` of the last successfully applied event; never `now`, never past a gap. Why: the cursor is the only durable claim of coverage; a cursor that can pass unapplied events makes `unread_events` the sole record of the gap, and a lost counter then silently claims coverage — the exact failure class this rebuild ends. Test: re-run from the stored cursor yields the same open set (`test_rerunning_from_the_stored_cursor_yields_the_same_open_set`). `seen` stays: the cursor is inclusive, so the boundary event is re-read and deduped; `OVERLAP_SECONDS` stays for client-stamped `recorded_at` skew. The upstream ordering question (stable tiebreak on `get-records`) is still unmeasured and still worth measuring; it no longer gates the design.
2. **Detection, not CAS (G27).** The store has no compare-and-swap; the checkpoint carries `writer` + monotonic `generation`; a pass re-reads before writing and **refuses by name** if the generation moved: "*agent* is acting twice (two hosts or a duplicated cron)" — the double-acting condition the lease nonce already alarms on. Exit non-zero, visible, no silent retry, never overwrite. It refuses to *lose* the update; it cannot *prevent* the race, and the plan says so.
3. **No compaction in v1; never delete events (G28).** Bound the work, not the history. Compaction is a second source of truth; it arrives only against a measured number, as a derivable, discardable snapshot provably equal to replay-from-empty.
4. **`max_events` — bound required; hitting it is not an error and not degraded (G25).** Apply what was read, cursor to the last applied event, `unread_events: N`, exit 0, printed on stdout as a remainder. The only error: zero applied while events exist → `FoldRefused("no progress")`, non-zero.

## What this plan does not do (spec §10)

Does not fix the pre-fence publication overwrite. Does not migrate the anti-slop findings. Deletes nothing. Does not implement the §7 inbox reconciler; Task 3's per-module import allowlist is the proof it cannot be composed into a fold path.

## Revision log

- **r1–r4:** see `6e0d42e5`/`21dc909c` history. r4 was a coherent rewrite after codex-coder's round 3.
- **r8 (2026-09-04, coord-boss `cdfe666e` + correction `7952b545`; codex-coder round 7 on `0b5fd60c`):** **The shape changed.** Seven rounds of syntactic gates each closed one spelling (the last: `x: object = reader.read_events`, an `AnnAssign` no alias map handled) and left the class open; coord-boss ruled a static check cannot prove a negative about an unrestricted Python module. G18–G23 superseded; their ceremony deleted (closed allowlist, alias maps, required-call shape, handler shape, launcher argv sets, forbidden tokens, mass caps, per-function budgets, wrapper rule, banned names — gone). **G29:** the guarantee is behavioural — `tests/harness.py` makes enumeration, process launch, file open and network absent from the test process (patched at the capability to raise a `BaseException` and count; package imported fresh under denial; `ctypes`/`pty`/`multiprocessing` unimportable), and `test_no_enumeration_harness.py` asserts both ways: the real fold and all six verbs complete with zero attempts; enumerating/launching/opening/globbing readers raise (parametrized), a swallowing one is still counted, an import-time `from posix import listdir` is denied, the real transport is inert. **G30:** one syntactic tripwire kept and demoted in those words. Boundary truths kept in `test_structural.py` (8 tests). **G31:** the cursor passes observed-irrelevant records (codex-coder's second P0); test with 3000 other-agent events. The spec's §3.4 claim corrected in the Goal. Task 0 gates: harness + truths + tripwire + ceiling + vocabulary.
- **r7 (2026-09-04, codex-reviewer round 4 on `5d0e065b`, three P0s all mutation-confirmed):** (1) `os` and `glob` imported nowhere; `transport.save_doc` unlinks through `pathlib`; enumeration calls banned package-wide by attribute name on any receiver and by alias-resolved origin — the reviewer's exact `os.listdir('.')` in `_records` now fails three gates. (2) G23 is no longer spelling-based: `cli.py` has a closed, alias-resolved call allowlist (`CLI_ALLOWED_ORIGINS` + `CLI_ALLOWED_METHODS`; unknown callees fail); owner-only ops are judged after alias resolution (relative imports included); each required call must be an executable top-level `return`/assignment (optionally inside a top-level `try`); handlers contain no loops, `with`, comprehensions, or definitions. Mutations (g) alias-bound `checkpoint.empty` and (h) unreachable `fold.run` added to Task 3; (f)–(h) enumeration mutations added to Task 1. (3) The cursor-to-`now` P0 was fixed by r6 (G26) and is on this head. Task 0 re-run green before filing.
- **r6 (2026-09-04, coord-boss directive 722f8f29 — the four rulings, all decided; r5 accepted as filed):** G25–G28 added with reasoning; G4 grows to eight fields (`generation`, `writer`); `fold.run` takes `writer_id`, sets the cursor to the last applied event (never `now`), re-reads before writing and raises `FoldContended` by name, and treats a capped pass as rc 0 with the only error being zero progress; `FoldContended` joins the manifest; `status` exits 3 only on `unreadable_pointers`; `test_cli_fold` grows from 9 to 13 tests (cursor-never-now, rerun-idempotence, capped-pass-is-a-remainder, zero-progress, concurrent-writer-refused-by-name); mutation (e)'s target line tracks the new `fold.run` call. Task 0 re-run green before filing.
- **r5 (2026-09-04, after round-4 CHANGES from both reviewers @ `21dc909c`):** (1) **Materialized the plan against itself** (Task 0) and fixed what actually failed — six failures, four more than predicted: a `/dev/stdout` constant the argv test did not allow; tuple-unpacked `RC_*` the definition scanner could not see; a `lambda` in `_render_open` (now `_row_sort_key`, a manifest name); `getattr` in `main` (now a shared parent parser, so `args.now`/`args.at` always exist); the wrapper rule flagging same-module one-liners (now scoped to cross-module delegation); and a `cli.py` node ceiling of 900 against a *measured* 1456 (now 1800, with the guarantee moved to G23). (2) **Launcher-alias bypass closed** (G22): launcher imports confined to `transport.py` per-module; launcher calls detected by resolving import *and assignment* aliases in the AST; forbidden subcommand tokens scanned in every module; mutation (b) in Task 1 is the exact `from subprocess import run as launch` form. (3) **Required call edges and owner-only operations** (G23): each handler must call its named owner operation and `cli.py` may never apply/empty/save a checkpoint, parse an event, or read events; mutation (e) in Task 3 inlines the fold into `cmd_fold`. (4) `cli.py` is a single tagged block; "add to cli.py" prose blocks are gone because a gate cannot see them. (5) G11 and Ruling 2 reworded to what is true: bounded per observed state, and detection rather than CAS. (6) **Found by Task 0 on r5 itself, before filing:** the materializer's fence regex spelled a literal fence marker and so truncated its own block (now built as `"`" * 3`); `_owed_row` returned one `None` for both *not owed* and *checkpoint unreadable*, so `claim/release/close` would have said *refused* on an UNKNOWN — it now returns `(row, load_state)` and the handlers exit 3 on `error`; `build_parser` exceeded the per-function budgets and is data-driven; the three budgets are set from the measured wiring (see G21).

## Self-review

1. **Spec coverage.** §3.1→T4. §3.2→G2, T7/T9 never list. §3.3→T6/T7. §3.4→T1 (corrected: the harness, not the type, is the proof). §4→T7/T10/T11. §5→T12–T14 + runbook. §6→verb table, T9 (`release`). §7→T1 DAG. §8→G9/G10, T11. §9→rulings + `release`. §1a→T0/T1/T2/T3.
2. **Placeholder scan.** The golden key set in T5 names its source file and line. Tasks 12–14 reference r3's code by commit rather than repeating it; they are old-side and outside Task 0's gate, and the plan says so.
3. **Type consistency.** `reader`/`writer` everywhere; `fold.run(..., now=, writer_id=, max_events=, verify_pointers=)` identical in T7's code and tests; `read_classified → (str|None, ReadState)` in T1/T5/T6/T7/T9; `write_event(cfg, payload, *, sender)` in T1/T7/T8; `checkpoint.path/empty/apply/load/save` identical in T6/T7; exit codes 0/2/3 in every verb; T3's manifest names exactly what T1/T4–T7 define, including `_row_sort_key`.
4. **Self-gate.** Task 0 was run against this file before filing; the output is in the filing note.
