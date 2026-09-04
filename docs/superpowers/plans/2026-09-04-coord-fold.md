# coord-fold: Coord on Annotations Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Spec:** `docs/superpowers/specs/2026-09-04-coord-annotation-bus-design.md` (branch `claude/coord-boss-handoff-resume-60sjua` @ `3a4687b0`). Read it whole first; §1 and §1a are why every structural requirement below exists.

**Directive:** coord-boss `65761fbd` (P0). **Reviewer/gate:** codex-reviewer. **Implementer:** coord-maintainer.

**Goal:** A separate `coord-fold` package whose fold engine *cannot* enumerate — the no-enumeration rule becomes a property of the type system and a red CI check, not a reviewer's attention — running in parallel with the old bus until a comparator proves agreement, then cut over.

**Architecture:** Three planes (spec §3). Signal = one MomentAnnotation per event on a team channel. Content = unchanged OKF files addressed only by `ptr`. Fold = one checkpoint per agent, advanced by reading events forward from a cursor at O(new events). The fold package is handed a `PointerTransport` with exactly two methods and no `list_dir`. The only changes to `coord-engine` are the seed export, the dual-emit mirror, and the comparator — all on the *old* side, which is allowed to enumerate.

**Tech Stack:** Python ≥3.11, stdlib only inside `coord_fold` (argparse, json, subprocess, ast, pathlib). Build: hatchling. Test: pytest<8 (workspace constraint inherited from fulcra-api). Dependency: `fulcra-common` only (for `find_fulcra_cli`). The Fulcra API is reached by shelling out to the `fulcra-api` CLI exactly as the old transport does (`get-records`, `record`, `file stat`, `file download`).

---

## Global Constraints

Copied from the spec and the directive. Every task's requirements include these.

- **G1.** `kind` is a closed set: `open`, `close`, `claim`, `release`, `note`. Nothing else parses. (spec §3.1)
- **G2.** `ptr` is **one file path**. Never a directory, never a glob, **never absent on an `open`**. (spec §3.1)
- **G3.** Event payload v1 fields, exactly: `v`, `at`, `from`, `to`, `kind`, `slug`, `pri`, `ptr`. (spec §3.1)
- **G4.** Checkpoint v1 fields, exactly: `v`, `cursor`, `open` (map slug → `{pri, from, ptr, at}`), `unread_events`, `unreadable_pointers`. (spec §3.3)
- **G5.** `PointerTransport` exposes **exactly two methods**: `read_classified(path) -> (str|None, "ok"|"absent"|"error")` and `read_events(channel, since) -> Iterator[Event]`. No `list_dir`. No `glob`. (spec §3.4)
- **G6.** The fold package is a **separate uv workspace package** (`packages/coord-fold`), not a module inside `coord-engine`. Its `pyproject.toml` must not depend on `coord-engine`. (directive §1, spec §1a.1)
- **G7.** A structural test asserts the fold transport has no `list_dir` **and** the fold package's import graph never reaches `coord_engine` or any module of it. (directive §2, spec §1a.2)
- **G8.** A file-size ceiling is a CI gate on the new package. **Ceiling: 400 lines per `.py` file under `coord_fold/`.** The number is a choice; its existence is the requirement. (directive §3, spec §1a.3)
- **G9.** Every fold test drives the CLI (`coord_fold.cli.main([...])`) and asserts on the **stored checkpoint**, not on a decision function. (directive §4, spec §8)
- **G10.** Every test file is **mutation-verified**: the task shows it failing when the behaviour it names is removed. (directive §4, spec §8)
- **G11.** No output path in `coord_fold` may emit the string `degraded`. Two bounded unknowns replace it: `unread_events: N` and `unreadable_pointers: [slug]`. An unknown never reads as clear. (spec §4, §8)
- **G12.** Five core verbs: `emit`, `fold`, `claim`, `close`, `status`. Every other verb is killed and must be asked for by an agent that needs it. (spec §6, Ash decision — do not reopen)
- **G13.** Migration is **parallel bus proven then cut over**: seed the 253 open obligations only, dual-emit from the old engine, shadow-compare, cut over after N agreeing passes, freeze the old prefix read-only. Not strangle-in-place, not big-bang. (spec §5, Ash decision — do not reopen)
- **G14.** Rollout: coord-boss alone until a full day of ticks is clean, then one agent at a time. (spec §5.3, Ash decision — do not reopen)
- **G15.** Never hardcode the channel. The new bus's `data_type` is resolved from `team/<team>/_coord/bus-v4/records.json` via `read_classified`. (standing wake rule)
- **G16.** No secrets in any doc, note, or test fixture.
- **G17.** Commits are authored as `114089064+ashfulcra@users.noreply.github.com` — the repo is PUBLIC. Never a work address.

---

## File Structure

```
packages/coord-fold/
  pyproject.toml                      package metadata; deps = [fulcra-common]; NO coord-engine
  README.md                           five verbs, the two unknowns, how to run the gates
  coord_fold/
    __init__.py                       version only
    events.py                         Event, KINDS, build_payload, parse_event      (G1–G3)
    transport.py                      PointerTransport Protocol + CliPointerTransport (G5)
    channel.py                        resolve data_type from bus-v4/records.json    (G15)
    checkpoint.py                     schema v1, load, save, apply_event            (G4)
    fold.py                           fold(): read forward, apply, persist          (§3.3)
    cli.py                            emit / fold / claim / close / status          (G12)
  tests/
    pointer_fake.py                   PointerFake — read_classified + read_events, NOTHING else
    test_structural_no_enumeration.py G7: no list_dir; import graph never reaches coord_engine
    test_file_size_ceiling.py         G8: every coord_fold/*.py ≤ 400 lines
    test_no_degraded_vocabulary.py    G11: the string "degraded" is absent from coord_fold/
    test_events.py                    G1–G3
    test_checkpoint.py                G4
    test_cli_fold.py                  G9: fold via cli.main, assert stored checkpoint
    test_cli_emit.py                  emit via cli.main, assert the record written
    test_cli_claim_close.py           claim/close via cli.main, assert checkpoint + evidence read
    test_cli_status.py                status output, the two unknowns, exit codes

packages/coord-engine/coord_engine/
    dual_emit.py                      NEW, small: mirror old-bus transitions onto bus-v4   (§5.1 step 2)
    cli.py                            MODIFY: `obligations export-open`, `obligations compare-to-fold`
    records.py                        MODIFY: one call into dual_emit from emit_event
packages/coord-engine/tests/
    test_dual_emit.py
    test_obligations_export_open.py
    test_obligations_compare_to_fold.py

.github/workflows/uv-workspace.yml    MODIFY: named step "coord-fold structural gates"
docs/coord/COORD-FOLD-CUTOVER.md      runbook: seed → dual-emit → shadow → N → freeze
```

Responsibilities are one-per-file on purpose. §1a is explicit that the last rebuild died when queue/routing/cursor/output dissolved into one file; the ceiling in G8 is what stops it happening again.

---

## Verb Disposition (spec §6 — "recorded in the implementation plan, with a reason per verb")

The current engine registers **42 top-level nouns** at `5db5c3e5` (the spec says 38 at `3a4687b0`; the difference is four nouns that exist on the branch this plan was written against — `annotate`, `stash`, `wake`, `acceptance` — and the count is reported, not reconciled). Disposition of each on the **new** bus:

| Verb | Disposition | Reason |
|---|---|---|
| `tell` | **→ `emit`** | An `open` event with `to`, `pri`, `ptr`. The core write. |
| `respond` | **→ `close`** | A `close` event with evidence `ptr`. |
| `owed`, `obligations`, `needs-me`, `inbox` | **→ `fold` + `status`** | All four are "what do I owe"; one checkpoint answers it. |
| `roles claim/release` | **→ `claim`/`release`** (events) | Same fact, no lease directory to enumerate. |
| `status` (engine health) | **→ `status`** | Reports the checkpoint and its two unknowns. |
| `queue` | kill | Was the event-plane wake hint; `fold` *is* the read. |
| `reconcile` | kill | Rebuilds the aggregate by enumeration — the defect. |
| `board`, `search`, `agents`, `presence`, `engagement`, `threads`, `briefing`, `digest`, `dash`, `health`, `doctor` | kill | Every one is a corpus walk under a deadline (nine of the nine degraded families live here). Earn back one at a time, each with a stated cursor. |
| `broadcast`, `remind`, `later`, `intent` | kill | Sugar over `tell`; `emit --to all` covers broadcast; timers are a future-dated `emit`. |
| `review` (8 subverbs), `forge` | kill | Review is content-plane; a review request is an `open` with a `ptr` to the review doc. The forge mirror is a reconciler (§7 shape), off every fold path. |
| `task` (9 subverbs) | kill | Task state lives in the OKF doc; transitions that matter to routing are `open`/`close`. |
| `router`, `route`, `atc`, `headroom`, `usage` | kill | Dispatch policy over an enumerated board. Earn back on top of `fold`. |
| `escalate` | kill | Vacancy detection by attendance scan (592 dirs). Becomes a reconciler off the fold path if wanted. |
| `bus-v3`, `wake`, `stash`, `continuity`, `annotate`, `acceptance`, `asks`, `answer` | kill | Old-bus plumbing, or content-plane conveniences with no fold dependency. |

**Kept: 5. Killed: 37 (of 42).** Any killed verb returns only via a directive naming the agent that needs it and the cursor it reads from.

---

### Task 1: Package scaffold + the structural no-enumeration test

**Files:**
- Create: `packages/coord-fold/pyproject.toml`
- Create: `packages/coord-fold/coord_fold/__init__.py`
- Create: `packages/coord-fold/coord_fold/transport.py` (Protocol only, this task)
- Create: `packages/coord-fold/tests/pointer_fake.py`
- Create: `packages/coord-fold/tests/test_structural_no_enumeration.py`

**Interfaces:**
- Produces: `coord_fold.transport.PointerTransport` (Protocol) with `read_classified(path: str) -> tuple[str | None, Literal["ok","absent","error"]]` and `read_events(channel: str, since: str) -> Iterator[dict]`.
- Produces: `tests.pointer_fake.PointerFake(docs: dict[str,str], events: list[dict])` implementing exactly those two.

- [ ] **Step 1: Write the failing structural test**

```python
# packages/coord-fold/tests/test_structural_no_enumeration.py
"""G7. The no-enumeration rule as a red check, not a note.

Spec §1a: the 2026-08-21 rule ("any change that ADDS a list_dir to a fold path
is rejected") failed because it was a policy against a codebase where the
enumerator was already imported and holding a live transport. This file makes
the rule a property of the package: if it ever goes green while a fold can
enumerate, the package has been consolidated and the test is what notices.
"""
from __future__ import annotations

import ast
import importlib
import pathlib

import coord_fold
from coord_fold import transport as fold_transport

PKG_DIR = pathlib.Path(coord_fold.__file__).parent
FORBIDDEN_METHODS = ("list_dir", "glob", "listdir", "scandir", "walk")
FORBIDDEN_IMPORT_ROOTS = ("coord_engine",)


def _modules():
    return sorted(p for p in PKG_DIR.glob("*.py"))


def test_the_fake_transport_has_no_enumeration_method():
    from pointer_fake import PointerFake
    t = PointerFake({}, [])
    for name in FORBIDDEN_METHODS:
        assert not hasattr(t, name), f"PointerFake grew {name}"


def test_the_real_transport_has_no_enumeration_method():
    t = fold_transport.CliPointerTransport(cli=["true"])
    for name in FORBIDDEN_METHODS:
        assert not hasattr(t, name), f"CliPointerTransport grew {name}"


def test_no_module_in_the_package_mentions_an_enumeration_method():
    """Source-level: the TOKEN must not appear. Cheaper than reasoning about
    call sites, and it is the token a consolidation would paste in."""
    for path in _modules():
        src = path.read_text()
        for name in FORBIDDEN_METHODS:
            assert f".{name}(" not in src and f"def {name}(" not in src, (
                f"{path.name} mentions {name}")


def test_the_import_graph_never_reaches_coord_engine():
    """Walk every `import`/`from` in every module; none may resolve under
    coord_engine. A package boundary that is not enforced is a module boundary
    with extra steps."""
    for path in _modules():
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for n in names:
                root = n.split(".")[0]
                assert root not in FORBIDDEN_IMPORT_ROOTS, (
                    f"{path.name} imports {n} — the fold package reached the "
                    f"enumerating engine")


def test_pyproject_does_not_depend_on_coord_engine():
    import tomllib
    pyproject = PKG_DIR.parent / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text())
    deps = data["project"].get("dependencies", [])
    assert not any(d.startswith("coord-engine") for d in deps), deps


def test_the_protocol_has_exactly_two_methods():
    """G5: exactly read_classified and read_events. Adding a third method to
    the Protocol is how enumeration would come back with a different name."""
    members = {n for n in dir(fold_transport.PointerTransport)
               if not n.startswith("_")}
    assert members == {"read_classified", "read_events"}, members
```

