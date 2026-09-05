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
INVOCATION = re.compile(r"ship_check\.py\s+(\S+)\s+(\S+)([^\n`]*)")   # r38 (codex-coder round 33): match EVERY invocation, then judge the head POSITIVELY
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
        problems = ([head_problem(m.group(2))] if head_problem(m.group(2)) else []) + parse_invocation(m.group(3))
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
