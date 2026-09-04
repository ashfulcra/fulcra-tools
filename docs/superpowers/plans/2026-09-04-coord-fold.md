# coord-fold: Coord on Annotations Implementation Plan (r4)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Spec:** `docs/superpowers/specs/2026-09-04-coord-annotation-bus-design.md` (branch `claude/coord-boss-handoff-resume-60sjua` @ `3a4687b0`). Read it whole first; §1 and §1a are why every structural requirement below exists.

**Directive:** coord-boss `65761fbd` (P0). **Reviewers (both required):** codex-reviewer, codex-coder. **Implementer:** coord-maintainer.

**r4 is a coherent rewrite, not a fourth splice.** codex-coder's round-3 verdict: *"integrate the amendment rather than layering contradictory snippets."* r1–r3 were layered; r4 folds every accepted change into the task bodies so that one manifest, one set of signatures, one fake, and one transport model appear everywhere. The revision log at the end maps r3 task numbers to r4.

**Goal:** A separate `coord-fold` package whose fold engine *cannot* enumerate — the no-enumeration rule becomes a property of the type system, the import graph, the call graph, and a red CI check, not a reviewer's attention — running in parallel with the old bus until a comparator proves agreement, then cut over.

**Architecture:** Three planes (spec §3). Signal = one MomentAnnotation per event on a team channel. Content = unchanged OKF files addressed only by `ptr`. Fold = one checkpoint per agent, advanced by reading events forward from a cursor at O(new events). The fold is handed a **reader** whose public surface is exactly two methods and whose private surface is three sealed subcommand calls; writes live on a separate, unrelated class. The only changes to `coord-engine` are the seed export, the dual-emit mirror, and the comparator — all on the *old* side, which is allowed to enumerate.

**Tech Stack:** Python ≥3.11, stdlib only inside `coord_fold` (argparse, json, subprocess, tempfile, datetime, typing). Build: hatchling. Test: pytest<8. Dependency: `fulcra-common` only (for `find_fulcra_cli`). The Fulcra API is reached by shelling out to the `fulcra-api` CLI exactly as the old transport does (`file stat`, `file download`, `get-records`, `record`, `file upload`) — five fixed argv shapes, nothing else.

---

## Global Constraints

Every task's requirements include these. G-numbers are stable across revisions so verdicts can cite them.

- **G1.** `kind` is a closed set: `open`, `close`, `claim`, `release`, `note`. (spec §3.1)
- **G2.** `ptr` is **one file path** — never a directory, never a glob, **never absent on `open` or `close`**. (spec §3.1)
- **G3.** Event payload v1 fields, exactly: `v`, `at`, `from`, `to`, `kind`, `slug`, `pri`, `ptr`. (spec §3.1)
- **G4.** Checkpoint v1 fields, exactly: `v`, `cursor`, `open`, `unread_events`, `unreadable_pointers`, **plus `seen`** (see Ruling 1). Until Ruling 1 lands, `seen` is the sixth field and this constraint says so rather than hiding it under an underscore. (spec §3.3, amended)
- **G5.** `PointerTransport` (the reader Protocol) exposes **exactly two methods**: `read_classified(path) -> (str|None, "ok"|"absent"|"error")` and `read_events(channel, since) -> Iterator[dict]`. (spec §3.4)
- **G6.** The fold package is a **separate uv workspace package** (`packages/coord-fold`); its `pyproject.toml` must not depend on `coord-engine`. (directive §1)
- **G7.** A structural test asserts no enumeration method exists on the reader or the writer **and** the package's import graph never reaches `coord_engine`. (directive §2)
- **G8.** File-size ceiling as a CI gate: **400 lines per `.py` under `coord_fold/`**, recursive. (directive §3)
- **G9.** Every fold test drives `coord_fold.cli.main([...])` and asserts on the **stored checkpoint** in the fake store, not on a decision function. (directive §4)
- **G10.** Every test file is **mutation-verified** by showing it fails when the behaviour it names is removed. (directive §4)
- **G11.** No output path in `coord_fold` may emit the string `degraded`. Two bounded unknowns replace it: `unread_events: N` and `unreadable_pointers: [slug]`. An unknown never reads as clear. (spec §4)
- **G12.** Six verbs: `emit`, `fold`, `claim`, `release`, `close`, `status`. `release` asserts *not mine*; `close` asserts *done, with evidence*. Every other verb is killed and must be asked for by an agent that needs it. (spec §6, amended r3 per both verdicts and open question 4)
- **G13.** Migration is **parallel bus proven then cut over**: seed the open obligations only, dual-emit from the old engine, shadow-compare, cut over after N agreeing passes spanning ≥24h with observed transitions, freeze the old prefix read-only. (spec §5, Ash decision — do not reopen)
- **G14.** Rollout: coord-boss alone until a full day of ticks is clean, then one agent at a time. (spec §5.3)
- **G15.** Never hardcode the channel: `data_type` is resolved from `team/<team>/_coord/bus-v4/records.json` via `read_classified`.
- **G16.** No secrets in any doc, note, or fixture.
- **G17.** Commits are authored as `114089064+ashfulcra@users.noreply.github.com` — the repo is PUBLIC.
- **G18.** *(r2, codex-coder)* **Ownership.** Every public symbol in the manifest is **defined** in one named module, nowhere else, with the required definition kind (a callable must be a `def`/`class`, never a stub assignment). No planned module may be a shim. The package tree is scanned **recursively** and may contain only the planned modules. No module may load or generate code at runtime.
- **G19.** *(r3, both verdicts)* **Allowed import DAG.** Only these intra-package edges exist: `cli → {fold, channel, events, checkpoint, transport}`; `fold → {channel, checkpoint, events, transport}`; `channel → {transport}`; `checkpoint → {transport}`; `events`, `transport`, `__init__` import nothing from the package. **No owner module may import `cli`.**
- **G20.** *(r3, amended r4 per codex-coder round 3)* **`cli.py` is wiring only, recursively.** `cli.py` defines exactly the names in `CLI_EXACT_DEFINITIONS` at top level, contains **zero nested `def`/`class`/`lambda` anywhere**, and every top-level function respects a per-function budget (≤ 30 statements, ≤ 220 AST nodes). Delegation is closed **semantically**: the package may not reference `sys.modules`, `globals`, `vars`, `locals`, `getattr`, `setattr`, `eval`, `exec`, `compile`, `__import__`, `importlib`, `marshal`, `runpy` or `types.ModuleType`; and every call inside an owner module's manifest callable must resolve to a same-module definition, an allowed-DAG import of that module, an allowed stdlib import, or a small builtin allowlist. A syntactic wrapper detector is retained as a cheap early signal but is not the guarantee.
- **G21.** *(r3)* **Mass ceilings, recursive:** per module ≤ 400 lines, ≤ 16 KB, ≤ 1500 AST nodes; `cli.py` ≤ 900 AST nodes.
- **G22.** *(r3, amended r4 per codex-coder round 3)* **The Protocol is a capability boundary.** `CliPointerReader` and `CliPointerWriter` are **independent classes** (`__mro__ == (cls, object)`), share no base, and a reader object possesses no write primitive at any name. **There is no generic argv receiver anywhere in the package**: each sealed method calls `subprocess.run` with a literal argv whose string constants are one of exactly five fixed prefixes; no function in the package takes `*args`; `subprocess.Popen`, `os.system`, `os.exec*`, `os.spawn*` do not appear. The string `"list"` does not appear in `transport.py`.

---

## File Structure

```
packages/coord-fold/
  pyproject.toml                      deps = [fulcra-common]; NO coord-engine; wheel ships coord_fold only
  README.md                           six verbs, the two unknowns, the gates
  coord_fold/
    __init__.py                       __version__ only
    events.py                         KINDS, PRIORITIES, PAYLOAD_VERSION, build_payload, parse_event   (G1–G3)
    transport.py                      PointerTransport, ReadState, TransportUnavailable,
                                      CliPointerReader, CliPointerWriter                             (G5, G22)
    channel.py                        CONFIG_PATH, ChannelUnresolved, config_path, resolve            (G15)
    checkpoint.py                     SCHEMA_VERSION, path, empty, apply, load, save                  (G4)
    fold.py                           OVERLAP_SECONDS, FoldOutcome, FoldRefused, run                  (§3.3)
    cli.py                            main, build_parser, RC_*, cmd_* (six), six private helpers      (G12, G20)
  tests/
    fakes.py                          FakeStore, FakeReader, FakeWriter — reader has NO write method
    test_structural.py                G5, G7, G22: enumeration, import graph, reader/writer boundary, sealed argv
    test_ownership.py                 G18–G21: manifest w/ kinds, recursive cli policy, DAG, semantic delegation, mass
    test_file_size_ceiling.py         G8
    test_no_degraded_vocabulary.py    G11
    test_events.py, test_checkpoint.py, test_channel.py
    test_cli_fold.py, test_cli_emit.py, test_cli_claim_release_close.py, test_cli_status.py

packages/coord-engine/coord_engine/
    dual_emit.py                      NEW: mirror old-bus transitions onto bus-v4       (§5.1 step 2)
    cli.py                            MODIFY: obligations export-open / compare-to-fold / cutover-ready
    records.py                        MODIFY: one call into dual_emit from emit_event
packages/coord-engine/tests/
    test_dual_emit.py, test_obligations_export_open.py, test_obligations_compare_to_fold.py, test_obligations_cutover_ready.py

.github/workflows/uv-workspace.yml    MODIFY: named step "coord-fold structural gates"
docs/coord/COORD-FOLD-CUTOVER.md      runbook
```

