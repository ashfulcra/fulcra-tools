"""Slice 3 CI gate: the docs answer "do I owe anything?" with no tooling.

Why a doc test is a real test here
----------------------------------
The obligation fold and the ``obligations`` verb only help an agent that has
coord-engine installed. This fleet's agents routinely do not: containers get
reclaimed and roll back their tooling (observed every hour for most of
2026-07-29), and a fresh agent joins before it installs anything. For those, the
documentation IS the implementation, and an incomplete or engine-dependent
procedure means they answer "nothing owed" by guessing.

So this gate holds the docs to the same standard as the code:

* the procedure exists and is findable;
* it names **every** component in the registry, so an agent following it cannot
  skip one — the one failure mode the fold itself cannot catch, because a
  component nobody named never reports anything;
* it invokes **no** coord-engine, or it is not an engine-absent procedure;
* it states the fail-closed rule in the imperative, not as an aside;
* and its commands actually run, in a subprocess where ``coord_engine`` is
  unimportable, against a fake store — including the case where one component is
  dark, which must be distinguishable from the case where none are.

The last point is the difference between a doc test and a spell-checker.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from coord_engine.obligations import OBLIGATION_COMPONENTS

DOC = (Path(__file__).resolve().parents[3] / "docs" / "coord" / "BUS-V3.md")
SECTION_TITLE = "### No engine? Carry the rule by hand"

#: A fake ``fulcra-api`` implementing only ``file list`` over a directory, plus a
#: dark-prefix switch. Small on purpose: the gate is about the documented
#: procedure, not about emulating the real CLI.
FAKE_CLI = r'''
import os, sys
from pathlib import Path
root = Path(os.environ["FAKE_ROOT"])
dark = os.environ.get("FAKE_DARK", "")
argv = sys.argv[1:]
if argv[:2] == ["file", "list"]:
    prefix = argv[2].lstrip("/")
    if dark and prefix.startswith(dark):
        sys.stderr.write("error: listing timed out\n")
        raise SystemExit(1)
    target = root / prefix
    if target.is_dir():
        for child in sorted(target.iterdir()):
            sys.stdout.write(f"0B    2026-07-29 10:00AM UTC  {child.name}\n")
    raise SystemExit(0)
sys.stderr.write(f"fake-fulcra-api: unsupported {argv}\n")
raise SystemExit(2)
'''


def doc_text() -> str:
    assert DOC.is_file(), f"missing {DOC}"
    return DOC.read_text(encoding="utf-8")


def section() -> str:
    text = doc_text()
    assert SECTION_TITLE in text, (
        f"BUS-V3.md must carry an engine-absent obligation procedure under "
        f"{SECTION_TITLE!r}; without it an agent with no tooling has no "
        "documented way to tell 'nothing owed' from 'never checked'"
    )
    body = text.split(SECTION_TITLE, 1)[1]
    # up to the next same-or-higher heading
    stop = re.search(r"^#{1,3} ", body, re.MULTILINE)
    return body[: stop.start()] if stop else body


def commands() -> list[str]:
    """Shell lines from the section's fenced blocks."""
    out: list[str] = []
    for block in re.findall(r"```bash\n(.*?)```", section(), re.DOTALL):
        for line in block.splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                out.append(line)
    return out


# --- the procedure must be complete and engine-free -------------------------

def test_every_registry_component_is_named_in_the_procedure():
    """The failure the fold cannot catch: a component nobody ever named.

    ``fold`` reports UNKNOWN for a component named in ``expected`` and not
    supplied. It cannot report anything about a component that was never in the
    list at all — and a hand-followed procedure IS that list. So the doc has to
    name all of them, and this test is what keeps the two in step.
    """
    body = section()
    missing = [c for c in OBLIGATION_COMPONENTS if c not in body]
    assert not missing, (
        f"the engine-absent procedure never mentions {missing}; an agent "
        "following it would report 'nothing owed' having never looked"
    )


def test_procedure_does_not_depend_on_the_engine():
    for line in commands():
        assert "coord-engine" not in line, (
            f"engine-absent procedure invokes the engine: {line!r}"
        )


def test_procedure_states_the_fail_closed_rule():
    body = section().lower()
    assert "unknown" in body
    assert "never" in body and "nothing owed" in body, (
        "the fail-closed rule must be stated outright — an agent that reads this "
        "under pressure needs the prohibition, not a hint"
    )
    assert "invalid" in body, "INVALID must stay distinct from UNKNOWN"


def test_procedure_has_runnable_commands():
    cmds = commands()
    assert cmds, "no commands in the engine-absent section"
    assert all("fulcra-api" in c for c in cmds), (
        f"every documented step should be a fulcra-api call; got {cmds}"
    )


# --- and it must actually run, with the engine unavailable ------------------