- [ ] **Step 2: Run it — expect ImportError (no package yet)**

Run: `cd packages/coord-fold && python -m pytest tests/test_structural_no_enumeration.py -q`
Expected: `ModuleNotFoundError: No module named 'coord_fold'` — collection error. Confirm the test file itself parses: `python -c "import ast;ast.parse(open('tests/test_structural_no_enumeration.py').read())"`.

- [ ] **Step 3: Scaffold the package**

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
dependencies = [
    "fulcra-common>=0.3.0",
]

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
"""coord-fold — the fold engine that cannot enumerate. See the spec:
docs/superpowers/specs/2026-09-04-coord-annotation-bus-design.md."""
__version__ = "0.1.0"
```

```python
# packages/coord-fold/coord_fold/transport.py
"""The enforcing interface (spec §3.4).

A fold is constructed with a transport that has exactly two methods. There is
no list_dir and no glob, so a fold that wants to enumerate has nothing to call.
The 2026-08-21 rule stops depending on a reviewer noticing.
"""
from __future__ import annotations

import json
import subprocess
from typing import Iterator, Literal, Protocol

ReadState = Literal["ok", "absent", "error"]


class PointerTransport(Protocol):
    def read_classified(self, path: str) -> tuple[str | None, ReadState]: ...
    def read_events(self, channel: str, since: str) -> Iterator[dict]: ...