---

## Verb Disposition (spec §6)

42 top-level nouns registered at `5db5c3e5` (the spec counts 38 at `3a4687b0`; the four extra on my tree are `annotate`, `stash`, `wake`, `acceptance` — reported, not reconciled).

| Verb(s) | Disposition | Reason |
|---|---|---|
| `tell` | → `emit` | An `open` event with `to`, `pri`, `ptr`. |
| `respond` | → `close` | A `close` event with evidence `ptr`. |
| `owed`, `obligations`, `needs-me`, `inbox` | → `fold` + `status` | All four are "what do I owe"; one checkpoint answers it. |
| `roles claim/release` | → `claim` / `release` | Same fact, no lease directory to enumerate. |
| `status` (engine health) | → `status` | Reports the checkpoint and its two unknowns. |
| `queue` | kill | `fold` *is* the read. |
| `reconcile` | kill | Rebuilds the aggregate by enumeration — the defect. |
| `board`, `search`, `agents`, `presence`, `engagement`, `threads`, `briefing`, `digest`, `dash`, `health`, `doctor` | kill | Corpus walks under a deadline. Earn back one at a time with a stated cursor. |
| `broadcast`, `remind`, `later`, `intent` | kill | `emit --to all`; a timer is a future-dated `emit`. |
| `review` (8), `forge` | kill | A review request is an `open` with a `ptr` to the review doc; the forge mirror is a §7-shaped reconciler off every fold path. |
| `task` (9) | kill | Task state lives in the OKF doc; routing-relevant transitions are `open`/`close`. |
| `router`, `route`, `atc`, `headroom`, `usage` | kill | Dispatch policy over an enumerated board. |
| `escalate` | kill | Attendance scan over 592 dirs; becomes a reconciler if wanted. |
| `bus-v3`, `wake`, `stash`, `continuity`, `annotate`, `acceptance`, `asks`, `answer` | kill | Old-bus plumbing or content-plane conveniences. |

**Kept: 6 (five from the spec plus `release`). Killed: 36.**

---

### Task 1: Package scaffold, fakes, and the structural boundary test (G5, G6, G7, G22)

**Files:**
- Create: `packages/coord-fold/pyproject.toml`, `README.md`
- Create: `coord_fold/__init__.py`, `coord_fold/transport.py`
- Create: `tests/fakes.py`, `tests/test_structural.py`

**Interfaces (produced):**
- `transport.PointerTransport` (Protocol): `read_classified(path) -> tuple[str|None, ReadState]`, `read_events(channel, since) -> Iterator[dict]`.
- `transport.CliPointerReader(cli: list[str], timeout=60.0)` — implements the Protocol; private: `_stat`, `_download`, `_records`.
- `transport.CliPointerWriter(cli: list[str], timeout=60.0)` — `write_event(cfg, payload, *, sender) -> bool`, `save_doc(path, text) -> bool`; private: `_record`, `_upload`.
- `transport.TransportUnavailable(RuntimeError)`, `transport.ReadState`.
- `fakes.FakeStore(docs, events)`; `fakes.FakeReader(store)`; `fakes.FakeWriter(store)`.

- [ ] **Step 1: Write the failing structural test**

```python
# packages/coord-fold/tests/test_structural.py
"""G5, G7, G22. The boundary as red checks.

Spec §1a: the 2026-08-21 rule failed because it was a policy against a codebase where the
enumerator was already imported and holding a live transport. Round-3 (codex-coder): a
shared base class and a name-mangled generic runner were not a boundary either — the
reader inherited write primitives and `reader._Cli__exec("file","list")` was callable.
So: two unrelated classes, five literal argv shapes, no varargs anywhere.
"""
from __future__ import annotations

import ast
import pathlib

import coord_fold
from coord_fold import transport as tr

PKG_DIR = pathlib.Path(coord_fold.__file__).parent
ENUM_NAMES = ("list_dir", "glob", "listdir", "scandir", "walk", "rglob", "iterdir")
FORBIDDEN_IMPORT_ROOTS = ("coord_engine",)
WRITE_NAMES = {"write_event", "save_doc", "_record", "_upload"}
READ_NAMES = {"read_classified", "read_events", "_stat", "_download", "_records"}
ALLOWED_ARGV = {("file", "stat"), ("file", "download"), ("get-records",), ("record",), ("file", "upload")}
FORBIDDEN_LAUNCHERS = {("subprocess", "Popen"), ("subprocess", "call"), ("subprocess", "check_output"),
                       ("os", "system"), ("os", "popen")}


def _modules():
    return sorted(p for p in PKG_DIR.rglob("*.py") if "__pycache__" not in p.parts)


def _tree(p):
    return ast.parse(p.read_text(), filename=str(p))


def test_no_enumeration_method_on_reader_writer_or_fakes():
    from fakes import FakeReader, FakeStore, FakeWriter
    st = FakeStore({}, [])
    for obj in (tr.CliPointerReader(cli=["true"]), tr.CliPointerWriter(cli=["true"]),
                FakeReader(st), FakeWriter(st)):
        for n in ENUM_NAMES:
            assert not hasattr(obj, n), f"{type(obj).__name__} has {n}"


def test_no_module_mentions_an_enumeration_token():
    for p in _modules():
        src = p.read_text()
        for n in ENUM_NAMES:
            assert f".{n}(" not in src and f"def {n}(" not in src, f"{p.name} mentions {n}"


def test_import_graph_never_reaches_coord_engine():
    for p in _modules():
        for node in ast.walk(_tree(p)):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for n in names:
                assert n.split(".")[0] not in FORBIDDEN_IMPORT_ROOTS, f"{p.name} imports {n}"


def test_pyproject_does_not_depend_on_coord_engine():
    import tomllib
    data = tomllib.loads((PKG_DIR.parent / "pyproject.toml").read_text())
    assert not any(d.startswith("coord-engine") for d in data["project"].get("dependencies", []))


def test_the_protocol_has_exactly_two_methods():
    members = {n for n in dir(tr.PointerTransport) if not n.startswith("_")}
    assert members == {"read_classified", "read_events"}, members


def test_reader_and_writer_are_unrelated_classes():
    """G22: no shared base, so nothing to inherit across the boundary."""
    assert tr.CliPointerReader.__mro__ == (tr.CliPointerReader, object)
    assert tr.CliPointerWriter.__mro__ == (tr.CliPointerWriter, object)


def test_reader_has_no_write_primitive_and_writer_has_no_read_primitive():
    for n in WRITE_NAMES:
        assert not hasattr(tr.CliPointerReader, n), f"reader has {n}"
    for n in READ_NAMES:
        assert not hasattr(tr.CliPointerWriter, n), f"writer has {n}"
    pub_r = {n for n in vars(tr.CliPointerReader) if not n.startswith("_")}
    pub_w = {n for n in vars(tr.CliPointerWriter) if not n.startswith("_")}
    assert pub_r == {"read_classified", "read_events"}, pub_r
    assert pub_w == {"write_event", "save_doc"}, pub_w
    priv_r = {n for n in vars(tr.CliPointerReader) if n.startswith("_") and not n.startswith("__")}
    priv_w = {n for n in vars(tr.CliPointerWriter) if n.startswith("_") and not n.startswith("__")}
    assert priv_r == {"_stat", "_download", "_records"}, priv_r
    assert priv_w == {"_record", "_upload"}, priv_w


def test_no_function_in_the_package_takes_varargs():
    for p in _modules():
        for node in ast.walk(_tree(p)):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                assert node.args.vararg is None, f"{p.name}:{getattr(node, 'name', 'lambda')} takes *args"


def test_every_subprocess_call_has_a_literal_argv_from_the_fixed_set():
    """The sealing. subprocess.run appears only in transport.py; each call's first arg is a
    List whose string constants are exactly one allowed prefix; nothing else launches."""
    for p in _modules():
        for node in ast.walk(_tree(p)):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name):
                key = (f.value.id, f.attr)
                assert key not in FORBIDDEN_LAUNCHERS, f"{p.name} uses {key}"
                if key == ("subprocess", "run"):
                    assert p.name == "transport.py", f"{p.name} launches a subprocess"
                    argv = node.args[0]
                    assert isinstance(argv, ast.List), f"{p.name}: subprocess.run argv is not a literal list"
                    consts = tuple(e.value for e in argv.elts if isinstance(e, ast.Constant) and isinstance(e.value, str))
                    assert consts in ALLOWED_ARGV, f"{p.name}: argv constants {consts} not in the fixed set"


def test_the_token_list_does_not_appear_in_transport():
    src = (PKG_DIR / "transport.py").read_text()
    assert '"list"' not in src and "'list'" not in src
```

