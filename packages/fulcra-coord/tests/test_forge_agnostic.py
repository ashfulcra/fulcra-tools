"""Enforce the forge-agnostic invariant on the coordination CORE.

THE INVARIANT (see AGENTS.md "Code review & merge" and SKILL.md): the
coordination bus is forge-agnostic. Nothing in ``fulcra_coord/*.py`` may CALL a
specific forge — no shelling out to ``gh``, no GitHub/GitLab API client. GitHub
is one OPTIONAL integration; the review/merge handshake coordinates an opaque
artifact ref (PR# / MR# / branch / commit SHA / URL / patch / non-code
deliverable) on the bus, and verdicts ride the bus
(``request-review <artifact>`` -> ``review-done --verdict``). A forge is sugar a
human/agent invokes SEPARATELY.

Why an AST scan and not a grep: the core is FULL of legitimate *mentions* of
"GitHub" / "PR" / "gh" — docstrings literally say "coord NEVER calls gh", help
text lists "PR#/MR#/branch", and the artifact under review may itself BE a
GitHub PR. A grep would drown in those false positives. We parse each module and
flag only actual forge *calls*: a forbidden import, or a ``subprocess`` /
``os.system`` invocation whose command LITERAL starts with a forge CLI. Commands
built from non-literals (coord's dynamic *Fulcra* CLI argv) are out of scope —
only detectable forge-CLI literals are forbidden.
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

# Top-level module names of forge API clients. An import of any of these from
# the core is a hard violation — the core must never talk to a forge's API.
FORBIDDEN_IMPORT_ROOTS = {"github", "pygithub", "gitlab"}
# Case-insensitive comparison handles "PyGithub" / "GitHub" spellings.
_FORBIDDEN_IMPORT_ROOTS_LC = {name.lower() for name in FORBIDDEN_IMPORT_ROOTS}

# subprocess entry points (and os.system) that actually run a command.
_SUBPROCESS_RUNNERS = {"run", "Popen", "call", "check_output", "check_call"}

# First-token literals that mean "we are driving a forge CLI". ``gh`` is the
# GitHub CLI; ``glab`` the GitLab CLI; pushing to a forge is also forbidden (the
# core never mutates a remote). Plain ``git`` reads are NOT here — only the
# write/forge-driving forms below are flagged.
_FORBIDDEN_FIRST_TOKENS = {"gh", "glab"}
# String-command prefixes (for ``os.system("...")`` / ``shell=True`` strings).
_FORBIDDEN_STR_PREFIXES = ("gh ", "glab ", "git push", "git remote add")


def _first_command_token(call: ast.Call) -> str | None:
    """Return the lowercased first command token of a subprocess/os.system call.

    Resolves only DETECTABLE literals: a list/tuple whose first element is a
    string constant (``["gh", ...]``) or a bare string constant (``"gh ..."``).
    Returns ``None`` when the command is non-literal (e.g. ``cmd + ["file"]``,
    coord's dynamic Fulcra argv) — those are deliberately out of scope.
    """
    if not call.args:
        return None
    first = call.args[0]
    if isinstance(first, (ast.List, ast.Tuple)):
        if first.elts and isinstance(first.elts[0], ast.Constant) \
                and isinstance(first.elts[0].value, str):
            return first.elts[0].value
        return None
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        return first.value
    return None


def _callee_name(func: ast.AST) -> tuple[str | None, str | None]:
    """Return ``(module_attr, attr)`` for a call's callee.

    ``subprocess.run(...)`` -> ``("subprocess", "run")``;
    ``os.system(...)``      -> ``("os", "system")``;
    a bare ``run(...)``     -> ``(None, "run")``.
    """
    if isinstance(func, ast.Attribute):
        owner = func.value
        owner_name = owner.id if isinstance(owner, ast.Name) else None
        return owner_name, func.attr
    if isinstance(func, ast.Name):
        return None, func.id
    return None, None


def _forge_violations(source: str, filename: str) -> list[str]:
    """Scan one module's source for actual forge CALLS.

    Returns a list of ``"<filename>:<lineno> <reason>"`` strings — empty when the
    module is forge-free. Detects two violation classes:

      1. importing a forge API client (``import github`` / ``from gitlab ...``);
      2. shelling out to a forge CLI (``subprocess.run(["gh", ...])`` and kin,
         or ``os.system("gh ...")`` / a ``git push`` to a remote).

    Mentions in docstrings, comments and unrelated string literals are invisible
    to the AST and therefore never flagged.
    """
    violations: list[str] = []
    tree = ast.parse(source, filename=filename)

    for node in ast.walk(tree):
        # --- 1. forbidden imports -------------------------------------------
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0].lower()
                if root in _FORBIDDEN_IMPORT_ROOTS_LC:
                    violations.append(
                        f"{filename}:{node.lineno} imports forge client "
                        f"'{alias.name}'"
                    )
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0].lower()
            if root in _FORBIDDEN_IMPORT_ROOTS_LC:
                violations.append(
                    f"{filename}:{node.lineno} imports from forge client "
                    f"'{node.module}'"
                )

        # --- 2. forge-CLI subprocess calls ----------------------------------
        elif isinstance(node, ast.Call):
            owner, attr = _callee_name(node.func)
            is_subprocess = owner == "subprocess" and attr in _SUBPROCESS_RUNNERS
            is_os_system = owner == "os" and attr == "system"
            if not (is_subprocess or is_os_system):
                continue
            token = _first_command_token(node)
            if token is None:
                continue
            first_word = token.split()[0].lower() if token.split() else ""
            token_lc = token.lower()
            if first_word in _FORBIDDEN_FIRST_TOKENS or \
                    token_lc.startswith(_FORBIDDEN_STR_PREFIXES):
                violations.append(
                    f"{filename}:{node.lineno} shells out to forge CLI "
                    f"'{token}'"
                )

    return violations


# THE one sanctioned exception (phase 2): forge_mirror.py IS the designed
# forge bridge — the single place the system may drive ``gh``, mirroring
# verdict-shaped forge signals into the evidence sub-log (marked
# source=forge-mirror, never closing a loop). Exempting it here does NOT
# weaken the invariant; its containment is enforced from the other direction
# by the reverse fitness pin (test_fulcra_coord.py::TestLoopsLayering::
# test_no_core_module_imports_forge_mirror): no core module may import it, so
# forge calls can never leak from the bridge back into the core.
_SANCTIONED_FORGE_BRIDGE = "forge_mirror.py"


def _core_source_files() -> list[Path]:
    """Every CORE module: ``fulcra_coord/*.py`` minus the one sanctioned
    forge bridge (see ``_SANCTIONED_FORGE_BRIDGE`` above).

    Deliberately top-level only — NOT tests/, adapters/, or docs/. Adapters are
    where a forge integration WOULD live if one existed; the invariant guards the
    core, not the optional edges.
    """
    pkg_dir = Path(__file__).resolve().parent.parent / "fulcra_coord"
    return sorted(p for p in pkg_dir.glob("*.py")
                  if p.name != _SANCTIONED_FORGE_BRIDGE)


class ForgeAgnosticCoreTest(unittest.TestCase):
    """The real scan: the core tree must contain zero forge calls."""

    def test_core_makes_no_forge_calls(self) -> None:
        files = _core_source_files()
        self.assertTrue(files, "expected to find fulcra_coord/*.py source files")

        all_violations: list[str] = []
        for path in files:
            source = path.read_text(encoding="utf-8")
            all_violations.extend(_forge_violations(source, str(path)))

        self.assertEqual(
            all_violations,
            [],
            "fulcra_coord core must be forge-agnostic — it may NEVER call a "
            "specific forge (no gh/glab, no GitHub/GitLab client). Violations:\n"
            + "\n".join(all_violations),
        )


class ForgeViolationCheckerUnitTest(unittest.TestCase):
    """Non-vacuity: the checker demonstrably FLAGS a real forge call.

    Without these the green core scan could pass simply because the checker
    never fires. Each case feeds a synthetic snippet and asserts a flag.
    """

    def test_flags_gh_subprocess_call(self) -> None:
        snippet = (
            "import subprocess\n"
            'subprocess.run(["gh", "pr", "merge"])\n'
        )
        violations = _forge_violations(snippet, "<synthetic>")
        self.assertEqual(len(violations), 1, violations)
        self.assertIn("gh", violations[0])
        self.assertIn("<synthetic>:2", violations[0])

    def test_flags_forge_client_import(self) -> None:
        violations = _forge_violations("import github\n", "<synthetic>")
        self.assertEqual(len(violations), 1, violations)
        self.assertIn("github", violations[0])

    def test_flags_from_gitlab_import(self) -> None:
        violations = _forge_violations(
            "from gitlab import Gitlab\n", "<synthetic>"
        )
        self.assertEqual(len(violations), 1, violations)

    def test_does_not_flag_mentions_in_docstrings_or_strings(self) -> None:
        # The exact shapes that live in the real core: docstrings/help text that
        # mention gh/GitHub/PR, and the allowed dynamic-Fulcra-CLI subprocess.
        snippet = (
            '"""coord NEVER calls gh — GitHub is optional sugar."""\n'
            "import subprocess\n"
            "help_text = 'What to review: PR#/MR#/branch/commit SHA'\n"
            "# we never shell out to gh from here\n"
            "cmd = ['fulcra', 'file']\n"
            'subprocess.run(cmd + ["download", "-"])\n'
            'subprocess.run(["launchctl", "load", "-w", plist])\n'
        )
        self.assertEqual(_forge_violations(snippet, "<synthetic>"), [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