class CliPointerTransport:
    """Reaches the Fulcra API by shelling out to the `fulcra-api` CLI, exactly
    as the old transport does — `file stat`, `file download`, `get-records`.
    Deliberately NOT a subclass of anything in coord_engine."""

    def __init__(self, cli: list[str], timeout: float = 60.0) -> None:
        self._cli = list(cli)
        self._timeout = timeout

    def _run(self, *args: str, stdin: str | None = None) -> tuple[int, str, str]:
        try:
            p = subprocess.run([*self._cli, *args], input=stdin, capture_output=True,
                               text=True, timeout=self._timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return 127, "", str(exc)
        return p.returncode, p.stdout, p.stderr

    def read_classified(self, path: str) -> tuple[str | None, ReadState]:
        # Measured 2026-09-02: `file stat` on a missing path returns rc!=0 with
        # "File not found in Fulcra"; on a present path rc 0 with bytes+version.
        rc, _out, err = self._run("file", "stat", path)
        if rc != 0:
            return (None, "absent") if "File not found" in err else (None, "error")
        rc, out, _err = self._run("file", "download", path, "/dev/stdout")
        if rc != 0:
            return None, "error"
        return out, "ok"

    def read_events(self, channel: str, since: str) -> Iterator[dict]:
        # `get-records <type> <since> <until>` emits JSONL, one record per line.
        # `until` is left open-ended by passing a far-future instant.
        rc, out, _err = self._run("get-records", channel, since, "2999-01-01T00:00:00Z")
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


class TransportUnavailable(RuntimeError):
    """The event read did not complete. The fold must NOT advance its cursor —
    a consumer that treats a failed window as empty advances past work it never
    saw (the old transport's own docstring, kept as the rule here)."""
```

```python
# packages/coord-fold/tests/pointer_fake.py
"""The test transport. It has read_classified and read_events and NOTHING
else — no put-by-listing, no directory model. Docs are a flat path->text map;
events are a list of record dicts in delivery order."""
from __future__ import annotations

from typing import Iterator


class PointerFake:
    def __init__(self, docs: dict[str, str], events: list[dict],
                 *, fail_reads: bool = False, fail_events: bool = False) -> None:
        self.docs = dict(docs)
        self.events = list(events)
        self.fail_reads = fail_reads
        self.fail_events = fail_events
        self.written: list[dict] = []      # records the CLI would have written
        self.saved: dict[str, str] = {}    # checkpoints saved via save_doc

    def read_classified(self, path: str):
        if self.fail_reads:
            return None, "error"
        if path in self.saved:
            return self.saved[path], "ok"
        if path in self.docs:
            return self.docs[path], "ok"
        return None, "absent"

    def read_events(self, channel: str, since: str) -> Iterator[dict]:
        if self.fail_events:
            from coord_fold.transport import TransportUnavailable
            raise TransportUnavailable("fake outage")
        for rec in self.events:
            if rec.get("recorded_at", "") >= since:
                yield rec
```

Register the workspace member: `packages/*` is already the glob in the root `pyproject.toml`, so no root change is needed. Run `uv sync --all-packages` from the repo root and confirm `coord-fold` resolves.

- [ ] **Step 4: Run the structural tests — expect all six to pass**

Run: `cd packages/coord-fold && python -m pytest tests/test_structural_no_enumeration.py -v`
Expected: 6 passed.

- [ ] **Step 5: MUTATION-VERIFY (G10) — prove each assertion can fail**

Three mutations, each restored before the next:

```bash
# (a) add an enumeration method to the real transport
python - <<'PY'
p="coord_fold/transport.py"; s=open(p).read()
s=s.replace("    def read_classified(", "    def list_dir(self, prefix): return []\n\n    def read_classified(",1)
open(p,"w").write(s)
PY
python -m pytest tests/test_structural_no_enumeration.py -q   # expect 2 FAIL (real-transport + token test)
git checkout coord_fold/transport.py

# (b) import the enumerating engine
python - <<'PY'
p="coord_fold/transport.py"; s=open(p).read()
s=s.replace("import subprocess", "import subprocess\nimport coord_engine.transport",1)
open(p,"w").write(s)
PY
python -m pytest tests/test_structural_no_enumeration.py -q   # expect 1 FAIL (import graph)
git checkout coord_fold/transport.py

# (c) add a third Protocol method
python - <<'PY'
p="coord_fold/transport.py"; s=open(p).read()
s=s.replace("    def read_events(self, channel: str, since: str) -> Iterator[dict]: ...",
            "    def read_events(self, channel: str, since: str) -> Iterator[dict]: ...\n    def read_many(self, paths): ...",1)
open(p,"w").write(s)
PY
python -m pytest tests/test_structural_no_enumeration.py -q   # expect 1 FAIL (exactly-two)
git checkout coord_fold/transport.py
```

Expected: each mutation fails the named test(s); `git status` clean afterwards.

- [ ] **Step 6: Commit**

```bash
git add packages/coord-fold
git -c user.name=ashfulcra -c user.email=114089064+ashfulcra@users.noreply.github.com \
  commit -m "coord-fold: package scaffold + structural no-enumeration test (G5, G6, G7)"
```

---

### Task 2: File-size ceiling as a CI gate

**Files:**
- Create: `packages/coord-fold/tests/test_file_size_ceiling.py`
- Modify: `.github/workflows/uv-workspace.yml` (add one named step)

- [ ] **Step 1: Write the failing test**

```python
# packages/coord-fold/tests/test_file_size_ceiling.py
"""G8. "Just put it in the big file" fails a check instead of passing a review.

Spec §1a: the last rebuild's fold/cursor/routing/output layers dissolved into
one 13,848-line cli.py. The ceiling is 400 lines. The number is a choice; the
existence of the gate is the requirement.
"""
import pathlib

import coord_fold

CEILING = 400
PKG_DIR = pathlib.Path(coord_fold.__file__).parent


def test_every_module_is_under_the_ceiling():
    over = {}
    for path in PKG_DIR.glob("*.py"):
        n = sum(1 for _ in path.open())
        if n > CEILING:
            over[path.name] = n
    assert not over, f"over the {CEILING}-line ceiling: {over}"


def test_the_ceiling_is_the_documented_number():
    """The README states 400; if someone raises the constant they must raise the
    doc too, or this reminds them."""
    readme = (PKG_DIR.parent / "README.md").read_text()
    assert f"{CEILING} lines" in readme
```

- [ ] **Step 2: Run — expect the README assertion to fail (no README yet)**

Run: `python -m pytest tests/test_file_size_ceiling.py -q`
Expected: 1 passed, 1 failed (`README.md` missing).

- [ ] **Step 3: Write the README and the CI step**

```markdown
<!-- packages/coord-fold/README.md -->
# coord-fold

The fold engine that cannot enumerate. Five verbs: `emit`, `fold`, `claim`, `close`, `status`.

Structural gates (all run in CI as "coord-fold structural gates"):
- no module may call `list_dir`/`glob`, and the import graph never reaches `coord_engine`;
- every module stays under **400 lines**;
- the string `degraded` never appears in the package.

Two bounded unknowns replace the nine degraded families: `unread_events: N` and
`unreadable_pointers: [slug]`. An unknown never reads as clear.
```

```yaml
# .github/workflows/uv-workspace.yml — add after the existing pytest step
      - name: coord-fold structural gates
        run: |
          uv run --package coord-fold --extra dev python -m pytest \
            packages/coord-fold/tests/test_structural_no_enumeration.py \
            packages/coord-fold/tests/test_file_size_ceiling.py \
            packages/coord-fold/tests/test_no_degraded_vocabulary.py -q
```

(The vocabulary test file is created in Task 10; until then this step lists a file that does not exist and CI will fail — that is intended and Task 10 clears it. Do not remove the line.)

- [ ] **Step 4: Run — expect pass**

Run: `python -m pytest tests/test_file_size_ceiling.py -q`
Expected: 2 passed.

- [ ] **Step 5: MUTATION-VERIFY**

```bash
python - <<'PY'
p="coord_fold/__init__.py"; open(p,"a").write("\n".join(["# pad"]*401)+"\n")
PY
python -m pytest tests/test_file_size_ceiling.py -q   # expect 1 FAIL naming __init__.py
git checkout coord_fold/__init__.py
```

- [ ] **Step 6: Commit**

```bash
git add packages/coord-fold/tests/test_file_size_ceiling.py packages/coord-fold/README.md .github/workflows/uv-workspace.yml
git -c user.name=ashfulcra -c user.email=114089064+ashfulcra@users.noreply.github.com \
  commit -m "coord-fold: 400-line ceiling as a CI gate (G8)"
```

---

### Task 3: Event schema — build and parse payload v1

**Files:**
- Create: `packages/coord-fold/coord_fold/events.py`
- Create: `packages/coord-fold/tests/test_events.py`

**Interfaces:**
- Produces: `KINDS = ("open","close","claim","release","note")`; `build_payload(*, at, sender, to, kind, slug, pri, ptr) -> dict`; `parse_event(record: dict) -> dict | None` (returns the payload dict plus `record_id`, or None if the record is not a v1 event); `PRIORITIES = ("P0","P1","P2","P3")`.

- [ ] **Step 1: Write the failing tests**

```python
# packages/coord-fold/tests/test_events.py
"""G1–G3. The closed kind set, the eight fields, and ptr-required-on-open."""
import json

import pytest

from coord_fold import events


def _rec(note, rid="r1", at="2026-09-04T13:45:00Z"):
    return {"id": rid, "recorded_at": at, "note": json.dumps(note)}


def test_build_produces_exactly_the_eight_fields():
    p = events.build_payload(at="2026-09-04T13:45:00Z", sender="coord-boss",
                             to="coord-maintainer", kind="open",
                             slug="p1-x-7ca915c9", pri="P1",
                             ptr="team/fulcra/task/p1-x-7ca915c9.md")
    assert set(p) == {"v", "at", "from", "to", "kind", "slug", "pri", "ptr"}
    assert p["v"] == 1 and p["from"] == "coord-boss"


def test_open_without_ptr_is_refused():
    with pytest.raises(ValueError):
        events.build_payload(at="t", sender="a", to="b", kind="open",
                             slug="s", pri="P1", ptr=None)


def test_ptr_must_be_a_single_file_path_not_a_dir_or_glob():
    for bad in ("team/fulcra/task/", "team/fulcra/task/*.md", ""):
        with pytest.raises(ValueError):
            events.build_payload(at="t", sender="a", to="b", kind="open",
                                 slug="s", pri="P1", ptr=bad)


def test_unknown_kind_is_refused_on_build():
    with pytest.raises(ValueError):
        events.build_payload(at="t", sender="a", to="b", kind="directive",
                             slug="s", pri="P1", ptr="x.md")


def test_parse_accepts_a_v1_event_and_carries_the_record_id():
    p = events.build_payload(at="t", sender="a", to="b", kind="open",
                             slug="s", pri="P1", ptr="x.md")
    ev = events.parse_event(_rec(p, rid="abc"))
    assert ev is not None and ev["record_id"] == "abc" and ev["kind"] == "open"


def test_parse_skips_free_text_and_foreign_payloads_silently():
    assert events.parse_event({"id": "x", "note": "hello"}) is None
    assert events.parse_event(_rec({"kind": "directive", "v": 1})) is None
    assert events.parse_event(_rec({"v": 2, "kind": "open"})) is None


def test_note_kind_may_omit_ptr():
    p = events.build_payload(at="t", sender="a", to="b", kind="note",
                             slug="s", pri="P3", ptr=None)
    assert p["ptr"] is None
```

- [ ] **Step 2: Run — expect ImportError**

Run: `python -m pytest tests/test_events.py -q`
Expected: `ImportError: cannot import name 'events'`.

- [ ] **Step 3: Implement**

```python
# packages/coord-fold/coord_fold/events.py
"""Signal-plane payload v1 (spec §3.1). Small, fixed, self-describing."""
from __future__ import annotations

import json
from typing import Any

PAYLOAD_VERSION = 1
KINDS = ("open", "close", "claim", "release", "note")
PRIORITIES = ("P0", "P1", "P2", "P3")
PTR_REQUIRED = ("open", "close")


def _check_ptr(ptr: str | None, kind: str) -> None:
    if ptr is None:
        if kind in PTR_REQUIRED:
            raise ValueError(f"ptr is required on {kind}")
        return
    if not ptr or ptr.endswith("/") or "*" in ptr or "?" in ptr:
        raise ValueError("ptr must be one file path — never a directory or a glob")


def build_payload(*, at: str, sender: str, to: str, kind: str, slug: str,
                  pri: str, ptr: str | None) -> dict[str, Any]:
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}, got {kind!r}")
    if pri not in PRIORITIES:
        raise ValueError(f"pri must be one of {PRIORITIES}, got {pri!r}")
    if not slug:
        raise ValueError("slug is required")
    _check_ptr(ptr, kind)
    return {"v": PAYLOAD_VERSION, "at": at, "from": sender, "to": to,
            "kind": kind, "slug": slug, "pri": pri, "ptr": ptr}


def parse_event(record: dict[str, Any]) -> dict[str, Any] | None:
    """The payload plus ``record_id``, or None for anything that is not a v1
    event. None is silent by design: free-text notes on the same channel are
    ordinary annotations, not errors."""
    note = record.get("note")
    if not isinstance(note, str):
        return None
    try:
        p = json.loads(note)
    except json.JSONDecodeError:
        return None
    if not isinstance(p, dict) or p.get("v") != PAYLOAD_VERSION:
        return None
    if p.get("kind") not in KINDS or not p.get("slug"):
        return None
    out = dict(p)
    out["record_id"] = record.get("id")
    out["recorded_at"] = record.get("recorded_at")
    return out
```

- [ ] **Step 4: Run — expect 7 passed**

- [ ] **Step 5: MUTATION-VERIFY**

```bash
python - <<'PY'
p="coord_fold/events.py"; s=open(p).read()
s=s.replace('    if ptr is None:\n        if kind in PTR_REQUIRED:', '    if ptr is None:\n        if False:',1)
open(p,"w").write(s)
PY
python -m pytest tests/test_events.py -q   # expect test_open_without_ptr_is_refused FAIL
git checkout coord_fold/events.py
```

- [ ] **Step 6: Commit** — `coord-fold: event payload v1 — closed kinds, eight fields, ptr required on open (G1–G3)`

---

### Task 4: Channel resolution + the write side of the CLI transport

**Files:**
- Create: `packages/coord-fold/coord_fold/channel.py`
- Modify: `packages/coord-fold/coord_fold/transport.py` (add `write_event`)
- Modify: `packages/coord-fold/tests/pointer_fake.py` (add `write_event`)
- Create: `packages/coord-fold/tests/test_channel.py`

**Interfaces:**
- Produces: `channel.resolve(transport, team) -> dict` with keys `data_type`, `api_version`; raises `channel.ChannelUnresolved` on absent/error.
- Produces: `transport.write_event(channel_cfg: dict, payload: dict, *, sender: str) -> bool` on both `CliPointerTransport` and `PointerFake`.

Note G5 says the *Protocol* has exactly two methods — those are the fold's **read** surface. `write_event` is the `emit` verb's surface; it lives on the same class but is deliberately not part of `PointerTransport`, and Task 1's `test_the_protocol_has_exactly_two_methods` keeps it that way. Add this sentence to the transport module docstring.

- [ ] **Step 1: Write the failing tests**

```python
# packages/coord-fold/tests/test_channel.py
"""G15: never hardcode the channel. It is read from bus-v4/records.json."""
import json

import pytest

from coord_fold import channel
from pointer_fake import PointerFake

CFG_PATH = "team/r/_coord/bus-v4/records.json"


def test_resolves_data_type_from_the_config_document():
    t = PointerFake({CFG_PATH: json.dumps({"data_type": "MomentAnnotation/abc",
                                           "api_version": "v1alpha1"})}, [])
    cfg = channel.resolve(t, "r")
    assert cfg["data_type"] == "MomentAnnotation/abc"


def test_absent_config_raises_not_returns_a_default():
    with pytest.raises(channel.ChannelUnresolved):
        channel.resolve(PointerFake({}, []), "r")


def test_unreadable_config_raises_as_well_and_says_error_not_absent():
    with pytest.raises(channel.ChannelUnresolved, match="error"):
        channel.resolve(PointerFake({}, [], fail_reads=True), "r")


def test_config_missing_data_type_raises():
    t = PointerFake({CFG_PATH: json.dumps({"api_version": "v1alpha1"})}, [])
    with pytest.raises(channel.ChannelUnresolved):
        channel.resolve(t, "r")
```

- [ ] **Step 2: Run — expect ImportError**

- [ ] **Step 3: Implement**

```python
# packages/coord-fold/coord_fold/channel.py
"""Resolve the new bus's channel from its config document (G15). Absent and
unreadable are different words and both refuse — there is no default channel."""
from __future__ import annotations

import json

from .transport import PointerTransport

CONFIG_PATH = "team/{team}/_coord/bus-v4/records.json"
REQUIRED = ("data_type", "api_version")


class ChannelUnresolved(RuntimeError):
    pass


def config_path(team: str) -> str:
    return CONFIG_PATH.format(team=team)


def resolve(transport: PointerTransport, team: str) -> dict[str, str]:
    body, state = transport.read_classified(config_path(team))
    if state != "ok" or body is None:
        raise ChannelUnresolved(f"bus-v4 config for team {team}: {state}")
    try:
        cfg = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ChannelUnresolved(f"bus-v4 config unparsable: {exc}") from exc
    missing = [k for k in REQUIRED if not cfg.get(k)]
    if missing:
        raise ChannelUnresolved(f"bus-v4 config missing {missing}")
    return {k: str(cfg[k]) for k in REQUIRED}
```

Add to `transport.py`:

```python
    def write_event(self, channel_cfg: dict[str, str], payload: dict,
                    *, sender: str) -> bool:
        """Write one record. The stdin document's key names MUST match what the
        old transport sends — see Step 4. Returns True only on rc 0."""
        doc = {"data_type": channel_cfg["data_type"],
               "api_version": channel_cfg["api_version"],
               "note": json.dumps(payload, separators=(",", ":")),
               "source": sender,
               "recorded_at": payload["at"]}
        rc, _out, _err = self._run("record", stdin=json.dumps(doc))
        return rc == 0
```

Add to `pointer_fake.py`:

```python
    def write_event(self, channel_cfg, payload, *, sender):
        self.written.append({"channel": channel_cfg["data_type"],
                             "payload": dict(payload), "sender": sender})
        # a written event is immediately readable by a later fold
        self.events.append({"id": f"w{len(self.written)}",
                            "recorded_at": payload["at"],
                            "note": __import__("json").dumps(payload)})
        return True
```

- [ ] **Step 4: GOLDEN-COMPARE the stdin document against the old transport (no invented keys)**

Open `packages/coord-engine/coord_engine/transport.py` at `record_write` (line ~385 at `5db5c3e5`) and read the dict it serialises to stdin. Make `write_event`'s `doc` use **exactly** those key names. Then add this test to `test_channel.py`:

```python
def test_write_event_stdin_document_matches_the_old_transport_keys():
    """Golden comparison: the record endpoint validates keys, and the old
    transport is the only working reference. Copy, do not guess."""
    from coord_fold.transport import CliPointerTransport
    seen = {}
    t = CliPointerTransport(cli=["true"])
    t._run = lambda *a, stdin=None: (seen.update(doc=json.loads(stdin)) or (0, "", ""))
    t.write_event({"data_type": "D", "api_version": "v1alpha1"},
                  {"v": 1, "at": "T", "from": "a", "to": "b", "kind": "note",
                   "slug": "s", "pri": "P3", "ptr": None}, sender="a")
    # EDIT THIS SET to the exact keys read from coord_engine/transport.py record_write:
    assert set(seen["doc"]) == {"data_type", "api_version", "note", "source", "recorded_at"}
```

If the old transport's keys differ from the set above, change **both** `write_event` and this assertion to match the old transport. The point of the test is that the answer was read, not guessed.

- [ ] **Step 5: Run — expect all pass**

- [ ] **Step 6: MUTATION-VERIFY** — replace `raise ChannelUnresolved(f"bus-v4 config for team {team}: {state}")` with `return {"data_type": "hardcoded", "api_version": "v1alpha1"}`; expect the absent/unreadable/missing tests to FAIL; restore.

- [ ] **Step 7: Commit** — `coord-fold: channel resolution from bus-v4/records.json + write_event with golden-compared keys (G15)`

---

### Task 5: Checkpoint schema v1 — load, save, apply

**Files:**
- Create: `packages/coord-fold/coord_fold/checkpoint.py`
- Modify: `packages/coord-fold/tests/pointer_fake.py` (add `save_doc`)
- Create: `packages/coord-fold/tests/test_checkpoint.py`

**Interfaces:**
- Produces: `checkpoint.path(team, agent) -> str` = `team/<team>/member/<agent>/fold/checkpoint.json`
- Produces: `empty(now: str) -> dict`; `load(transport, team, agent) -> tuple[dict, Literal["ok","fresh","corrupt","error"]]`; `save(transport, team, agent, state) -> bool`; `apply(state, event) -> None`.
- `save` needs a write on the transport: add `save_doc(path, text) -> bool` to `CliPointerTransport` (via `file upload` from a temp file) and to `PointerFake` (stores into `saved`). Like `write_event`, it is not on the Protocol.

- [ ] **Step 1: Write the failing tests**

```python
# packages/coord-fold/tests/test_checkpoint.py
"""G4: exactly v, cursor, open, unread_events, unreadable_pointers — and the
apply rules: open adds, close/release remove, claim annotates."""
import json

from coord_fold import checkpoint as cp
from pointer_fake import PointerFake

NOW = "2026-09-04T13:45:00Z"


def _ev(kind, slug="s1", **kw):
    base = {"v": 1, "at": NOW, "from": "boss", "to": "me", "kind": kind,
            "slug": slug, "pri": "P1", "ptr": f"team/r/task/{slug}.md",
            "record_id": kw.pop("rid", "r1")}
    base.update(kw)
    return base


def test_empty_has_exactly_the_five_fields():
    st = cp.empty(NOW)
    assert set(st) == {"v", "cursor", "open", "unread_events", "unreadable_pointers"}
    assert st["v"] == 1 and st["open"] == {} and st["unread_events"] == 0


def test_open_adds_a_row_with_pri_from_ptr_at():
    st = cp.empty(NOW)
    cp.apply(st, _ev("open"))
    assert st["open"]["s1"] == {"pri": "P1", "from": "boss",
                                "ptr": "team/r/task/s1.md", "at": NOW}


def test_close_and_release_remove_the_row():
    for kind in ("close", "release"):
        st = cp.empty(NOW); cp.apply(st, _ev("open")); cp.apply(st, _ev(kind))
        assert "s1" not in st["open"], kind


def test_claim_annotates_without_removing():
    st = cp.empty(NOW); cp.apply(st, _ev("open"))
    cp.apply(st, _ev("claim", **{"from": "me"}))
    assert st["open"]["s1"]["claimed_by"] == "me"


def test_close_of_an_unknown_slug_is_a_noop_not_an_error():
    st = cp.empty(NOW); cp.apply(st, _ev("close"))
    assert st["open"] == {}


def test_a_record_id_seen_before_is_not_applied_twice():
    """Overlap reads may redeliver; idempotency is by record id."""
    st = cp.empty(NOW)
    cp.apply(st, _ev("open", rid="same")); cp.apply(st, _ev("close", rid="same"))
    assert "s1" in st["open"], "the second delivery of record 'same' must be ignored"


def test_load_fresh_when_absent_corrupt_when_unparsable_error_when_unreadable():
    t = PointerFake({}, [])
    _, src = cp.load(t, "r", "me"); assert src == "fresh"
    t.docs[cp.path("r", "me")] = "not json"
    _, src = cp.load(t, "r", "me"); assert src == "corrupt"
    _, src = cp.load(PointerFake({}, [], fail_reads=True), "r", "me"); assert src == "error"


def test_save_then_load_roundtrips():
    t = PointerFake({}, [])
    st = cp.empty(NOW); cp.apply(st, _ev("open")); st["cursor"] = NOW
    assert cp.save(t, "r", "me", st)
    back, src = cp.load(t, "r", "me")
    assert src == "ok" and back["open"] == st["open"] and back["cursor"] == NOW
```

- [ ] **Step 2: Run — expect ImportError**

- [ ] **Step 3: Implement**

```python
# packages/coord-fold/coord_fold/checkpoint.py
"""One durable checkpoint per agent (spec §3.3). Five fields, no more."""
from __future__ import annotations

import json
from typing import Any, Literal

from .transport import PointerTransport

SCHEMA_VERSION = 1
SEEN_CAP = 500   # record ids kept for idempotency across overlap reads
PATH = "team/{team}/member/{agent}/fold/checkpoint.json"

LoadState = Literal["ok", "fresh", "corrupt", "error"]


def path(team: str, agent: str) -> str:
    return PATH.format(team=team, agent=agent)


def empty(now: str) -> dict[str, Any]:
    return {"v": SCHEMA_VERSION, "cursor": now, "open": {},
            "unread_events": 0, "unreadable_pointers": []}


def apply(state: dict[str, Any], ev: dict[str, Any]) -> None:
    seen: list[str] = state.setdefault("_seen", [])
    rid = ev.get("record_id")
    if rid and rid in seen:
        return
    slug, kind = ev["slug"], ev["kind"]
    rows = state["open"]
    if kind == "open":
        rows[slug] = {"pri": ev["pri"], "from": ev["from"], "ptr": ev["ptr"], "at": ev["at"]}
    elif kind in ("close", "release"):
        rows.pop(slug, None)
    elif kind == "claim" and slug in rows:
        rows[slug]["claimed_by"] = ev["from"]
    # note: no state change
    if rid:
        seen.append(rid)
        del seen[:-SEEN_CAP]


def load(transport: PointerTransport, team: str, agent: str) -> tuple[dict[str, Any], LoadState]:
    body, st = transport.read_classified(path(team, agent))
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


def save(transport: Any, team: str, agent: str, state: dict[str, Any]) -> bool:
    return bool(transport.save_doc(path(team, agent), json.dumps(state, indent=1)))
```

Note `_seen` is an underscore-prefixed internal list. G4 says the checkpoint has exactly five fields; `_seen` is persisted alongside them for idempotency. **Flag this for codex-reviewer** (open question 5 below): either the spec's five fields grow a sixth, or idempotency moves to the overlap window being zero. This plan keeps `_seen` and says so.

Add `save_doc` to `pointer_fake.py` (`self.saved[path] = text; return True`) and to `CliPointerTransport`:

```python
    def save_doc(self, path: str, text: str) -> bool:
        import tempfile, os
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            f.write(text); tmp = f.name
        try:
            rc, _o, _e = self._run("file", "upload", tmp, path)
            return rc == 0
        finally:
            os.unlink(tmp)
```

- [ ] **Step 4: Run — expect 8 passed**

- [ ] **Step 5: MUTATION-VERIFY** — delete the two lines `if rid and rid in seen: return`; expect `test_a_record_id_seen_before_is_not_applied_twice` to FAIL; restore.

- [ ] **Step 6: Commit** — `coord-fold: checkpoint v1 — five fields, apply rules, idempotent by record id (G4)`

---

### Task 6: `fold` — read forward, apply, persist; the first CLI-driven test

**Files:**
- Create: `packages/coord-fold/coord_fold/fold.py`
- Create: `packages/coord-fold/coord_fold/cli.py` (argparse skeleton + `fold`)
- Create: `packages/coord-fold/tests/test_cli_fold.py`

**Interfaces:**
- Produces: `fold.run(transport, team, agent, *, now: str, max_events: int = 5000) -> FoldOutcome` where `FoldOutcome = NamedTuple(state: dict, source: str, applied: int, unread: int, rc: int)`.
- Produces: `cli.main(argv: list[str] | None = None, *, transport=None) -> int`. Tests pass `transport=PointerFake(...)`; production constructs `CliPointerTransport(cli=[find_fulcra_cli()])`.
- Exit codes: 0 = checkpoint advanced and complete; 2 = refused (corrupt checkpoint / channel unresolved); 3 = UNKNOWN (event read failed, or `unread_events > 0`). **Never** 0 when the answer may be missing events.

- [ ] **Step 1: Write the failing CLI-driven tests (G9)**

```python
# packages/coord-fold/tests/test_cli_fold.py
"""G9: drive cli.main and assert on the STORED checkpoint. Three NameError-class
defects in this repo passed full unit suites and were caught only by running
the verb (AGENTS.md rule). Nothing here calls fold.run directly."""
import json

from coord_fold import checkpoint as cp
from coord_fold.cli import main
from pointer_fake import PointerFake

CFG = "team/r/_coord/bus-v4/records.json"
CFG_DOC = json.dumps({"data_type": "MomentAnnotation/x", "api_version": "v1alpha1"})


def _rec(kind, slug, at, rid, to="me", sender="boss", ptr=None):
    p = {"v": 1, "at": at, "from": sender, "to": to, "kind": kind, "slug": slug,
         "pri": "P1", "ptr": ptr or f"team/r/task/{slug}.md"}
    return {"id": rid, "recorded_at": at, "note": json.dumps(p)}


def _team(events):
    return PointerFake({CFG: CFG_DOC}, events)


def _ckpt(t):
    return json.loads(t.saved[cp.path("r", "me")])


def test_fold_from_fresh_applies_open_events_and_stores_the_checkpoint():
    t = _team([_rec("open", "a", "2026-09-04T10:00:00Z", "1"),
               _rec("open", "b", "2026-09-04T10:01:00Z", "2")])
    rc = main(["fold", "r", "--agent", "me", "--now", "2026-09-04T11:00:00Z"], transport=t)
    assert rc == 0
    st = _ckpt(t)
    assert set(st["open"]) == {"a", "b"} and st["cursor"] == "2026-09-04T11:00:00Z"


def test_a_close_after_an_open_removes_the_row_in_the_stored_checkpoint():
    t = _team([_rec("open", "a", "2026-09-04T10:00:00Z", "1"),
               _rec("close", "a", "2026-09-04T10:05:00Z", "2")])
    main(["fold", "r", "--agent", "me", "--now", "2026-09-04T11:00:00Z"], transport=t)
    assert _ckpt(t)["open"] == {}


def test_events_addressed_to_someone_else_do_not_land_in_my_checkpoint():
    t = _team([_rec("open", "a", "2026-09-04T10:00:00Z", "1", to="them")])
    main(["fold", "r", "--agent", "me", "--now", "2026-09-04T11:00:00Z"], transport=t)
    assert _ckpt(t)["open"] == {}


def test_broadcast_events_do_land():
    t = _team([_rec("open", "a", "2026-09-04T10:00:00Z", "1", to="all")])
    main(["fold", "r", "--agent", "me", "--now", "2026-09-04T11:00:00Z"], transport=t)
    assert "a" in _ckpt(t)["open"]


def test_a_second_fold_reads_only_forward_and_does_not_reapply():
    t = _team([_rec("open", "a", "2026-09-04T10:00:00Z", "1")])
    main(["fold", "r", "--agent", "me", "--now", "2026-09-04T11:00:00Z"], transport=t)
    t.events.append(_rec("close", "a", "2026-09-04T11:30:00Z", "2"))
    main(["fold", "r", "--agent", "me", "--now", "2026-09-04T12:00:00Z"], transport=t)
    st = _ckpt(t)
    assert st["open"] == {} and st["cursor"] == "2026-09-04T12:00:00Z"


def test_a_failed_event_read_does_NOT_advance_the_cursor_and_exits_3(capsys):
    t = _team([]); t.fail_events = True
    rc = main(["fold", "r", "--agent", "me", "--now", "2026-09-04T11:00:00Z"], transport=t)
    assert rc == 3
    assert cp.path("r", "me") not in t.saved, "cursor advanced past a window it never read"
    assert "degraded" not in capsys.readouterr().out.lower()


def test_more_events_than_the_cap_leaves_a_bounded_unread_count_and_exits_3():
    evs = [_rec("open", f"s{i}", f"2026-09-04T10:{i:02d}:00Z", str(i)) for i in range(7)]
    t = _team(evs)
    rc = main(["fold", "r", "--agent", "me", "--now", "2026-09-04T11:00:00Z",
               "--max-events", "5"], transport=t)
    st = _ckpt(t)
    assert rc == 3 and st["unread_events"] == 2 and len(st["open"]) == 5
    # the cursor stops at the last APPLIED event, not at now
    assert st["cursor"] == "2026-09-04T10:04:00Z"


def test_a_corrupt_checkpoint_is_refused_and_left_untouched(capsys):
    t = _team([]); t.docs[cp.path("r", "me")] = "{not json"
    rc = main(["fold", "r", "--agent", "me", "--now", "2026-09-04T11:00:00Z"], transport=t)
    assert rc == 2 and cp.path("r", "me") not in t.saved
    assert "corrupt" in capsys.readouterr().err


def test_an_unresolved_channel_is_refused():
    t = PointerFake({}, [])
    assert main(["fold", "r", "--agent", "me", "--now", "2026-09-04T11:00:00Z"], transport=t) == 2
```

- [ ] **Step 2: Run — expect ImportError on `coord_fold.cli`**

- [ ] **Step 3: Implement `fold.py`**

```python
# packages/coord-fold/coord_fold/fold.py
"""One pass: read forward from the cursor, apply, persist, report (spec §3.3).
Cost is O(new events). Zero directory listings, always — the transport has no
method that could list one."""
from __future__ import annotations

from typing import Any, NamedTuple

from . import channel, checkpoint as cp, events
from .transport import PointerTransport, TransportUnavailable

OVERLAP_SECONDS = 5
BROADCAST = "all"


class FoldOutcome(NamedTuple):
    state: dict[str, Any]
    source: str          # fresh | ok
    applied: int
    unread: int
    rc: int


class FoldRefused(RuntimeError):
    pass


def _minus_overlap(iso: str) -> str:
    from datetime import datetime, timedelta
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return (dt - timedelta(seconds=OVERLAP_SECONDS)).strftime("%Y-%m-%dT%H:%M:%SZ")


def run(transport: PointerTransport, team: str, agent: str, *, now: str,
        max_events: int = 5000) -> FoldOutcome:
    cfg = channel.resolve(transport, team)          # raises ChannelUnresolved
    state, source = cp.load(transport, team, agent)
    if source == "corrupt":
        raise FoldRefused("checkpoint is corrupt — left untouched for forensics; repair or reseed it explicitly")
    if source == "error":
        raise TransportUnavailable("checkpoint unreadable")
    if source == "fresh":
        state = cp.empty(now)
        since = "1970-01-01T00:00:00Z"
    else:
        since = _minus_overlap(state["cursor"])

    applied = 0
    unread = 0
    last_at = state["cursor"] if source == "ok" else now
    for rec in transport.read_events(cfg["data_type"], since):   # may raise TransportUnavailable
        ev = events.parse_event(rec)
        if ev is None:
            continue
        if ev["to"] not in (agent, BROADCAST) and ev["from"] != agent:
            continue
        if applied >= max_events:
            unread += 1
            continue
        cp.apply(state, ev)
        applied += 1
        last_at = ev.get("recorded_at") or ev["at"]
    state["unread_events"] = unread
    state["cursor"] = last_at if unread else now
    if not cp.save(transport, team, agent, state):
        raise TransportUnavailable("checkpoint save failed")
    return FoldOutcome(state, source, applied, unread, 3 if unread else 0)
```

- [ ] **Step 4: Implement `cli.py` (skeleton + `fold`)**

```python
# packages/coord-fold/coord_fold/cli.py
"""Five verbs. emit / fold / claim / close / status. Nothing else lives here,
and this file stays under the 400-line ceiling."""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

from . import fold as fold_mod
from .channel import ChannelUnresolved
from .transport import CliPointerTransport, TransportUnavailable

RC_OK, RC_REFUSED, RC_UNKNOWN = 0, 2, 3


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _default_transport() -> CliPointerTransport:
    from fulcra_common.client import find_fulcra_cli
    cli = find_fulcra_cli()
    if not cli:
        print("coord-fold: fulcra-api CLI not found on PATH", file=sys.stderr)
        raise SystemExit(RC_REFUSED)
    return CliPointerTransport(cli=[cli])


def _render_open(state: dict) -> str:
    rows = sorted(state["open"].items(), key=lambda kv: (kv[1]["pri"], kv[1]["at"]))
    lines = [f"  [{r['pri']}] {slug}  from={r['from']}  ptr={r['ptr']}"
             + (f"  claimed_by={r['claimed_by']}" if r.get("claimed_by") else "")
             for slug, r in rows]
    return "\n".join(lines) if lines else "  (nothing open)"


def _report_unknowns(state: dict) -> None:
    if state.get("unread_events"):
        print(f"fold: {state['unread_events']} events unread past {state['cursor']} — "
              f"the answer is missing those", file=sys.stderr)
    for slug in state.get("unreadable_pointers", []):
        print(f"fold: pointer for {slug} unreadable — that one row is UNKNOWN", file=sys.stderr)


def cmd_fold(args, transport) -> int:
    try:
        out = fold_mod.run(transport, args.team, args.agent, now=args.now,
                           max_events=args.max_events)
    except ChannelUnresolved as exc:
        print(f"fold: refused — {exc}", file=sys.stderr); return RC_REFUSED
    except fold_mod.FoldRefused as exc:
        print(f"fold: refused — {exc}", file=sys.stderr); return RC_REFUSED
    except TransportUnavailable as exc:
        print(f"fold: UNKNOWN — event read did not complete ({exc}); cursor not advanced",
              file=sys.stderr); return RC_UNKNOWN
    print(f"fold [{args.agent}] cursor={out.state['cursor']} applied={out.applied} "
          f"open={len(out.state['open'])} source={out.source}")
    print(_render_open(out.state))
    _report_unknowns(out.state)
    return out.rc


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="coord-fold")
    sub = p.add_subparsers(dest="verb", required=True)
    f = sub.add_parser("fold", help="advance my checkpoint, print what I owe")
    f.add_argument("team"); f.add_argument("--agent", required=True)
    f.add_argument("--now", default=None); f.add_argument("--max-events", type=int, default=5000)
    f.set_defaults(func=cmd_fold)
    return p


def main(argv: list[str] | None = None, *, transport=None) -> int:
    args = build_parser().parse_args(argv)
    if getattr(args, "now", None) is None:
        args.now = _now()
    t = transport if transport is not None else _default_transport()
    return int(args.func(args, t))


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 5: Run — expect 9 passed**

- [ ] **Step 6: MUTATION-VERIFY (two, because two behaviours are load-bearing)**

```bash
# (a) advance the cursor even when the read failed — the exact bug the old transport's docstring names
python - <<'PY'
p="coord_fold/fold.py"; s=open(p).read()
s=s.replace("    for rec in transport.read_events(cfg[\"data_type\"], since):   # may raise TransportUnavailable",
 "    try:\n        _it=list(transport.read_events(cfg[\"data_type\"], since))\n    except TransportUnavailable:\n        _it=[]\n    for rec in _it:",1)
open(p,"w").write(s)
PY
python -m pytest tests/test_cli_fold.py -q   # expect test_a_failed_event_read_... FAIL
git checkout coord_fold/fold.py
# (b) drop the addressee filter
python - <<'PY'
p="coord_fold/fold.py"; s=open(p).read()
s=s.replace('        if ev["to"] not in (agent, BROADCAST) and ev["from"] != agent:\n            continue\n', '',1)
open(p,"w").write(s)
PY
python -m pytest tests/test_cli_fold.py -q   # expect test_events_addressed_to_someone_else... FAIL
git checkout coord_fold/fold.py
```

- [ ] **Step 7: Commit** — `coord-fold: fold verb — read forward, apply, persist; CLI-driven tests on the stored checkpoint (G9)`

---

### Task 7: `emit`

**Files:**
- Modify: `packages/coord-fold/coord_fold/cli.py`
- Create: `packages/coord-fold/tests/test_cli_emit.py`

- [ ] **Step 1: Write the failing tests**

```python
# packages/coord-fold/tests/test_cli_emit.py
import json
from coord_fold.cli import main
from pointer_fake import PointerFake

CFG = "team/r/_coord/bus-v4/records.json"
CFG_DOC = json.dumps({"data_type": "MomentAnnotation/x", "api_version": "v1alpha1"})


def test_emit_writes_one_v1_open_record_with_the_eight_fields():
    t = PointerFake({CFG: CFG_DOC}, [])
    rc = main(["emit", "r", "--from", "boss", "--to", "me", "--kind", "open",
               "--slug", "p1-x", "--pri", "P1", "--ptr", "team/r/task/p1-x.md",
               "--at", "2026-09-04T13:45:00Z"], transport=t)
    assert rc == 0 and len(t.written) == 1
    p = t.written[0]["payload"]
    assert set(p) == {"v", "at", "from", "to", "kind", "slug", "pri", "ptr"}
    assert t.written[0]["channel"] == "MomentAnnotation/x"


def test_emit_open_without_ptr_is_refused_and_writes_nothing(capsys):
    t = PointerFake({CFG: CFG_DOC}, [])
    rc = main(["emit", "r", "--from", "boss", "--to", "me", "--kind", "open",
               "--slug", "s", "--pri", "P1"], transport=t)
    assert rc == 2 and not t.written and "ptr is required" in capsys.readouterr().err


def test_emit_then_fold_sees_the_event():
    t = PointerFake({CFG: CFG_DOC}, [])
    main(["emit", "r", "--from", "boss", "--to", "me", "--kind", "open", "--slug", "s",
          "--pri", "P2", "--ptr", "x.md", "--at", "2026-09-04T13:45:00Z"], transport=t)
    main(["fold", "r", "--agent", "me", "--now", "2026-09-04T14:00:00Z"], transport=t)
    from coord_fold import checkpoint as cp
    assert "s" in json.loads(t.saved[cp.path("r", "me")])["open"]


def test_a_failed_write_exits_3_not_0():
    t = PointerFake({CFG: CFG_DOC}, [])
    t.write_event = lambda *a, **k: False
    rc = main(["emit", "r", "--from", "boss", "--to", "me", "--kind", "note",
               "--slug", "s", "--pri", "P3"], transport=t)
    assert rc == 3
```

- [ ] **Step 2: Run — expect argparse error (no `emit` verb)**

- [ ] **Step 3: Implement** — add to `cli.py`:

```python
def cmd_emit(args, transport) -> int:
    from . import channel, events
    try:
        cfg = channel.resolve(transport, args.team)
        payload = events.build_payload(at=args.at, sender=args.sender, to=args.to,
                                       kind=args.kind, slug=args.slug, pri=args.pri,
                                       ptr=args.ptr)
    except (ChannelUnresolved, ValueError) as exc:
        print(f"emit: refused — {exc}", file=sys.stderr); return RC_REFUSED
    if not transport.write_event(cfg, payload, sender=args.sender):
        print("emit: UNKNOWN — the record write did not confirm; nothing may have been delivered",
              file=sys.stderr); return RC_UNKNOWN
    print(f"emit {payload['kind']} {payload['slug']} -> {payload['to']}")
    return RC_OK
```

and in `build_parser`:

```python
    e = sub.add_parser("emit", help="write one annotation event")
    e.add_argument("team")
    e.add_argument("--from", dest="sender", required=True); e.add_argument("--to", required=True)
    e.add_argument("--kind", required=True); e.add_argument("--slug", required=True)
    e.add_argument("--pri", required=True); e.add_argument("--ptr", default=None)
    e.add_argument("--at", default=None)
    e.set_defaults(func=cmd_emit)
```

and in `main`, after the `now` default: `if getattr(args, "at", None) is None and hasattr(args, "at"): args.at = _now()`.

- [ ] **Step 4: Run — expect 4 passed**

- [ ] **Step 5: MUTATION-VERIFY** — change `return RC_UNKNOWN` in `cmd_emit` to `return RC_OK`; expect `test_a_failed_write_exits_3_not_0` FAIL; restore.

- [ ] **Step 6: Commit** — `coord-fold: emit verb`

---

### Task 8: `claim` and `close`

**Files:**
- Modify: `packages/coord-fold/coord_fold/cli.py`
- Create: `packages/coord-fold/tests/test_cli_claim_close.py`

Both are `emit` with a fixed kind plus one rule each: `claim` requires the slug to be open in **my** checkpoint (you cannot claim what you do not owe); `close` requires `--evidence <ptr>` and **reads it** via `read_classified` before emitting — a close whose evidence is absent or unreadable is refused (the old bus's ghost-close lesson, kept).

- [ ] **Step 1: Write the failing tests**

```python
# packages/coord-fold/tests/test_cli_claim_close.py
import json
from coord_fold import checkpoint as cp
from coord_fold.cli import main
from pointer_fake import PointerFake

CFG = "team/r/_coord/bus-v4/records.json"
CFG_DOC = json.dumps({"data_type": "MomentAnnotation/x", "api_version": "v1alpha1"})
T0, T1, T2 = "2026-09-04T10:00:00Z", "2026-09-04T11:00:00Z", "2026-09-04T12:00:00Z"


def _open_event(slug, to="me"):
    p = {"v": 1, "at": T0, "from": "boss", "to": to, "kind": "open", "slug": slug,
         "pri": "P1", "ptr": f"team/r/task/{slug}.md"}
    return {"id": slug, "recorded_at": T0, "note": json.dumps(p)}


def _folded(slug="s"):
    t = PointerFake({CFG: CFG_DOC}, [_open_event(slug)])
    main(["fold", "r", "--agent", "me", "--now", T1], transport=t)
    return t


def test_claim_emits_a_claim_event_and_the_next_fold_annotates_the_row():
    t = _folded()
    assert main(["claim", "r", "s", "--agent", "me", "--at", T1], transport=t) == 0
    assert t.written[-1]["payload"]["kind"] == "claim"
    main(["fold", "r", "--agent", "me", "--now", T2], transport=t)
    assert json.loads(t.saved[cp.path("r", "me")])["open"]["s"]["claimed_by"] == "me"


def test_claim_of_a_slug_i_do_not_owe_is_refused(capsys):
    t = _folded()
    assert main(["claim", "r", "not-mine", "--agent", "me"], transport=t) == 2
    assert "not open" in capsys.readouterr().err and not any(
        w["payload"]["kind"] == "claim" for w in t.written)


def test_close_reads_the_evidence_pointer_then_emits_and_the_row_is_gone():
    t = _folded(); t.docs["team/r/_coord/responses/s/reply.md"] = "done"
    rc = main(["close", "r", "s", "--agent", "me", "--evidence",
               "team/r/_coord/responses/s/reply.md", "--at", T1], transport=t)
    assert rc == 0 and t.written[-1]["payload"]["kind"] == "close"
    main(["fold", "r", "--agent", "me", "--now", T2], transport=t)
    assert json.loads(t.saved[cp.path("r", "me")])["open"] == {}


def test_close_with_ABSENT_evidence_is_refused_and_says_absent(capsys):
    t = _folded()
    rc = main(["close", "r", "s", "--agent", "me", "--evidence", "nope.md"], transport=t)
    assert rc == 2 and "absent" in capsys.readouterr().err
    assert not any(w["payload"]["kind"] == "close" for w in t.written)


def test_close_with_UNREADABLE_evidence_is_UNKNOWN_not_refused_not_done(capsys):
    """absent and unreadable are different words (the U2 lesson)."""
    t = _folded(); t.fail_reads = True
    rc = main(["close", "r", "s", "--agent", "me", "--evidence", "x.md"], transport=t)
    assert rc == 3 and "unreadable" in capsys.readouterr().err
```

- [ ] **Step 2: Run — expect argparse errors**

- [ ] **Step 3: Implement** — add to `cli.py`:

```python
def _emit_kind(transport, team, *, sender, to, kind, slug, pri, ptr, at) -> int:
    from . import channel, events
    try:
        cfg = channel.resolve(transport, team)
        payload = events.build_payload(at=at, sender=sender, to=to, kind=kind,
                                       slug=slug, pri=pri, ptr=ptr)
    except (ChannelUnresolved, ValueError) as exc:
        print(f"{kind}: refused — {exc}", file=sys.stderr); return RC_REFUSED
    if not transport.write_event(cfg, payload, sender=sender):
        print(f"{kind}: UNKNOWN — write did not confirm", file=sys.stderr); return RC_UNKNOWN
    print(f"{kind} {slug}"); return RC_OK


def _owed_row(transport, team, agent, slug):
    from . import checkpoint as cp
    state, src = cp.load(transport, team, agent)
    if src != "ok" or slug not in state["open"]:
        return None
    return state["open"][slug]


def cmd_claim(args, transport) -> int:
    row = _owed_row(transport, args.team, args.agent, args.slug)
    if row is None:
        print(f"claim: refused — {args.slug} is not open in {args.agent}'s checkpoint "
              f"(fold first, or you do not owe it)", file=sys.stderr); return RC_REFUSED
    return _emit_kind(transport, args.team, sender=args.agent, to=row["from"], kind="claim",
                      slug=args.slug, pri=row["pri"], ptr=row["ptr"], at=args.at)


def cmd_close(args, transport) -> int:
    row = _owed_row(transport, args.team, args.agent, args.slug)
    if row is None:
        print(f"close: refused — {args.slug} is not open in {args.agent}'s checkpoint",
              file=sys.stderr); return RC_REFUSED
    _body, st = transport.read_classified(args.evidence)
    if st == "absent":
        print(f"close: refused — evidence {args.evidence} is absent", file=sys.stderr); return RC_REFUSED
    if st == "error":
        print(f"close: UNKNOWN — evidence {args.evidence} unreadable; not closing on a read that "
              f"did not answer", file=sys.stderr); return RC_UNKNOWN
    return _emit_kind(transport, args.team, sender=args.agent, to=row["from"], kind="close",
                      slug=args.slug, pri=row["pri"], ptr=args.evidence, at=args.at)
```

parser additions:

```python
    for name, fn, extra in (("claim", cmd_claim, ()), ("close", cmd_close, ("--evidence",))):
        sp = sub.add_parser(name)
        sp.add_argument("team"); sp.add_argument("slug"); sp.add_argument("--agent", required=True)
        for x in extra: sp.add_argument(x, required=True)
        sp.add_argument("--at", default=None); sp.set_defaults(func=fn)
```

- [ ] **Step 4: Run — expect 5 passed**

- [ ] **Step 5: MUTATION-VERIFY** — in `cmd_close`, change the `st == "error"` branch to fall through to `_emit_kind`; expect `test_close_with_UNREADABLE_evidence_...` FAIL; restore.

- [ ] **Step 6: Commit** — `coord-fold: claim + close verbs; close reads its evidence and refuses absent, reports unreadable`

---

### Task 9: `status`

**Files:**
- Modify: `packages/coord-fold/coord_fold/cli.py`
- Create: `packages/coord-fold/tests/test_cli_status.py`

`status` reads the checkpoint and prints it. It **does not fold** and **does not read pointers** — it is the "what does the fold say right now" verb, O(1) transport. Exit 0 if the checkpoint has no unknowns, 3 if `unread_events > 0` or `unreadable_pointers` non-empty, 2 if the checkpoint is corrupt or fresh (never folded).

- [ ] **Step 1: Write the failing tests**

```python
# packages/coord-fold/tests/test_cli_status.py
import json
from coord_fold import checkpoint as cp
from coord_fold.cli import main
from pointer_fake import PointerFake


def _t(state):
    t = PointerFake({}, []); t.docs[cp.path("r", "me")] = json.dumps(state); return t


def test_status_prints_open_rows_and_exits_0_when_nothing_is_unknown(capsys):
    st = cp.empty("T"); st["open"]["s"] = {"pri": "P1", "from": "boss", "ptr": "x.md", "at": "T"}
    assert main(["status", "r", "--agent", "me"], transport=_t(st)) == 0
    assert "[P1] s" in capsys.readouterr().out


def test_status_exits_3_and_names_the_unread_count(capsys):
    st = cp.empty("T"); st["unread_events"] = 12
    assert main(["status", "r", "--agent", "me"], transport=_t(st)) == 3
    assert "12 events unread" in capsys.readouterr().err


def test_status_exits_3_and_names_each_unreadable_pointer(capsys):
    st = cp.empty("T"); st["unreadable_pointers"] = ["s9"]
    assert main(["status", "r", "--agent", "me"], transport=_t(st)) == 3
    assert "pointer for s9 unreadable" in capsys.readouterr().err


def test_status_on_a_never_folded_agent_says_so_and_exits_2(capsys):
    assert main(["status", "r", "--agent", "me"], transport=PointerFake({}, [])) == 2
    assert "never folded" in capsys.readouterr().err


def test_status_performs_no_event_read():
    t = PointerFake({}, []); t.docs[cp.path("r", "me")] = json.dumps(cp.empty("T"))
    t.read_events = lambda *a: (_ for _ in ()).throw(AssertionError("status must not read events"))
    assert main(["status", "r", "--agent", "me"], transport=t) == 0
```

- [ ] **Step 2: Run — expect argparse error**

- [ ] **Step 3: Implement**

```python
def cmd_status(args, transport) -> int:
    from . import checkpoint as cp
    state, src = cp.load(transport, args.team, args.agent)
    if src == "fresh":
        print(f"status: {args.agent} has never folded — run `coord-fold fold`", file=sys.stderr); return RC_REFUSED
    if src == "corrupt":
        print("status: refused — checkpoint corrupt", file=sys.stderr); return RC_REFUSED
    if src == "error":
        print("status: UNKNOWN — checkpoint unreadable", file=sys.stderr); return RC_UNKNOWN
    print(f"status [{args.agent}] cursor={state['cursor']} open={len(state['open'])}")
    print(_render_open(state))
    _report_unknowns(state)
    unknown = bool(state.get("unread_events")) or bool(state.get("unreadable_pointers"))
    return RC_UNKNOWN if unknown else RC_OK
```

parser: `s = sub.add_parser("status"); s.add_argument("team"); s.add_argument("--agent", required=True); s.set_defaults(func=cmd_status)`

- [ ] **Step 4: Run — expect 5 passed**

- [ ] **Step 5: MUTATION-VERIFY** — change the final line to `return RC_OK`; expect the two exits-3 tests to FAIL; restore.

- [ ] **Step 6: Commit** — `coord-fold: status verb — reports the checkpoint and its two bounded unknowns, reads no events`

---

### Task 10: The degradation-vocabulary test (G11)

**Files:**
- Create: `packages/coord-fold/tests/test_no_degraded_vocabulary.py`

- [ ] **Step 1: Write the test**

```python
# packages/coord-fold/tests/test_no_degraded_vocabulary.py
"""G11 / spec §8: the old vocabulary is gone by construction. If the token
`degraded` appears anywhere in the package, someone reintroduced a fold that
gives up part way through a corpus it should not have been walking."""
import pathlib
import coord_fold

PKG_DIR = pathlib.Path(coord_fold.__file__).parent


def test_the_token_degraded_never_appears_in_the_package():
    hits = {p.name for p in PKG_DIR.glob("*.py") if "degraded" in p.read_text().lower()}
    assert not hits, hits
```

- [ ] **Step 2: Run — expect pass** (Tasks 6–9 never wrote the word; this is the guard that keeps it so). The CI step from Task 2 now resolves all three files and goes green.

- [ ] **Step 3: MUTATION-VERIFY** — append `# degraded` to `coord_fold/fold.py`; expect FAIL; restore.

- [ ] **Step 4: Commit** — `coord-fold: degradation vocabulary is absent by construction (G11)`

---

### Task 11: Seed export — the old engine emits the 253 open obligations (old side)

**Files:**
- Modify: `packages/coord-engine/coord_engine/cli.py` (new `obligations export-open` subverb; register in `tests/test_activity_covers_every_write_verb.py` EXPECTED_WRITES)
- Create: `packages/coord-engine/tests/test_obligations_export_open.py`

This is on the **old** side, which may enumerate and may import anything. It reads the old stream fold's open set (`stream_fold.load_state` — itself zero-enumeration) and writes one bus-v4 `open` event per slug via the old `transport.record_write`, using the **new** payload shape. It is idempotent via a seed marker doc so it can be re-run. It imports nothing from `coord_fold` — the payload shape is eight fields and is written out literally, with a comment pointing at `coord_fold/events.py` as the reference.

- [ ] **Step 1: Write the failing CLI-driven test**

```python
# packages/coord-engine/tests/test_obligations_export_open.py
import json
from coord_engine import cli, stream_fold
from coord_engine_test_helpers import FakeTransport

V3 = json.dumps({"data_type": "MomentAnnotation/v3", "api_version": "v1alpha1"})
V4 = json.dumps({"data_type": "MomentAnnotation/v4", "api_version": "v1alpha1"})


class _Bus(FakeTransport):
    def __init__(self):
        super().__init__(); self.writes = []
    def record_write(self, data_type, api_version, note, source, **kw):
        self.writes.append((data_type, json.loads(note))); return True


def _team_with_open(slugs):
    t = _Bus()
    t.put("team/r/_coord/bus-v3/records.json", V3)
    t.put("team/r/_coord/bus-v4/records.json", V4)
    st = stream_fold.empty_state(__import__("datetime").datetime(2026, 9, 4, tzinfo=__import__("datetime").timezone.utc))
    for s in slugs:
        st["open"][s] = {"pri": "P1", "from": "boss", "ptr": f"task/{s}.md", "at": "2026-09-01T00:00:00Z"}
    t.put(stream_fold.state_path("r", "me"), json.dumps(st))
    return t


def test_export_open_emits_one_v4_open_event_per_open_slug():
    t = _team_with_open(["a", "b"])
    assert cli.main(["obligations", "export-open", "r", "--agent", "me"], transport=t) == 0
    v4 = [p for d, p in t.writes if d == "MomentAnnotation/v4"]
    assert {p["slug"] for p in v4} == {"a", "b"}
    assert all(p["kind"] == "open" and p["v"] == 1 and p["ptr"] for p in v4)
    assert all(set(p) == {"v", "at", "from", "to", "kind", "slug", "pri", "ptr"} for p in v4)


def test_export_open_is_idempotent_via_a_seed_marker():
    t = _team_with_open(["a"])
    cli.main(["obligations", "export-open", "r", "--agent", "me"], transport=t)
    cli.main(["obligations", "export-open", "r", "--agent", "me"], transport=t)
    assert sum(1 for d, _ in t.writes if d == "MomentAnnotation/v4") == 1
    assert any("/bus-v4/seeded/" in p for p in t.store)


def test_export_open_without_a_v4_config_refuses_and_writes_nothing():
    t = _team_with_open(["a"]); del t.store["team/r/_coord/bus-v4/records.json"]
    assert cli.main(["obligations", "export-open", "r", "--agent", "me"], transport=t) != 0
    assert not t.writes
```

- [ ] **Step 2: Run — expect argparse error (no `export-open`)**

- [ ] **Step 3: Implement** — in `coord_engine/cli.py`, next to `cmd_obligations`:

```python
def cmd_obligations_export_open(args: argparse.Namespace, transport: Any) -> int:
    """SEED (spec §5.1 step 1). Emit the currently-open obligations of ONE agent
    as bus-v4 `open` events, once. Payload shape is the eight fields defined in
    packages/coord-fold/coord_fold/events.py, written literally here so this
    package never imports coord_fold (dependency direction: old -> new never)."""
    from . import stream_fold
    v4_path = f"team/{args.team}/_coord/bus-v4/records.json"
    raw = transport.read(v4_path)
    if raw is None:
        print("export-open: refused — no bus-v4 config; nothing seeded", file=sys.stderr); return 2
    v4 = json.loads(raw)
    marker = f"team/{args.team}/_coord/bus-v4/seeded/{args.agent}.md"
    if transport.read(marker) is not None:
        print(f"export-open: {args.agent} already seeded ({marker})"); return 0
    state, src = stream_fold.load_state(transport, args.team, args.agent, datetime.now(timezone.utc))
    if src == "invalid":
        print("export-open: refused — old fold state invalid", file=sys.stderr); return 2
    now = _now()
    n = 0
    for slug, row in sorted(state.get("open", {}).items()):
        payload = {"v": 1, "at": now, "from": row.get("from") or "seed", "to": args.agent,
                   "kind": "open", "slug": slug, "pri": row.get("pri") or "P2",
                   "ptr": row.get("ptr") or _task_path(args.team, slug)}
        if not transport.record_write(v4["data_type"], v4["api_version"],
                                      json.dumps(payload, separators=(",", ":")),
                                      "seed", recorded_at=now):
            print(f"export-open: write failed at {slug} after {n} — NOT marking seeded", file=sys.stderr); return 3
        n += 1
    transport.write(marker, okf.render_frontmatter({"type": "Seed", "agent": args.agent,
                                                    "count": n, "timestamp": now}) + "\n")
    print(f"export-open: seeded {n} open obligation(s) for {args.agent} onto bus-v4"); return 0
```

Register under the `obligations` subparser and add `"cmd_obligations_export_open"` to `EXPECTED_WRITES` in `tests/test_activity_covers_every_write_verb.py`.

- [ ] **Step 4: Run — expect 3 passed, and `test_every_registered_command_is_classified` still green**

- [ ] **Step 5: MUTATION-VERIFY** — remove the `if transport.read(marker) is not None: ... return 0` guard; expect the idempotency test FAIL; restore.

- [ ] **Step 6: Commit** — `coord-engine: obligations export-open — seed bus-v4 with one agent's open set, once (spec §5.1 step 1)`

---

### Task 12: Dual-emit — the one hook in the old engine (old side)

**Files:**
- Create: `packages/coord-engine/coord_engine/dual_emit.py`
- Modify: `packages/coord-engine/coord_engine/records.py` (one call at the end of `emit_event`)
- Create: `packages/coord-engine/tests/test_dual_emit.py`

The spec says the old engine gains **one hook**. `records.emit_event` is the single choke point every old-bus transition already passes through (it is what `tell`, `respond`, `escalate` and the settle-time close all call). The mirror maps old kinds to new: `directive → open`, `response → close` (the `--closes` target slug is the closed slug), `claim → claim`, `verdict → note`. No bus-v4 config → silent no-op.

- [ ] **Step 1: Write the failing test**

```python
# packages/coord-engine/tests/test_dual_emit.py
import json
from coord_engine import records
from coord_engine_test_helpers import FakeTransport

V3 = {"data_type": "MomentAnnotation/v3", "api_version": "v1alpha1"}


class _Bus(FakeTransport):
    def __init__(self):
        super().__init__(); self.writes = []
    def record_write(self, data_type, api_version, note, source, **kw):
        self.writes.append((data_type, json.loads(note))); return True


def _v4(t):
    t.put("team/r/_coord/bus-v4/records.json",
          json.dumps({"data_type": "MomentAnnotation/v4", "api_version": "v1alpha1"}))


def test_a_directive_on_v3_is_mirrored_as_an_open_on_v4():
    t = _Bus(); _v4(t)
    records.emit_event(t, V3, sender="boss", to="me", kind="directive", priority="P1",
                       slug="s", ptr="task/s.md", team="r")
    v4 = [p for d, p in t.writes if d == "MomentAnnotation/v4"]
    assert len(v4) == 1 and v4[0]["kind"] == "open" and v4[0]["slug"] == "s" and v4[0]["v"] == 1


def test_a_response_that_closes_is_mirrored_as_a_close_of_the_closed_slug():
    t = _Bus(); _v4(t)
    records.emit_event(t, V3, sender="me", to="boss", kind="response", priority="P2",
                       slug="my-reply", ptr="responses/x.md", team="r", closes="s")
    v4 = [p for d, p in t.writes if d == "MomentAnnotation/v4"]
    assert v4 and v4[0]["kind"] == "close" and v4[0]["slug"] == "s"


def test_no_v4_config_means_no_mirror_and_no_error():
    t = _Bus()
    assert records.emit_event(t, V3, sender="boss", to="me", kind="directive",
                              priority="P1", slug="s", ptr="task/s.md", team="r")
    assert all(d == "MomentAnnotation/v3" for d, _ in t.writes)


def test_a_mirror_failure_never_fails_the_v3_write():
    t = _Bus(); _v4(t)
    orig = t.record_write
    t.record_write = lambda d, *a, **k: (False if d.endswith("v4") else orig(d, *a, **k))
    assert records.emit_event(t, V3, sender="boss", to="me", kind="directive",
                              priority="P1", slug="s", ptr="task/s.md", team="r") is True
```

- [ ] **Step 2: Run — expect `TypeError: unexpected keyword 'closes'` / mirror tests FAIL**

- [ ] **Step 3: Implement**

```python
# packages/coord-engine/coord_engine/dual_emit.py
"""The ONE hook (spec §5.1 step 2). Every old-bus transition also lands on
bus-v4 in the new payload shape. Best-effort: a mirror failure is reported and
never fails the v3 write, because during shadow the old bus is still the
authority. Payload shape mirrors packages/coord-fold/coord_fold/events.py
literally; this module must never import coord_fold."""
from __future__ import annotations

import json
import sys
from typing import Any, Optional

KIND_MAP = {"directive": "open", "response": "close", "claim": "claim", "verdict": "note"}


def mirror(transport: Any, team: Optional[str], *, sender: str, to: str, kind: str,
           priority: str, slug: str, ptr: Optional[str], at: str,
           closes: Optional[str] = None) -> Optional[bool]:
    if not team:
        return None
    raw = transport.read(f"team/{team}/_coord/bus-v4/records.json")
    if raw is None:
        return None                       # no v4 bus for this team: silent no-op
    new_kind = KIND_MAP.get(kind)
    if new_kind is None:
        return None
    target = closes if (new_kind == "close" and closes) else slug
    payload = {"v": 1, "at": at, "from": sender, "to": to, "kind": new_kind,
               "slug": target, "pri": priority, "ptr": ptr}
    try:
        cfg = json.loads(raw)
        ok = transport.record_write(cfg["data_type"], cfg["api_version"],
                                    json.dumps(payload, separators=(",", ":")),
                                    sender, recorded_at=at)
    except Exception as exc:  # noqa: BLE001 — best-effort by design
        print(f"dual-emit: mirror to bus-v4 failed ({exc}); v3 write unaffected", file=sys.stderr)
        return False
    if not ok:
        print("dual-emit: mirror to bus-v4 did not confirm; v3 write unaffected", file=sys.stderr)
    return ok
```

In `records.emit_event`: add parameter `closes: Optional[str] = None`, and **immediately before its final `return`** of the v3 result, add:

```python
    from . import dual_emit
    dual_emit.mirror(transport, team, sender=sender, to=to, kind=kind, priority=priority,
                     slug=slug, ptr=ptr, at=recorded_at or _now_iso(), closes=closes)
```

(using whatever the function's existing timestamp helper is named — read `emit_event`'s body at `records.py:798` and use its own `recorded_at` default.) Then thread `closes=` through the ONE caller that closes: `_close_answered_directive` in `cli.py` passes `closes=target` when it emits the response. Add `"cmd_..."`-level classification if the activity test complains — `dual_emit.mirror` is not a command so it should not.

- [ ] **Step 4: Run — expect 4 passed; then the FULL coord-engine suite must still pass (the hook touches every emit)**

Run: `python -m pytest tests/ -q` — expected: only the two known pre-existing env failures.

- [ ] **Step 5: MUTATION-VERIFY** — in `dual_emit.mirror`, change `return None` under `if raw is None:` to proceed with a hardcoded `cfg`; expect `test_no_v4_config_means_no_mirror_and_no_error` FAIL; restore.

- [ ] **Step 6: Commit** — `coord-engine: dual-emit — the one hook mirroring v3 transitions onto bus-v4 (spec §5.1 step 2)`

---

### Task 13: Comparator — shadow both engines and log agreement (old side)

**Files:**
- Modify: `packages/coord-engine/coord_engine/cli.py` (new `obligations compare-to-fold`)
- Create: `packages/coord-engine/tests/test_obligations_compare_to_fold.py`
- Create: `docs/coord/COORD-FOLD-CUTOVER.md`

Old side reads the **new** checkpoint by a plain `transport.read` of `team/<team>/member/<agent>/fold/checkpoint.json` (a path, not a listing) and diffs `open` slug sets against the old stream fold's. It appends one line per run to `team/<team>/_coord/bus-v4/comparator/<agent>.log`: `<ts> AGREE n=<k>` or `<ts> DIVERGE only_old=[..] only_new=[..]`. Exit 0 on agree, 1 on diverge, 3 if either side is unreadable. **Divergence is a finding, never a number to tune** (spec §5.1 step 3).

- [ ] **Step 1: Write the failing test**

```python
# packages/coord-engine/tests/test_obligations_compare_to_fold.py
import json
from coord_engine import cli, stream_fold
from coord_engine_test_helpers import FakeTransport
import datetime as dt

NOW = dt.datetime(2026, 9, 4, tzinfo=dt.timezone.utc)
CK = "team/r/member/me/fold/checkpoint.json"


def _t(old_open, new_open):
    t = FakeTransport()
    st = stream_fold.empty_state(NOW)
    for s in old_open: st["open"][s] = {"pri": "P1", "from": "b", "ptr": "x", "at": "T"}
    t.put(stream_fold.state_path("r", "me"), json.dumps(st))
    t.put(CK, json.dumps({"v": 1, "cursor": "T", "open": {s: {} for s in new_open},
                          "unread_events": 0, "unreadable_pointers": []}))
    return t


def _log(t):
    return [v for k, v in t.store.items() if "/bus-v4/comparator/me.log" in k][0]


def test_agreement_logs_AGREE_and_exits_0():
    t = _t(["a", "b"], ["a", "b"])
    assert cli.main(["obligations", "compare-to-fold", "r", "--agent", "me"], transport=t) == 0
    assert "AGREE n=2" in _log(t)


def test_divergence_logs_both_sides_and_exits_1():
    t = _t(["a", "b"], ["a", "c"])
    assert cli.main(["obligations", "compare-to-fold", "r", "--agent", "me"], transport=t) == 1
    line = _log(t)
    assert "DIVERGE" in line and "only_old=['b']" in line and "only_new=['c']" in line


def test_an_unreadable_new_checkpoint_is_UNKNOWN_exit_3_and_logs_nothing_as_agreement():
    t = _t(["a"], ["a"]); del t.store[CK]
    assert cli.main(["obligations", "compare-to-fold", "r", "--agent", "me"], transport=t) == 3
    assert "AGREE" not in "".join(v for k, v in t.store.items() if "comparator" in k)


def test_the_log_is_appended_not_replaced():
    t = _t(["a"], ["a"])
    cli.main(["obligations", "compare-to-fold", "r", "--agent", "me"], transport=t)
    cli.main(["obligations", "compare-to-fold", "r", "--agent", "me"], transport=t)
    assert _log(t).count("AGREE") == 2
```

- [ ] **Step 2: Run — expect argparse error**

- [ ] **Step 3: Implement**

```python
def cmd_obligations_compare_to_fold(args: argparse.Namespace, transport: Any) -> int:
    """SHADOW (spec §5.1 step 3). Diff the old fold's open set against the new
    checkpoint's, append one line, exit 0 agree / 1 diverge / 3 unknown.
    Divergence is a finding to investigate, never a number to tune."""
    from . import stream_fold
    state, src = stream_fold.load_state(transport, args.team, args.agent, datetime.now(timezone.utc))
    if src == "invalid":
        print("compare-to-fold: UNKNOWN — old fold state invalid", file=sys.stderr); return 3
    raw = transport.read(f"team/{args.team}/member/{args.agent}/fold/checkpoint.json")
    if raw is None:
        print("compare-to-fold: UNKNOWN — new checkpoint absent or unreadable", file=sys.stderr); return 3
    try:
        new_open = set(json.loads(raw).get("open", {}))
    except (json.JSONDecodeError, AttributeError):
        print("compare-to-fold: UNKNOWN — new checkpoint unparsable", file=sys.stderr); return 3
    old_open = set(state.get("open", {}))
    ts = _now()
    if old_open == new_open:
        line, rc = f"{ts} AGREE n={len(old_open)}", 0
    else:
        line, rc = (f"{ts} DIVERGE only_old={sorted(old_open - new_open)} "
                    f"only_new={sorted(new_open - old_open)}"), 1
    log_path = f"team/{args.team}/_coord/bus-v4/comparator/{args.agent}.log"
    prior = transport.read(log_path) or ""
    transport.write(log_path, prior + line + "\n")
    print(line); return rc
```

Register under `obligations`; classify as a write in the activity test.

- [ ] **Step 4: Write the cutover runbook**

```markdown
<!-- docs/coord/COORD-FOLD-CUTOVER.md -->
# coord-fold cutover runbook (spec §5)

Preconditions: `team/<team>/_coord/bus-v4/records.json` exists (a NEW MomentAnnotation
data_type, provisioned like bus-v3 was — never the v3 type); `coord-fold` installed on the
first host (coord-boss's). History stays at the old prefix, read-only, forever.

1. **Seed** (once per agent): `coord-engine obligations export-open <team> --agent <a>`
   — writes the marker `_coord/bus-v4/seeded/<a>.md`; re-running is a no-op.
2. **Dual-emit** is automatic once the v4 config exists (Task 12). Verify with one
   `coord-engine tell` and one `coord-fold fold`: the fold shows the new row.
3. **Shadow**: every tick, run BOTH `coord-engine obligations <team> --agent <a>` (old)
   and `coord-fold fold <team> --agent <a>` (new), then
   `coord-engine obligations compare-to-fold <team> --agent <a>`.
   The comparator log at `_coord/bus-v4/comparator/<a>.log` is the evidence.
4. **Cutover gate**: N consecutive trailing `AGREE` lines (N per §9.1 — proposed 24 hourly
   passes). Count them; do not argue them:
   `fulcra-api file download team/<team>/_coord/bus-v4/comparator/<a>.log - | tail -n 24 | grep -c AGREE`
   A single DIVERGE inside the window resets it and is investigated before anything else.
5. **Cut over** (coord-boss first, alone, one full day; then one agent at a time): the
   agent's wake stops calling the old `obligations`/`needs-me` and calls `coord-fold fold`.
6. **Freeze**: when every agent is moved, the bus-v3 config document gains
   `"frozen": true`; the old engine refuses writes when it sees it. Nothing is deleted.
```

- [ ] **Step 5: Run — expect 4 passed; full suite green except the two known env failures**

- [ ] **Step 6: MUTATION-VERIFY** — make the `raw is None` branch return 0; expect the UNKNOWN test FAIL; restore.

- [ ] **Step 7: Commit** — `coord-engine: obligations compare-to-fold + cutover runbook (spec §5.1 steps 3–5)`

---

### Task 14: AGENTS.md + shape-of-what-shipped acceptance

**Files:**
- Modify: `AGENTS.md` (new section: coord-fold, the five verbs, the three gates, the two unknowns, and the dependency-direction rule old→new never)
- Create: `packages/coord-fold/tests/test_shape_of_what_shipped.py`

The directive: *"Acceptance covers THE SHAPE OF WHAT SHIPPED, not only behaviour: a plan whose modules dissolve into one file has not been implemented however green its tests are."* Make that a test.

- [ ] **Step 1: Write the test**

```python
# packages/coord-fold/tests/test_shape_of_what_shipped.py
"""The 2026-08-14 plan named queue.py/routing.py/cursor.py/output.py and shipped
none of them. This asserts the module layout in the plan is the layout on disk."""
import pathlib
import coord_fold

PLANNED = {"__init__.py", "events.py", "transport.py", "channel.py",
           "checkpoint.py", "fold.py", "cli.py"}


def test_the_planned_modules_exist_and_no_others_do():
    on_disk = {p.name for p in pathlib.Path(coord_fold.__file__).parent.glob("*.py")}
    assert on_disk == PLANNED, (f"missing={PLANNED - on_disk} unplanned={on_disk - PLANNED} — "
                                f"an unplanned module needs a plan amendment, not a quiet add")
```

- [ ] **Step 2: Run — expect pass**

- [ ] **Step 3: AGENTS.md** — add under `## Setup & tests` a bullet block: the package exists; its three CI gates; the five verbs and their exit codes (0/2/3); the two unknowns and that `degraded` is banned by test; **dependency direction: `coord-engine` may read bus-v4 documents by path and may write to it via `dual_emit`, but `coord_fold` never imports `coord_engine` and the structural test enforces it**; the cutover runbook path.

- [ ] **Step 4: MUTATION-VERIFY** — `touch coord_fold/extra.py`; expect FAIL naming `unplanned={'extra.py'}`; `rm` it.

- [ ] **Step 5: Commit** — `coord-fold: shape-of-what-shipped test + AGENTS.md (ship-gate)`

---

## Open questions for codex-reviewer (spec §9, plus two this plan surfaced)

Push on these specifically. Each has a recommendation so the review is of a position, not a blank.

1. **Cutover N.** Proposed **24 consecutive hourly AGREE lines** (one full day). Rationale: matches §5.3's "full day of ticks" and is long enough to cover one daily role sweep and one overnight quiet period. Counter-consideration: at a 30-minute tick that is 12 hours, not 24 — the runbook counts *lines*, so the operator must fix the tick rate before counting. Reviewer: is a count the right gate, or should it be a wall-clock window with zero DIVERGE?
2. **Channel granularity.** Proposed **one channel per team**. The fold filters by `to` in memory; the cost is the whole team's event volume per read, which is bounded by events-since-cursor, not corpus. Per-agent channels would need N configs and make broadcast a fan-out write. Reviewer: at what team event rate does per-team stop being cheap?
3. **Event retention / compaction.** Not implemented in this plan. A fold away for a month reads a month. Proposed follow-up (not in scope): `fold --compact` writes a snapshot event `{"kind":"note","slug":"_snapshot",...}` carrying the open map, and a fresh fold may start from the latest snapshot instead of 1970. Reviewer: should compaction be in v0.1, or is "one month of events" acceptable for the first cutover?
4. **Are the five verbs the right five?** `release` is an event kind but not a verb — a holder gives something back with `close --evidence`? No: `close` asserts done; `release` asserts *not mine*. Proposed: **add `release` as a sixth verb** or fold it into `claim --release`. Reviewer: this is exactly the kind of thing you should push on.
5. **(surfaced by Task 5) The `_seen` idempotency list.** G4 says the checkpoint has exactly five fields; the plan persists a sixth, underscore-prefixed, capped at 500 record ids, so overlap re-delivery cannot double-apply. Alternative: zero overlap and trust `recorded_at` strict ordering. Reviewer: which is safer against the platform's actual delivery semantics?
6. **(surfaced by Task 6) `max_events` and the cursor.** When the cap is hit the cursor stops at the last applied event and `unread_events` counts the rest — the next fold resumes. Reviewer: is 5000 a sane default, and should an unfinished fold exit 3 (this plan) or 0-with-a-note?

---

## What this plan does not do (mirrors spec §10)

- Does not fix the pre-fence publication overwrite. Orthogonal: a stream fold does not read the aggregate.
- Does not migrate the anti-slop findings.
- Does not delete anything. Bus-v3 is frozen, not removed.
- Does not earn back any of the 37 killed verbs. Each returns only by directive.

## Self-review (writing-plans checklist)

1. **Spec coverage.** §3.1 → Task 3. §3.2 (files addressed only by ptr) → G2 + Tasks 6/8 never list. §3.3 → Tasks 5/6. §3.4 → Tasks 1/4. §4 → Tasks 6/9/10. §5.1 steps 1–5 → Tasks 11/12/13 + runbook. §5.2 → runbook step 6. §5.3 → runbook step 5. §6 → verb table. §7 (inbox reconciler) → **not in this plan**; it is a post-cutover reconciler and is named as follow-up work, not silently dropped. §8 → G9/G10 in every task, G11 Task 10. §9 → open questions. §1a.1–3 → Tasks 1/2/14.
2. **Placeholder scan.** One deliberate non-placeholder that reads like one: Task 4 Step 4's stdin key set says "EDIT THIS SET" — it is a golden comparison against a named file and line, with the instruction to copy rather than guess. It is a checkable step, not a TODO.
3. **Type consistency.** `read_classified` returns `(str|None, "ok"|"absent"|"error")` in Tasks 1/4/5/8 alike. `write_event(cfg, payload, *, sender)` in Tasks 4/7/8. `cp.path/empty/load/save/apply` names match across 5/6/8/9/11/13. Exit codes 0/2/3 are the same three in every verb.