- [ ] **Step 2: Run — expect ImportError (no package yet).** Confirm the test file parses: `python -c "import ast;ast.parse(open('tests/test_structural.py').read())"`.

- [ ] **Step 3: Scaffold**

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
"""The enforcing interface (spec §3.4) as a capability boundary (G22).

Two unrelated classes. A fold is handed a CliPointerReader; it has two public methods
and three sealed private ones, each of which builds its own literal argv. There is no
method that accepts free argv, no shared base, and no write primitive on the reader.
"""
from __future__ import annotations

import json
import os
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
            p = subprocess.run([*self._cli, "file", "stat", path], capture_output=True,
                               text=True, timeout=self._timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return 127, "", str(exc)
        return p.returncode, p.stdout, p.stderr

    def _download(self, path: str) -> tuple[int, str, str]:
        try:
            p = subprocess.run([*self._cli, "file", "download", path, "/dev/stdout"],
                               capture_output=True, text=True, timeout=self._timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return 127, "", str(exc)
        return p.returncode, p.stdout, p.stderr

    def _records(self, channel: str, since: str) -> tuple[int, str, str]:
        try:
            p = subprocess.run([*self._cli, "get-records", channel, since, _FAR_FUTURE],
                               capture_output=True, text=True, timeout=self._timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return 127, "", str(exc)
        return p.returncode, p.stdout, p.stderr

    def read_classified(self, path: str) -> tuple[str | None, ReadState]:
        # Measured 2026-09-02: `file stat` on a missing path -> rc!=0 + "File not found".
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
            p = subprocess.run([*self._cli, "record"], input=doc, capture_output=True,
                               text=True, timeout=self._timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return 127, "", str(exc)
        return p.returncode, p.stdout, p.stderr

    def _upload(self, local: str, remote: str) -> tuple[int, str, str]:
        try:
            p = subprocess.run([*self._cli, "file", "upload", local, remote],
                               capture_output=True, text=True, timeout=self._timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return 127, "", str(exc)
        return p.returncode, p.stdout, p.stderr

    def write_event(self, channel_cfg: dict[str, str], payload: dict, *, sender: str) -> bool:
        # Key names are a GOLDEN COMPARISON against coord_engine/transport.py record_write
        # (line ~385 at 5db5c3e5): read them, do not guess. Task 5 Step 4 asserts them.
        doc = {"data_type": channel_cfg["data_type"], "api_version": channel_cfg["api_version"],
               "note": json.dumps(payload, separators=(",", ":")), "source": sender,
               "recorded_at": payload["at"]}
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
            os.unlink(tmp)
```

```python
# packages/coord-fold/tests/fakes.py
"""One store, two views. FakeReader has read_classified/read_events and NOTHING else;
FakeWriter has write_event/save_doc and nothing else. Tests build both from one store."""
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
        self._s.events.append({"id": f"w{len(self._s.written)}", "recorded_at": payload["at"],
                               "note": json.dumps(payload)})
        return True

    def save_doc(self, path: str, text: str) -> bool:
        self._s.saved[path] = text
        return True
```

README:

```markdown
# coord-fold
The fold engine that cannot enumerate. Six verbs: `emit`, `fold`, `claim`, `release`, `close`, `status`.
Structural gates (CI step "coord-fold structural gates"): no enumeration method or token; import graph
never reaches `coord_engine`; reader and writer are unrelated classes and a reader has no write primitive;
every subprocess call is one of five literal argv shapes; ownership manifest with definition kinds;
`cli.py` is wiring only, recursively; allowed import DAG; semantic delegation ban; every module under
**400 lines**, 16 KB and 1500 AST nodes; the string `degraded` never appears.
Two bounded unknowns: `unread_events: N` and `unreadable_pointers: [slug]`. An unknown never reads as clear.
```

- [ ] **Step 4: Run — expect all 11 structural tests to pass.**

- [ ] **Step 5: MUTATION-VERIFY** (each restored before the next)

```bash
# (a) a shared base class
python - <<'PY'
p="coord_fold/transport.py"; s=open(p).read()
s=s.replace("class CliPointerReader:", "class _Base:\n    pass\n\nclass CliPointerReader(_Base):",1)
open(p,"w").write(s)
PY
python -m pytest tests/test_structural.py -q    # expect FAIL: unrelated-classes
git checkout coord_fold/transport.py
# (b) a generic runner
python - <<'PY'
p="coord_fold/transport.py"; s=open(p).read()
s=s.replace("    def _stat(self, path: str)", "    def _run(self, *argv):\n        return subprocess.run([*self._cli, *argv])\n\n    def _stat(self, path: str)",1)
open(p,"w").write(s)
PY
python -m pytest tests/test_structural.py -q    # expect >=2 FAIL: varargs + argv-not-literal/priv-set
git checkout coord_fold/transport.py
# (c) an enumerating argv
python - <<'PY'
p="coord_fold/transport.py"; s=open(p).read()
s=s.replace('"file", "stat", path', '"file", "list", path',1)
open(p,"w").write(s)
PY
python -m pytest tests/test_structural.py -q    # expect 2 FAIL: fixed-set + token
git checkout coord_fold/transport.py
# (d) a write primitive on the reader
python - <<'PY'
p="coord_fold/transport.py"; s=open(p).read()
s=s.replace("    def read_classified(self, path: str)", "    def _upload(self, a, b):\n        return 0, '', ''\n\n    def read_classified(self, path: str)",1)
open(p,"w").write(s)
PY
python -m pytest tests/test_structural.py -q    # expect FAIL: reader has _upload
git checkout coord_fold/transport.py
```

- [ ] **Step 6: Commit** — `coord-fold: scaffold, reader/writer boundary, sealed argv, structural tests (G5–G7, G22)`

---

### Task 2: File-size ceiling as a CI gate (G8)

- [ ] **Step 1: Write the test**

```python
# packages/coord-fold/tests/test_file_size_ceiling.py
import pathlib
import coord_fold
CEILING = 400
PKG_DIR = pathlib.Path(coord_fold.__file__).parent

def test_every_module_is_under_the_ceiling_recursively():
    over = {p.name: sum(1 for _ in p.open()) for p in PKG_DIR.rglob("*.py")
            if "__pycache__" not in p.parts and sum(1 for _ in p.open()) > CEILING}
    assert not over, over

def test_the_ceiling_is_the_documented_number():
    assert f"{CEILING} lines" in (PKG_DIR.parent / "README.md").read_text()
```

- [ ] **Step 2: CI step** — in `.github/workflows/uv-workspace.yml` after the pytest step:

```yaml
      - name: coord-fold structural gates
        run: |
          uv run --package coord-fold --extra dev python -m pytest \
            packages/coord-fold/tests/test_structural.py \
            packages/coord-fold/tests/test_ownership.py \
            packages/coord-fold/tests/test_file_size_ceiling.py \
            packages/coord-fold/tests/test_no_degraded_vocabulary.py -q
```

- [ ] **Step 3: Run — 2 passed. Mutation:** append 401 comment lines to `__init__.py` → FAIL; restore. **Commit** — `coord-fold: 400-line ceiling as a CI gate (G8)`

---

### Task 3: Ownership, recursive `cli.py` policy, import DAG, semantic delegation ban, mass (G18–G21)

**Why — attributed.** r2 (codex-coder): six shims plus one ≤399-line `cli.py` pass coupling, interface, length and filename gates. r3 (both): a `("cmd_", "_")` prefix lets the implementation sit in `cli.py` behind one-line forwarding wrappers. r3→r4 (codex-coder round 3): a top-level-only definition scan admits **nested** definitions inside an allowed `cli.py` function, and a one-statement wrapper detector admits **two-statement `sys.modules` delegation** with no owner import of `cli`. So ownership is recursive, `cli.py` may nest nothing, delegation is banned by **name** (`sys.modules`, `getattr`, `globals`…) and by **call graph** (every call in an owner's manifest callable resolves to something the DAG allows), and mass is capped per module *and* per function.

**Files:** create `tests/test_ownership.py`. It cannot fail-first meaningfully before Tasks 4–10 create the modules; its proof of discrimination is Step 4's mutations, run after Task 10.

- [ ] **Step 1: Write the test**

```python
# packages/coord-fold/tests/test_ownership.py
"""G18–G21. Ownership with kinds, recursive cli.py policy, DAG, semantic delegation ban, mass.

Every check here exists because a reviewer produced a counterexample that passed the
previous version. See the plan's revision log; the mutations in Task 3 Step 4 ARE those
counterexamples.
"""
from __future__ import annotations

import ast
import pathlib
import tomllib

import coord_fold

PKG_DIR = pathlib.Path(coord_fold.__file__).parent

OWNERSHIP: dict[str, dict[str, str]] = {
    "events.py": {"PAYLOAD_VERSION": "value", "KINDS": "value", "PRIORITIES": "value",
                  "build_payload": "callable", "parse_event": "callable"},
    "transport.py": {"ReadState": "value", "PointerTransport": "callable", "TransportUnavailable": "callable",
                     "CliPointerReader": "callable", "CliPointerWriter": "callable"},
    "channel.py": {"CONFIG_PATH": "value", "ChannelUnresolved": "callable", "config_path": "callable", "resolve": "callable"},
    "checkpoint.py": {"SCHEMA_VERSION": "value", "path": "callable", "empty": "callable",
                      "apply": "callable", "load": "callable", "save": "callable"},
    "fold.py": {"OVERLAP_SECONDS": "value", "FoldOutcome": "callable", "FoldRefused": "callable", "run": "callable"},
    "cli.py": {"main": "callable", "build_parser": "callable"},
    "__init__.py": {"__version__": "value"},
}
CLI_EXACT_DEFINITIONS = {
    "main", "build_parser", "RC_OK", "RC_REFUSED", "RC_UNKNOWN",
    "cmd_emit", "cmd_fold", "cmd_claim", "cmd_release", "cmd_close", "cmd_status",
    "_now", "_default_transports", "_render_open", "_report_unknowns", "_emit_kind", "_owed_row",
}
CLI_MAX_STATEMENTS_PER_FUNCTION = 30
CLI_MAX_NODES_PER_FUNCTION = 220
ALLOWED_EDGES: dict[str, set[str]] = {
    "cli.py": {"fold", "channel", "events", "checkpoint", "transport"},
    "fold.py": {"channel", "checkpoint", "events", "transport"},
    "channel.py": {"transport"}, "checkpoint.py": {"transport"},
    "events.py": set(), "transport.py": set(), "__init__.py": set(),
}
OWNER_MODULES = {"events.py", "transport.py", "channel.py", "checkpoint.py", "fold.py"}
STDLIB_ALLOWED = {"__future__", "argparse", "datetime", "json", "os", "subprocess", "sys", "tempfile", "typing"}
THIRD_PARTY_ALLOWED = {"fulcra_common"}
BANNED_NAMES = {"getattr", "setattr", "delattr", "globals", "vars", "locals", "eval", "exec", "compile", "__import__"}
BANNED_ATTRS = {("sys", "modules"), ("importlib", "import_module"), ("importlib", "reload"),
                ("marshal", "loads"), ("runpy", "run_path"), ("runpy", "run_module"), ("types", "ModuleType")}
BUILTIN_CALLS_ALLOWED = {"len", "sorted", "dict", "list", "set", "tuple", "str", "int", "float", "bool",
                         "isinstance", "min", "max", "any", "all", "range", "enumerate", "print", "repr",
                         "ValueError", "RuntimeError", "KeyError", "TypeError", "NamedTuple", "open", "iter", "next"}
MAX_BYTES, MAX_NODES, MAX_NODES_CLI = 16 * 1024, 1500, 900


def _tree(name: str) -> ast.Module:
    return ast.parse((PKG_DIR / name).read_text(), filename=name)


def _top_defs(tree: ast.Module) -> dict[str, str]:
    out: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out[node.name] = "callable"
        elif isinstance(node, ast.Assign):
            out.update({t.id: "value" for t in node.targets if isinstance(t, ast.Name)})
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            out[node.target.id] = "value"
    return out


def _package_imports(tree: ast.Module) -> set[str]:
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 1:
            out.update([node.module.split(".")[0]] if node.module else [a.name for a in node.names])
    return out


def _absolute_imports(tree: ast.Module) -> set[str]:
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            out.add(node.module.split(".")[0])
    return out


def _import_aliases(tree: ast.Module) -> set[str]:
    """Local names bound by ANY import in the module (e.g. `cp` in `from . import checkpoint as cp`)."""
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            out.update((a.asname or a.name).split(".")[0] for a in node.names)
    return out


# --- G18 ownership ------------------------------------------------------------------

def test_every_manifest_symbol_is_defined_in_its_module_with_the_right_kind():
    for mod, symbols in OWNERSHIP.items():
        defs = _top_defs(_tree(mod))
        for name, kind in symbols.items():
            assert name in defs, f"{mod} does not define {name!r}"
            if kind == "callable":
                assert defs[name] == "callable", f"{mod}: {name!r} is a bare assignment, not a def/class"


def test_no_manifest_symbol_is_defined_in_a_second_module():
    for mod, symbols in OWNERSHIP.items():
        for other in OWNERSHIP:
            if other != mod:
                dup = set(symbols) & set(_top_defs(_tree(other)))
                assert not dup, f"{sorted(dup)} owned by {mod} also defined in {other}"


def test_no_planned_module_is_a_shim():
    for mod in OWNERSHIP:
        tree = _tree(mod)
        assert set(_top_defs(tree)) - _import_aliases(tree), f"{mod} defines nothing of its own"


def test_package_tree_recursively_equals_the_manifest():
    found = sorted(p.relative_to(PKG_DIR).as_posix() for p in PKG_DIR.rglob("*")
                   if p.is_file() and "__pycache__" not in p.parts)
    assert found == sorted(OWNERSHIP), {"unplanned": sorted(set(found) - set(OWNERSHIP)), "missing": sorted(set(OWNERSHIP) - set(found))}


def test_pyproject_ships_exactly_the_package_and_no_data():
    wheel = tomllib.loads((PKG_DIR.parent / "pyproject.toml").read_text())["tool"]["hatch"]["build"]["targets"]["wheel"]
    assert wheel.get("packages") == ["coord_fold"], wheel
    assert not ({"include", "artifacts", "force-include", "only-include"} & set(wheel)), wheel


# --- G20 cli.py is wiring only, RECURSIVELY -------------------------------------------

def test_cli_top_level_definitions_are_exactly_the_manifest():
    got = set(_top_defs(_tree("cli.py")))
    assert got == CLI_EXACT_DEFINITIONS, {"extra": sorted(got - CLI_EXACT_DEFINITIONS), "missing": sorted(CLI_EXACT_DEFINITIONS - got)}


def test_cli_contains_no_nested_definitions_anywhere():
    """Round-3: implementation hidden as nested defs inside an allowed function."""
    tree = _tree("cli.py")
    for top in tree.body:
        for node in ast.walk(top):
            if node is top:
                continue
            assert not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)), (
                f"cli.py nests a definition inside {getattr(top, 'name', type(top).__name__)}")


def test_cli_functions_respect_per_function_budgets():
    for node in _tree("cli.py").body:
        if isinstance(node, ast.FunctionDef):
            stmts = sum(1 for n in ast.walk(node) if isinstance(n, ast.stmt)) - 1
            nodes = sum(1 for _ in ast.walk(node))
            assert stmts <= CLI_MAX_STATEMENTS_PER_FUNCTION, f"cli.{node.name}: {stmts} statements"
            assert nodes <= CLI_MAX_NODES_PER_FUNCTION, f"cli.{node.name}: {nodes} AST nodes"


# --- G19 DAG + G20 semantic delegation ban ----------------------------------------------

def test_every_intra_package_import_is_an_allowed_edge():
    for mod, allowed in ALLOWED_EDGES.items():
        bad = _package_imports(_tree(mod)) - allowed
        assert not bad, f"{mod} imports {sorted(bad)}"


def test_no_owner_module_imports_cli():
    for mod in OWNER_MODULES:
        assert "cli" not in _package_imports(_tree(mod)), f"{mod} imports cli"


def test_absolute_imports_are_stdlib_or_fulcra_common_only():
    """Also the §7 composition-root exclusion proof: the inbox reconciler cannot be imported."""
    for mod in OWNERSHIP:
        bad = _absolute_imports(_tree(mod)) - STDLIB_ALLOWED - THIRD_PARTY_ALLOWED
        assert not bad, f"{mod} imports {sorted(bad)}"


def test_no_banned_name_or_attribute_anywhere():
    """Closes sys.modules / getattr indirection and runtime code loading in one sweep."""
    for mod in OWNERSHIP:
        for node in ast.walk(_tree(mod)):
            if isinstance(node, ast.Name) and node.id in BANNED_NAMES:
                raise AssertionError(f"{mod} uses {node.id}")
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and (node.value.id, node.attr) in BANNED_ATTRS:
                raise AssertionError(f"{mod} uses {node.value.id}.{node.attr}")


def test_owner_callables_only_call_what_the_dag_allows():
    """The call-graph rule. Inside each owner's manifest callable, every call resolves to a
    same-module definition, an import alias of that module, or an allowed builtin."""
    for mod in OWNER_MODULES:
        tree = _tree(mod)
        local = set(_top_defs(tree)) | _import_aliases(tree)
        for top in tree.body:
            if not (isinstance(top, ast.FunctionDef) and OWNERSHIP[mod].get(top.name) == "callable"):
                continue
            bound = set(a.arg for a in ast.walk(top) if isinstance(a, ast.arg))
            bound |= {n.id for n in ast.walk(top) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
            for call in (n for n in ast.walk(top) if isinstance(n, ast.Call)):
                f = call.func
                root = f.id if isinstance(f, ast.Name) else (f.value.id if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) else None)
                if root is None:
                    continue  # a call on an expression result (e.g. x.get(...)()) — covered by name bans
                assert root in local or root in bound or root in BUILTIN_CALLS_ALLOWED, (
                    f"{mod}.{top.name} calls through {root!r}, which is not a local definition, an allowed import, or an allowed builtin")


def test_no_manifest_callable_is_a_one_statement_forwarding_wrapper():
    """Retained as a cheap early signal; the guarantee is the call-graph rule above."""
    for mod, symbols in OWNERSHIP.items():
        for node in _tree(mod).body:
            if isinstance(node, ast.FunctionDef) and symbols.get(node.name) == "callable":
                body = [n for n in node.body if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant))]
                if len(body) == 1 and isinstance(body[0], ast.Return) and isinstance(body[0].value, ast.Call):
                    raise AssertionError(f"{mod}.{node.name} is a one-line forwarding wrapper")


# --- G21 mass -------------------------------------------------------------------------

def test_mass_ceilings_recursive():
    for p in PKG_DIR.rglob("*.py"):
        if "__pycache__" in p.parts:
            continue
        nodes = sum(1 for _ in ast.walk(ast.parse(p.read_text())))
        assert p.stat().st_size <= MAX_BYTES, f"{p.name}: {p.stat().st_size} bytes"
        assert nodes <= (MAX_NODES_CLI if p.name == "cli.py" else MAX_NODES), f"{p.name}: {nodes} AST nodes"
```

- [ ] **Step 2: Run now — expect failures naming missing modules.** Commit as failing-first: `coord-fold: ownership, recursive cli policy, DAG, semantic delegation ban, mass (G18–G21) — failing until Tasks 4–10`.

- [ ] **Step 3 (after Task 10): Run — expect 16 passed.** If a symbol drifted, fix the module to the manifest, never the manifest to the module.

- [ ] **Step 4 (after Task 10): MUTATION-VERIFY — the reviewers' counterexamples, one each**

```bash
# (a) r2: duplicate build_payload into cli.py (duplication, so the suite still imports)
python - <<'PY'
p="coord_fold/cli.py"; open(p,"a").write("\n\ndef build_payload(**kw):\n    return dict(kw)\n")
PY
python -m pytest tests/test_ownership.py -q     # expect 2 FAIL: second-module + cli exact set
git checkout coord_fold/cli.py

# (b) r3: implementation in cli.py, one-line wrapper in events.py (events imports cli)
python - <<'PY'
c="coord_fold/cli.py"; open(c,"a").write("\n\ndef _build_payload_impl(**kw):\n    return dict(kw)\n")
e="coord_fold/events.py"; t=open(e).read().replace("def build_payload(", "def _real_build_payload(",1)
t += "\nfrom . import cli as _cli\n\ndef build_payload(**kw):\n    return _cli._build_payload_impl(**kw)\n"
open(e,"w").write(t)
PY
python -m pytest tests/test_ownership.py -q     # expect >=4 FAIL: DAG edge, owner-imports-cli, call-graph, wrapper, cli exact set
git checkout coord_fold/cli.py coord_fold/events.py

# (c) r4: NESTED implementation inside an allowed cli.py function
python - <<'PY'
p="coord_fold/cli.py"; s=open(p).read()
s=s.replace("def _now() -> str:", "def _now() -> str:\n    def _hidden_engine(x):\n        return x\n    _hidden_engine(0)",1)
open(p,"w").write(s)
PY
python -m pytest tests/test_ownership.py -q     # expect 1 FAIL: nested definition
git checkout coord_fold/cli.py

# (d) r4: TWO-STATEMENT sys.modules delegation in an owner, no import of cli
python - <<'PY'
e="coord_fold/events.py"; t=open(e).read().replace("def build_payload(", "def _real_build_payload(",1)
t += "\nimport sys\n\ndef build_payload(**kw):\n    impl = sys.modules['coord_fold.cli']._emit_kind\n    return impl(**kw)\n"
open(e,"w").write(t)
PY
python -m pytest tests/test_ownership.py -q     # expect >=2 FAIL: banned sys.modules attribute + call-graph (impl is bound but sys is not a local def; the attribute ban fires first)
git checkout coord_fold/events.py

# (e) r4: getattr indirection
python - <<'PY'
e="coord_fold/events.py"; t=open(e).read().replace("def build_payload(", "def _real_build_payload(",1)
t += "\nimport json as _j\n\ndef build_payload(**kw):\n    return getattr(_j, 'dumps')(kw)\n"
open(e,"w").write(t)
PY
python -m pytest tests/test_ownership.py -q     # expect 1 FAIL: banned name getattr
git checkout coord_fold/events.py

# (f) mass: dense padding under the line ceiling
python - <<'PY'
p="coord_fold/cli.py"; open(p,"a").write("\n" + "; ".join(f"_p{i}=[{i}]*{i}" for i in range(1,220)) + "\n")
PY
python -m pytest tests/test_ownership.py -q     # expect >=2 FAIL: cli exact set + cli node ceiling
git checkout coord_fold/cli.py
```

- [ ] **Step 5: Commit** — `coord-fold: ownership gates green after Task 10; mutation-verified against all six bypass shapes (r2, r3, r4)`

---

### Task 4: Event schema (G1–G3)

**Interfaces (produced):** `events.KINDS`, `events.PRIORITIES`, `events.PAYLOAD_VERSION`, `events.build_payload(*, at, sender, to, kind, slug, pri, ptr) -> dict`, `events.parse_event(record) -> dict | None`.

- [ ] **Step 1: Failing tests** — `tests/test_events.py`

```python
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
    assert ev and ev["record_id"] == "abc" and ev["recorded_at"] == "T2" and ev["kind"] == "open"


def test_parse_skips_free_text_and_foreign_payloads_silently():
    assert events.parse_event({"id": "x", "note": "hello"}) is None
    assert events.parse_event(_rec({"kind": "directive", "v": 1})) is None
    assert events.parse_event(_rec({"v": 2, "kind": "open", "slug": "s"})) is None
```

- [ ] **Step 2: Run — ImportError. Step 3: Implement**

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

- [ ] **Step 4: Run — 6 passed. Mutation:** change `if ptr is None and kind in _PTR_REQUIRED` to `if False` → first refusal test FAILS; restore. **Commit** — `coord-fold: event payload v1 (G1–G3)`

---

### Task 5: Channel resolution + the golden-compared write (G15)

**Interfaces (produced):** `channel.CONFIG_PATH`, `channel.ChannelUnresolved`, `channel.config_path(team) -> str`, `channel.resolve(reader, team) -> dict[str,str]`.

- [ ] **Step 1: Failing tests** — `tests/test_channel.py`

```python
import json
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
    st = FakeStore({CFG: json.dumps({"api_version": "v1alpha1"})}, [])
    with pytest.raises(channel.ChannelUnresolved):
        channel.resolve(FakeReader(st), "r")


def test_write_event_stdin_document_matches_the_old_transport_keys(monkeypatch):
    """GOLDEN: copy the key set from coord_engine/transport.py record_write (~line 385 at 5db5c3e5).
    EDIT the expected set if the old transport's keys differ; never guess them."""
    import subprocess
    seen = {}
    def fake_run(argv, input=None, **kw):
        seen["doc"] = json.loads(input)
        class R: returncode, stdout, stderr = 0, "", ""
        return R()
    monkeypatch.setattr(subprocess, "run", fake_run)
    w = CliPointerWriter(cli=["true"])
    w.write_event({"data_type": "D", "api_version": "v1alpha1"},
                  {"v": 1, "at": "T", "from": "a", "to": "b", "kind": "note", "slug": "s", "pri": "P3", "ptr": None}, sender="a")
    assert set(seen["doc"]) == {"data_type", "api_version", "note", "source", "recorded_at"}
```

- [ ] **Step 2: Run — ImportError. Step 3: Implement**

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

- [ ] **Step 4: Run — 4 passed. Mutation:** replace the first `raise ChannelUnresolved(...)` with `return {"data_type": "hardcoded", "api_version": "v1alpha1"}` → 2 FAIL; restore. **Commit** — `coord-fold: channel resolution + golden-compared write keys (G15)`

---

### Task 6: Checkpoint schema (G4)

**Interfaces (produced):** `checkpoint.SCHEMA_VERSION`, `path(team, agent)`, `empty(now)`, `apply(state, event)`, `load(reader, team, agent) -> (state, "ok"|"fresh"|"corrupt"|"error")`, `save(writer, team, agent, state) -> bool`.

- [ ] **Step 1: Failing tests** — `tests/test_checkpoint.py`

```python
import json
from coord_fold import checkpoint as cp
from fakes import FakeReader, FakeStore, FakeWriter
NOW = "2026-09-04T13:45:00Z"


def _ev(kind, slug="s1", rid="r1", **kw):
    b = {"v": 1, "at": NOW, "from": "boss", "to": "me", "kind": kind, "slug": slug, "pri": "P1",
         "ptr": f"team/r/task/{slug}.md", "record_id": rid}
    b.update(kw); return b


def test_empty_has_exactly_the_six_fields():
    assert set(cp.empty(NOW)) == {"v", "cursor", "open", "unread_events", "unreadable_pointers", "seen"}


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
    st_ = FakeStore({}, []); r, w = FakeReader(st_), FakeWriter(st_)
    assert cp.load(r, "r", "me")[1] == "fresh"
    st_.docs[cp.path("r", "me")] = "not json"; assert cp.load(r, "r", "me")[1] == "corrupt"
    bad = FakeStore({}, []); bad.fail_reads = True; assert cp.load(FakeReader(bad), "r", "me")[1] == "error"
    s = cp.empty(NOW); cp.apply(s, _ev("open")); s["cursor"] = NOW
    assert cp.save(w, "r", "me", s)
    back, src = cp.load(r, "r", "me"); assert src == "ok" and back["open"] == s["open"]
```

- [ ] **Step 2: Run — ImportError. Step 3: Implement**

```python
# packages/coord-fold/coord_fold/checkpoint.py
"""One durable checkpoint per agent (spec §3.3). Six fields — `seen` is the idempotency ring
and is a stated field, not a hidden one (G4; see Ruling 1 for whether it survives)."""
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
    return {"v": SCHEMA_VERSION, "cursor": now, "open": {}, "unread_events": 0, "unreadable_pointers": [], "seen": []}


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

- [ ] **Step 4: Run — 5 passed. Mutation:** delete `if rid and rid in state["seen"]: return` → idempotency test FAILS; restore. **Commit** — `coord-fold: checkpoint v1, six stated fields, idempotent by record id (G4)`

---

### Task 7: `fold` (§3.3) with `--verify-pointers`

**Interfaces (produced):** `fold.OVERLAP_SECONDS`, `fold.FoldOutcome(state, source, applied, unread, rc)`, `fold.FoldRefused`, `fold.run(reader, writer, team, agent, *, now, max_events=5000, verify_pointers=False) -> FoldOutcome`; `cli.main(argv=None, *, reader=None, writer=None) -> int`. Exit codes: 0 complete; 2 refused; 3 UNKNOWN.

- [ ] **Step 1: Failing CLI-driven tests (G9)** — `tests/test_cli_fold.py`

```python
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


def _run(st, *extra):
    return main(["fold", "r", "--agent", "me", "--now", "2026-09-04T11:00:00Z", *extra], reader=FakeReader(st), writer=FakeWriter(st))


def _ckpt(st):
    return json.loads(st.saved[cp.path("r", "me")])


def test_fold_from_fresh_applies_open_events_and_stores_the_checkpoint():
    st = _team([_rec("open", "a", "2026-09-04T10:00:00Z", "1"), _rec("open", "b", "2026-09-04T10:01:00Z", "2")])
    assert _run(st) == 0 and set(_ckpt(st)["open"]) == {"a", "b"} and _ckpt(st)["cursor"] == "2026-09-04T11:00:00Z"


def test_close_after_open_removes_the_row():
    st = _team([_rec("open", "a", "2026-09-04T10:00:00Z", "1"), _rec("close", "a", "2026-09-04T10:05:00Z", "2")])
    _run(st); assert _ckpt(st)["open"] == {}


def test_events_for_someone_else_do_not_land_but_broadcast_does():
    st = _team([_rec("open", "a", "2026-09-04T10:00:00Z", "1", to="them"), _rec("open", "b", "2026-09-04T10:00:00Z", "2", to="all")])
    _run(st); assert set(_ckpt(st)["open"]) == {"b"}


def test_a_second_fold_reads_only_forward():
    st = _team([_rec("open", "a", "2026-09-04T10:00:00Z", "1")]); _run(st)
    st.events.append(_rec("close", "a", "2026-09-04T11:30:00Z", "2"))
    main(["fold", "r", "--agent", "me", "--now", "2026-09-04T12:00:00Z"], reader=FakeReader(st), writer=FakeWriter(st))
    assert _ckpt(st)["open"] == {} and _ckpt(st)["cursor"] == "2026-09-04T12:00:00Z"


def test_a_failed_event_read_does_NOT_advance_the_cursor_and_exits_3(capsys):
    st = _team([]); st.fail_events = True
    assert _run(st) == 3 and cp.path("r", "me") not in st.saved
    assert "degraded" not in capsys.readouterr().out.lower()


def test_more_events_than_the_cap_leaves_a_bounded_unread_count_and_exits_3():
    st = _team([_rec("open", f"s{i}", f"2026-09-04T10:{i:02d}:00Z", str(i)) for i in range(7)])
    assert _run(st, "--max-events", "5") == 3
    c = _ckpt(st); assert c["unread_events"] == 2 and len(c["open"]) == 5 and c["cursor"] == "2026-09-04T10:04:00Z"


def test_corrupt_checkpoint_is_refused_and_untouched(capsys):
    st = _team([]); st.docs[cp.path("r", "me")] = "{not json"
    assert _run(st) == 2 and cp.path("r", "me") not in st.saved and "corrupt" in capsys.readouterr().err


def test_unresolved_channel_is_refused():
    st = FakeStore({}, []); assert _run(st) == 2


def test_verify_pointers_records_an_absent_pointer_by_slug_and_exits_3():
    st = _team([_rec("open", "a", "2026-09-04T10:00:00Z", "1", ptr="team/r/task/gone.md")])
    assert _run(st, "--verify-pointers") == 3 and _ckpt(st)["unreadable_pointers"] == ["a"]


def test_verify_pointers_leaves_a_readable_pointer_alone_and_default_reads_none():
    st = _team([_rec("open", "a", "2026-09-04T10:00:00Z", "1")]); st.docs["team/r/task/a.md"] = "x"
    assert _run(st, "--verify-pointers") == 0 and _ckpt(st)["unreadable_pointers"] == []
    reads = []; r = FakeReader(st); orig = r.read_classified
    r.read_classified = lambda p: (reads.append(p), orig(p))[1]
    main(["fold", "r", "--agent", "me", "--now", "2026-09-04T11:00:00Z"], reader=r, writer=FakeWriter(st))
    assert not any("/task/" in p for p in reads)
```

- [ ] **Step 2: Run — ImportError. Step 3: Implement `fold.py`**

```python
# packages/coord-fold/coord_fold/fold.py
"""One pass: read forward from the cursor, apply, persist, report (spec §3.3). O(new events)."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, NamedTuple

from . import channel, checkpoint as cp, events
from .transport import PointerTransport, TransportUnavailable

OVERLAP_SECONDS = 5
_BROADCAST = "all"


class FoldOutcome(NamedTuple):
    state: dict[str, Any]
    source: str
    applied: int
    unread: int
    rc: int


class FoldRefused(RuntimeError):
    pass


def _minus_overlap(iso: str) -> str:
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return (dt - timedelta(seconds=OVERLAP_SECONDS)).strftime("%Y-%m-%dT%H:%M:%SZ")


def run(reader: PointerTransport, writer: Any, team: str, agent: str, *, now: str,
        max_events: int = 5000, verify_pointers: bool = False) -> FoldOutcome:
    cfg = channel.resolve(reader, team)
    state, source = cp.load(reader, team, agent)
    if source == "corrupt":
        raise FoldRefused("checkpoint is corrupt — left untouched for forensics; repair or reseed it explicitly")
    if source == "error":
        raise TransportUnavailable("checkpoint unreadable")
    if source == "fresh":
        state, since = cp.empty(now), "1970-01-01T00:00:00Z"
    else:
        since = _minus_overlap(state["cursor"])
    applied = unread = 0
    last_at = state["cursor"] if source == "ok" else now
    for rec in reader.read_events(cfg["data_type"], since):
        ev = events.parse_event(rec)
        if ev is None or (ev["to"] not in (agent, _BROADCAST) and ev["from"] != agent):
            continue
        if applied >= max_events:
            unread += 1
            continue
        cp.apply(state, ev)
        applied += 1
        last_at = ev.get("recorded_at") or ev["at"]
    state["unread_events"] = unread
    state["cursor"] = last_at if unread else now
    state["unreadable_pointers"] = []
    if verify_pointers:
        for slug, row in state["open"].items():
            _body, st = reader.read_classified(row["ptr"])
            if st != "ok":
                state["unreadable_pointers"].append(slug)
    if not cp.save(writer, team, agent, state):
        raise TransportUnavailable("checkpoint save failed")
    rc = 3 if (unread or state["unreadable_pointers"]) else 0
    return FoldOutcome(state, source, applied, unread, rc)
```

- [ ] **Step 4: Implement `cli.py` (skeleton + `fold`) — exactly the manifest names, nothing nested**

```python
# packages/coord-fold/coord_fold/cli.py
"""Six verbs. Wiring only, recursively (G20)."""
from __future__ import annotations

import argparse
import sys
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


def _render_open(state: dict) -> str:
    rows = sorted(state["open"].items(), key=lambda kv: (kv[1]["pri"], kv[1]["at"]))
    lines = [f"  [{r['pri']}] {slug}  from={r['from']}  ptr={r['ptr']}" + (f"  claimed_by={r['claimed_by']}" if r.get("claimed_by") else "")
             for slug, r in rows]
    return "\n".join(lines) if lines else "  (nothing open)"


def _report_unknowns(state: dict) -> None:
    if state.get("unread_events"):
        print(f"fold: {state['unread_events']} events unread past {state['cursor']} — the answer is missing those", file=sys.stderr)
    for slug in state.get("unreadable_pointers", []):
        print(f"fold: pointer for {slug} unreadable — that one row is UNKNOWN", file=sys.stderr)


def cmd_fold(args, reader, writer) -> int:
    try:
        out = fold.run(reader, writer, args.team, args.agent, now=args.now, max_events=args.max_events, verify_pointers=args.verify_pointers)
    except (channel.ChannelUnresolved, fold.FoldRefused) as exc:
        print(f"fold: refused — {exc}", file=sys.stderr)
        return RC_REFUSED
    except TransportUnavailable as exc:
        print(f"fold: UNKNOWN — event read did not complete ({exc}); cursor not advanced", file=sys.stderr)
        return RC_UNKNOWN
    print(f"fold [{args.agent}] cursor={out.state['cursor']} applied={out.applied} open={len(out.state['open'])} source={out.source}")
    print(_render_open(out.state))
    _report_unknowns(out.state)
    return out.rc


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="coord-fold")
    sub = p.add_subparsers(dest="verb", required=True)
    f = sub.add_parser("fold", help="advance my checkpoint, print what I owe")
    f.add_argument("team"); f.add_argument("--agent", required=True); f.add_argument("--now", default=None)
    f.add_argument("--max-events", type=int, default=5000); f.add_argument("--verify-pointers", action="store_true")
    f.set_defaults(func=cmd_fold)
    return p


def main(argv: list[str] | None = None, *, reader=None, writer=None) -> int:
    args = build_parser().parse_args(argv)
    if getattr(args, "now", None) is None:
        args.now = _now()
    if getattr(args, "at", None) is None and hasattr(args, "at"):
        args.at = _now()
    if reader is None or writer is None:
        reader, writer = _default_transports()
    return int(args.func(args, reader, writer))


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 5: Run — 10 passed. Mutations:** (a) wrap the `read_events` loop so `TransportUnavailable` becomes an empty list → the failed-read test FAILS; (b) delete the addressee filter → the someone-else test FAILS; (c) make `verify_pointers` unconditionally `False` → the absent-pointer test FAILS. Restore each. **Commit** — `coord-fold: fold verb + --verify-pointers; CLI-driven tests on the stored checkpoint (G9)`

---

### Task 8: `emit`

- [ ] **Step 1: Failing tests** — `tests/test_cli_emit.py`

```python
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

- [ ] **Step 2: Implement** — add to `cli.py`:

```python
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


def cmd_emit(args, reader, writer) -> int:
    return _emit_kind(reader, writer, args.team, sender=args.sender, to=args.to, kind=args.kind, slug=args.slug, pri=args.pri, ptr=args.ptr, at=args.at)
```

parser: `e = sub.add_parser("emit"); e.add_argument("team"); e.add_argument("--from", dest="sender", required=True); e.add_argument("--to", required=True); e.add_argument("--kind", required=True); e.add_argument("--slug", required=True); e.add_argument("--pri", required=True); e.add_argument("--ptr", default=None); e.add_argument("--at", default=None); e.set_defaults(func=cmd_emit)`

- [ ] **Step 3: Run — 4 passed. Mutation:** `return RC_UNKNOWN` → `return RC_OK` in `_emit_kind` → the failed-write test FAILS; restore. **Commit** — `coord-fold: emit verb`

---

### Task 9: `claim`, `release`, `close`

`claim` and `release` require the slug to be open in **my** checkpoint. `close` additionally requires `--evidence <ptr>` and **reads it**: absent → refused (rc 2); unreadable → UNKNOWN (rc 3) — different words, the U2 lesson.

- [ ] **Step 1: Failing tests** — `tests/test_cli_claim_release_close.py`

```python
import json
from coord_fold import checkpoint as cp
from coord_fold.cli import main
from fakes import FakeReader, FakeStore, FakeWriter
CFG = "team/r/_coord/bus-v4/records.json"; CFG_DOC = json.dumps({"data_type": "MomentAnnotation/x", "api_version": "v1alpha1"})
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


def test_claim_and_release_of_a_slug_i_do_not_owe_are_refused(capsys):
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

- [ ] **Step 2: Implement** — add to `cli.py`:

```python
def _owed_row(reader, team, agent, slug):
    state, src = checkpoint.load(reader, team, agent)
    if src != "ok" or slug not in state["open"]:
        return None
    return state["open"][slug]


def cmd_claim(args, reader, writer) -> int:
    row = _owed_row(reader, args.team, args.agent, args.slug)
    if row is None:
        print(f"claim: refused — {args.slug} is not open in {args.agent}'s checkpoint", file=sys.stderr)
        return RC_REFUSED
    return _emit_kind(reader, writer, args.team, sender=args.agent, to=row["from"], kind="claim", slug=args.slug, pri=row["pri"], ptr=row["ptr"], at=args.at)


def cmd_release(args, reader, writer) -> int:
    row = _owed_row(reader, args.team, args.agent, args.slug)
    if row is None:
        print(f"release: refused — {args.slug} is not open in {args.agent}'s checkpoint", file=sys.stderr)
        return RC_REFUSED
    return _emit_kind(reader, writer, args.team, sender=args.agent, to=row["from"], kind="release", slug=args.slug, pri=row["pri"], ptr=row["ptr"], at=args.at)


def cmd_close(args, reader, writer) -> int:
    row = _owed_row(reader, args.team, args.agent, args.slug)
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
```

parser: for `claim`/`release`/`close`: `sp.add_argument("team"); sp.add_argument("slug"); sp.add_argument("--agent", required=True); sp.add_argument("--at", default=None)`; `close` also `--evidence` required.

- [ ] **Step 3: Run — 5 passed. Mutation:** in `cmd_close`, make the `st == "error"` branch fall through → the unreadable assertion FAILS; restore. **Commit** — `coord-fold: claim, release, close`

---

### Task 10: `status`

Reads the checkpoint, prints it, reads no events, reads no pointers. Exit 0 no unknowns; 3 if `unread_events > 0` or `unreadable_pointers`; 2 if fresh (never folded) or corrupt.

- [ ] **Step 1: Failing tests** — `tests/test_cli_status.py`

```python
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


def test_status_exits_3_on_either_unknown(capsys):
    s = cp.empty("T"); s["unread_events"] = 12; assert _m(_st(s)) == 3 and "12 events unread" in capsys.readouterr().err
    s = cp.empty("T"); s["unreadable_pointers"] = ["s9"]; assert _m(_st(s)) == 3 and "pointer for s9" in capsys.readouterr().err


def test_status_never_folded_exits_2_and_reads_no_events(capsys):
    assert _m(FakeStore({}, [])) == 2 and "never folded" in capsys.readouterr().err
    st = _st(cp.empty("T")); r = FakeReader(st); r.read_events = lambda *a: (_ for _ in ()).throw(AssertionError("status read events"))
    assert main(["status", "r", "--agent", "me"], reader=r, writer=FakeWriter(st)) == 0
```

- [ ] **Step 2: Implement**

```python
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
    return RC_UNKNOWN if (state.get("unread_events") or state.get("unreadable_pointers")) else RC_OK
```

parser: `s = sub.add_parser("status"); s.add_argument("team"); s.add_argument("--agent", required=True); s.set_defaults(func=cmd_status)`

- [ ] **Step 3: Run — 3 passed; then Task 3 Step 3 (16 passed) and Task 3 Step 4 mutations. Mutation here:** final `return` → `RC_OK` → exits-3 test FAILS; restore. **Commit** — `coord-fold: status verb; ownership gates green`

---

### Task 11: Degradation vocabulary (G11)

- [ ] `tests/test_no_degraded_vocabulary.py`:

```python
import pathlib
import coord_fold
PKG_DIR = pathlib.Path(coord_fold.__file__).parent

def test_the_token_degraded_never_appears_in_the_package():
    hits = {p.name for p in PKG_DIR.rglob("*.py") if "__pycache__" not in p.parts and "degraded" in p.read_text().lower()}
    assert not hits, hits
```

Run — pass. **Mutation:** append `# degraded` to `fold.py` → FAIL; restore. **Commit.**

---

### Task 12: Seed export (old side, spec §5.1 step 1)

`coord-engine obligations export-open <team> --agent <a>`: reads the old stream fold's open set, writes one bus-v4 `open` event per slug via the old `transport.record_write`, idempotent via `_coord/bus-v4/seeded/<agent>.md`. Payload shape written literally (this package never imports `coord_fold`). Tests drive `cli.main` on a `FakeTransport` with `record_write`: one event per open slug with the eight fields; idempotent; refuses without a v4 config. Classify `cmd_obligations_export_open` as a write in `test_activity_covers_every_write_verb.py`. Mutation: remove the seed-marker guard → idempotency test FAILS. *(Code as in r3 Task 11; unchanged.)*

---

### Task 13: Dual-emit — the one hook (old side, spec §5.1 step 2)

`coord_engine/dual_emit.py::mirror(...)` called once at the end of `records.emit_event`, mapping `directive→open`, `response→close` (of the `closes` target), `claim→claim`, `verdict→note`; no v4 config → silent no-op; a mirror failure never fails the v3 write. Thread `closes=` through `_close_answered_directive`. Tests as in r3 Task 12. Mutation: hardcode a cfg when the v4 config is absent → the no-v4 test FAILS.

---

### Task 14: Comparator, `cutover-ready`, runbook (old side, spec §5.1 steps 3–5)

`obligations compare-to-fold` diffs **(slug, pri, ptr)** tuples between the old open set and the new checkpoint, appends `AGREE n=k` / `DIVERGE slugs=[...]` to `_coord/bus-v4/comparator/<agent>.log`, exits 0/1/3 (unknown if either side unreadable). `obligations cutover-ready` exits 0 only if the trailing `AGREE` run is ≥ N **and** its first and last timestamps are ≥ 24h apart **and** within that window the new checkpoint's open set both grew and shrank at least once. Tests: 24 lines one minute apart → not ready; 25h apart with no transition → not ready; with a grow and a shrink → ready. Runbook `docs/coord/COORD-FOLD-CUTOVER.md`: seed → dual-emit → shadow → `cutover-ready` → cut over (coord-boss first) → freeze by `"frozen": true` in the v3 config. Mutation: make the 24h check always true → the one-minute-apart test FAILS.

---

### Task 15: AGENTS.md (ship-gate)

Under `## Setup & tests`: the package exists; its four gate files and the CI step; six verbs and exit codes 0/2/3; the two unknowns and the `degraded` ban; **dependency direction** (`coord-engine` may read bus-v4 documents by path and write via `dual_emit`; `coord_fold` never imports `coord_engine`); the reader/writer boundary and why (`_run` and a shared base were not a boundary); the recursive `cli.py` policy and why (nested defs and `sys.modules` delegation); the ownership manifest and why (six shims passed everything). Commit — `coord-fold: AGENTS.md (ship-gate)`.

---

## Items that need a ruling before implementation

Event-correctness contracts raised by codex-reviewer (round 2). The plan proposes; it does not decide.

1. **Lossless cursor.** `read_events(channel, since)` filters `get-records` by timestamp. Whether that endpoint orders by `recorded_at` with a stable tiebreak, and whether a record can surface with a `recorded_at` earlier than one already returned, is an **upstream fact** nobody in this thread has. Proposal: measure it (two records with identical `recorded_at`, read back twice, compare); if ordering is stable and monotone, set `OVERLAP_SECONDS = 0` and drop `seen` (G4 back to five fields); if not, keep the ring and G4 stays at six. Until measured the plan keeps the ring and G4 says so.
2. **Single writer / lost update.** Proposal: the checkpoint carries `writer` (host id) and `written_at`; `save` re-reads first and **refuses** (exit 3) if `written_at` is newer than the value loaded at the start of this fold and `writer` differs; the store versions every upload (measured 2026-09-02), so the lost version is recoverable. Adds two fields to G4 if accepted.
3. **Retention / backlog.** A fold away for a month reads a month. Proposal is open question 3's snapshot event. Out of scope until ruled.
4. **`max_events` semantics.** The CLI returns the whole window in one call, so the read cost is one invocation regardless; the cap bounds *apply* work and the unread count is exact because the read already happened. The unbounded part is the window, which is item 3. Stated, not claimed.

## What this plan does not do (spec §10)

Does not fix the pre-fence publication overwrite (orthogonal: a stream fold never reads the aggregate). Does not migrate the anti-slop findings. Deletes nothing — bus-v3 is frozen, not removed. Does not implement the §7 inbox reconciler; it is post-cutover work, and Task 3's absolute-import allowlist is the proof it cannot be composed into a fold path.

## Revision log

- **r1 (2026-09-04):** initial plan from the spec; 14 tasks.
- **r2:** after codex-coder CHANGES — ownership manifest (G18), recursive artifact scan, runtime-loading ban; Task 14's filename shape test subsumed.
- **r3:** after round-2 CHANGES from both — G19–G22; import DAG; exact `cli.py` set; forwarding-wrapper detector; ceilings; reader/writer split (as a layered amendment); `release`; `--verify-pointers`; tuple comparator + 24h/transition gate; rulings section.
- **r4 (2026-09-04, after codex-coder round-3 CHANGES @ `6e0d42e5`): coherent rewrite.** Ownership is recursive — `cli.py` may nest no `def`/`class`/`lambda` and every function has a statement and node budget; delegation is closed semantically — banned names (`getattr`, `globals`, `vars`, `locals`, …), banned attributes (`sys.modules`, `importlib.*`, `marshal.loads`, `runpy.*`, `types.ModuleType`), and a call-graph rule that every call inside an owner's manifest callable resolves to a same-module definition, an allowed import alias, or an allowed builtin; the transport has **no generic argv receiver** — five sealed methods each calling `subprocess.run` with a literal argv, a test that every such call's constants are one of five fixed prefixes, no varargs anywhere, `Popen`/`os.system` banned; `CliPointerReader` and `CliPointerWriter` are unrelated classes with `__mro__ == (cls, object)` and a reader has no write primitive at any name; the manifest, Task 1 tests, the golden test (now patching `subprocess.run`), `fold.run(reader, writer, …)`, `cp.save(writer, …)`, `main(argv, *, reader, writer)` and the fakes (`FakeStore`/`FakeReader`/`FakeWriter`) are consistent everywhere; `seen` is a stated sixth checkpoint field under G4 rather than an underscore-hidden one. Task 3's mutation set (a)–(f) is the union of the r2, r3 and r4 counterexamples. **Task map r3→r4:** 2b+2c → 3; 3→4; 4→5; 5→6; 6+6b→7; 7→8; 8+8b→9; 9→10; 10→11; 11→12; 12→13; 13→14; 14→15.

## Self-review

1. **Spec coverage.** §3.1→T4. §3.2→G2, T7/T9 never list. §3.3→T6/T7. §3.4→T1. §4→T7/T10/T11. §5.1→T12/T13/T14. §5.2/§5.3→runbook. §6→verb table, T9 (`release`). §7→"does not do" + T3 allowlist as the exclusion proof. §8→G9/G10 every task, G11 T11. §9→rulings + `release` adopted. §1a.1–3→T1/T2/T3.
2. **Placeholder scan.** One deliberate golden-comparison step (T5 Step 1's key set) with the file and line to copy from. Tasks 12–14 reference r3's code for the old-side verbs by name rather than repeating ~150 unchanged lines; the r3 text is in the branch history at `6e0d42e5` and the mutations are stated here.
3. **Type consistency.** `reader`/`writer` everywhere; `read_classified` returns `(str|None, "ok"|"absent"|"error")` in T1/T5/T6/T7/T9; `write_event(cfg, payload, *, sender)` in T1/T8/T9; `checkpoint.path/empty/apply/load/save` identical across T6/T7/T9/T10; exit codes 0/2/3 in every verb; the manifest in T3 names exactly the symbols T1/T4–T7 define.