@pytest.fixture
def sandbox(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    """A fake store plus an env whose PATH has a fake ``fulcra-api`` and whose
    interpreter cannot import ``coord_engine``."""
    store = tmp_path / "store"
    for prefix in ("task", "review", "roles",
                   "_coord/forge/watch", "_coord/forge/feedback"):
        (store / "team" / "fulcra" / prefix).mkdir(parents=True, exist_ok=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shim = bin_dir / "fulcra-api"
    shim.write_text(f"#!{sys.executable}\n{FAKE_CLI}", encoding="utf-8")
    shim.chmod(0o755)

    env = {
        "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
        "FAKE_ROOT": str(store),
        "TEAM": "fulcra",
        "AGENT": "opie",
        # Empty PYTHONPATH so the subprocess cannot reach the workspace source:
        # an engine-absent test that can still import coord_engine is not one.
        "PYTHONPATH": "",
        "HOME": str(tmp_path),
    }
    return store, env


def _run_procedure(env: dict[str, str]) -> list[int]:
    """Run each documented command; return exit codes in order."""
    codes = []
    for line in commands():
        proc = subprocess.run(line, shell=True, env=env, capture_output=True,
                              text=True, timeout=60)
        codes.append(proc.returncode)
    return codes


def test_engine_is_genuinely_absent_in_the_sandbox(sandbox):
    """Guard against a vacuous gate: prove the engine really is unreachable."""
    _store, env = sandbox
    proc = subprocess.run(
        [sys.executable, "-S", "-c", "import coord_engine"],
        env=env, capture_output=True, text=True, timeout=60)
    assert proc.returncode != 0, (
        "coord_engine is importable in the sandbox, so this suite is not "
        "testing the engine-absent path at all"
    )


def test_documented_procedure_runs_clean_when_every_component_is_readable(sandbox):
    _store, env = sandbox
    assert _run_procedure(env) == [0] * len(commands()), (
        "the documented procedure must succeed on a healthy store, or an agent "
        "following it can never legitimately claim CLEAR"
    )


def test_a_dark_component_makes_the_procedure_fail_loudly(sandbox):
    """One dark component and the agent CANNOT reach a clean run.

    This is the executable form of the doc's rule. If the review listing being
    down still produced all-zero exits, the procedure would let a careful agent
    conclude "nothing owed" from an incomplete check.
    """
    _store, env = sandbox
    env = dict(env, FAKE_DARK="team/fulcra/review")
    codes = _run_procedure(env)
    assert any(code != 0 for code in codes), (
        "a dark component produced an all-clean run; the documented procedure "
        "cannot distinguish 'nothing owed' from 'never checked'"
    )


def test_each_component_can_independently_darken(sandbox):
    """No component is silently unchecked by the procedure.

    A step that is documented but never actually reaches its prefix would pass
    the text tests and still leave that component unverified forever.
    """
    _store, env = sandbox
    for prefix in ("team/fulcra/task", "team/fulcra/review", "team/fulcra/roles",
                   "team/fulcra/_coord/forge"):
        codes = _run_procedure(dict(env, FAKE_DARK=prefix))
        assert any(code != 0 for code in codes), (
            f"darkening {prefix} changed nothing — the procedure never reads it, "
            "so that component is documented but unchecked"
        )


def test_v2_activation_gates_include_the_obligation_hook():
    """The v2 gap is a binding activation precondition, not a footnote.

    Ratified at the s3 merge. `queue --obligations` hooks only the v1 read path,
    so activating v2 without porting it silently stops reconciliation — and a
    fold that never runs is indistinguishable from a fold that found nothing.
    This test is what keeps that promise attached to the gate list rather than
    to somebody's memory.
    """
    body = doc_text()
    marker = "### Cursor-v2 activation and physical isolation"
    assert marker in body
    section_body = body.split(marker, 1)[1].split("\nVersion 1.10.0", 1)[0]
    assert "three hard activation gates" in section_body, (
        "the gate count must match the enumerated gates"
    )
    assert "obligation fold must be hooked on the v2 read path" in section_body


def test_v2_read_path_has_no_obligation_hook_and_says_so():
    """Pin the known gap at the code, not only in the doc.

    codex-reviewer's residual finding on the rc separation: v2 reads return
    before reconciliation, so the opt-in fold is a legacy-cursor integration
    only. That is fine while v2 is dormant and it is a binding activation
    precondition — but "fine because documented elsewhere" decays. This asserts
    the warning lives on the function that would cause the problem, so somebody
    porting v2 reads it in the diff rather than in a doc they did not open.
    """
    import inspect
    from coord_engine import cli

    src = inspect.getsource(cli._cmd_queue_v2)
    assert "NO OBLIGATION RECONCILIATION HAPPENS HERE" in src
    assert "v2-activation precondition" in src
    # And the gap must still be real. Checking for a CALL (open paren), not a
    # mention: the warning above names the function, and an assertion that
    # cannot tell a reference from an invocation would fire on the very comment
    # it is guarding.
    assert "_reconcile_after_empty_read(" not in src, (
        "v2 now reconciles — remove the warning and the third activation gate "
        "in the same change, and update this test"
    )
