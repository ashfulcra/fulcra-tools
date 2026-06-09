"""Package-boundary fitness test — event substrate must not import upward.

THE INVARIANT:

The event substrate sits at the BOTTOM of the fulcra-coord layering stack.
Anything above it (feature / orchestration / visibility modules) must depend
DOWN, never the reverse.  Introducing an upward import recreates the tangle
this migration is designed to eliminate — so the violation must be caught
automatically, not by code review.

Two substrate files, two distinct tiers:

  events.py — PURE LEAF
    Allowed fulcra_coord imports: ``timeutil`` only.
    Everything else — including ``remote`` (I/O), ``eventlog``, ``schema``,
    and all feature modules — is a violation.  The reducer must be testable
    from a bare event list with zero service dependencies.

  eventlog.py — I/O leaf-adjacent
    Allowed fulcra_coord imports: ``remote``, ``events``, ``timeutil``.
    Forbidden: the FEATURE / ORCHESTRATION / VISIBILITY layer listed in
    ``_FORBIDDEN_UPWARD_MODULES`` below.

Why AST and not grep:
    grep cannot distinguish an import from a docstring mention.  The substrate
    files legitimately *mention* lifecycle, views, etc. in their module-level
    docstrings to DOCUMENT what they must not import.  An AST walk sees only
    real import nodes.

See also: ``test_forge_agnostic.py`` for the parallel guard on forge-API calls.
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

# ---------------------------------------------------------------------------
# The forbidden set — feature / orchestration / visibility modules.
# These are the layers ABOVE the substrate; importing any of them from
# events.py or eventlog.py would re-create an upward dependency.
#
# NOT included (these are leaf/transport/low-level, NOT upward):
#   remote, timeutil, schema, io, cache, output, textfmt, __init__
# ---------------------------------------------------------------------------
_FORBIDDEN_UPWARD_MODULES: frozenset[str] = frozenset(
    {
        "lifecycle",
        "views",
        "query",
        "digest",
        "annotations",
        "inbox",
        "retention",
        "presence",
        "routing_ops",
        "installers",
        "openclaw",
        "openclaw_plugin",
        "claude_code",
        "codex",
        "cli",
        "writepipe",
        "config",
        "doctor",
        "heartbeat",
        "digest_schedule",
    }
)


def _imported_submodules(source: str) -> set[str]:
    """Return the set of fulcra_coord submodule names imported by *source*.

    Recognises four import forms:

    1. ``import fulcra_coord.X`` or ``import fulcra_coord.X.y`` → ``"X"``
    2. ``from fulcra_coord import X`` or ``from fulcra_coord.X import y`` → ``"X"``
    3. ``from . import X`` → ``"X"``
    4. ``from .X import y`` → ``"X"``

    Plain stdlib/third-party imports (``import os``, ``from typing import Any``)
    produce no entries because they don't name a fulcra_coord submodule.

    The returned strings are the immediate submodule component — the first dotted
    name segment after ``fulcra_coord``.  This is sufficient to identify whether a
    module belongs to the forbidden upward layer or to an allowed tier.

    Args:
        source: Python source text to scan.

    Returns:
        Set of submodule name strings (possibly empty).
    """
    tree = ast.parse(source)
    names: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            # ``import fulcra_coord.X[.y...]``
            for alias in node.names:
                parts = alias.name.split(".")
                if parts[0] == "fulcra_coord" and len(parts) >= 2:
                    names.add(parts[1])

        elif isinstance(node, ast.ImportFrom):
            level = node.level or 0   # 0 = absolute, ≥1 = relative

            if level >= 1:
                # Relative import — always inside the fulcra_coord package.
                # ``from . import X`` → module is None, names contain X.
                # ``from .X import y`` → module is "X".
                module = node.module or ""
                if module:
                    # ``from .X import y`` or ``from .X.y import z``
                    names.add(module.split(".")[0])
                else:
                    # ``from . import X, Y``
                    for alias in node.names:
                        names.add(alias.name.split(".")[0])
            else:
                # Absolute import — check if it targets fulcra_coord.
                # ``from fulcra_coord import X``  → module == "fulcra_coord"
                # ``from fulcra_coord.X import y`` → module == "fulcra_coord.X"
                module = node.module or ""
                parts = module.split(".")
                if parts[0] == "fulcra_coord":
                    if len(parts) >= 2:
                        # ``from fulcra_coord.X import y`` → submodule is X
                        names.add(parts[1])
                    else:
                        # ``from fulcra_coord import X, Y``
                        for alias in node.names:
                            names.add(alias.name.split(".")[0])

    return names


def _substrate_file(name: str) -> Path:
    """Resolve a substrate filename relative to the package root."""
    pkg_dir = Path(__file__).resolve().parent.parent / "fulcra_coord"
    return pkg_dir / name


class EventsIsPureLeafTest(unittest.TestCase):
    """events.py must import ONLY stdlib + fulcra_coord.timeutil.

    It is the pure reducer leaf — no I/O, no feature modules, no transport.
    Even ``remote`` is forbidden here because it performs I/O.
    """

    def test_events_is_pure_leaf(self) -> None:
        path = _substrate_file("events.py")
        self.assertTrue(path.exists(), f"substrate file not found: {path}")

        source = path.read_text(encoding="utf-8")
        imported = _imported_submodules(source)

        # The ONLY fulcra_coord submodule permitted in the pure leaf.
        _ALLOWED = {"timeutil"}
        offenders = imported - _ALLOWED

        self.assertEqual(
            offenders,
            set(),
            f"events.py is supposed to be a pure leaf "
            f"(stdlib + fulcra_coord.timeutil only), but it imports "
            f"fulcra_coord submodule(s) that are not allowed: "
            f"{sorted(offenders)}.  "
            f"Full imported set: {sorted(imported)}.  "
            f"Fix: move any I/O / feature logic out of events.py.",
        )


class EventlogDoesNotImportUpwardTest(unittest.TestCase):
    """eventlog.py may import remote / events / timeutil, but NOT feature modules.

    Allowed tier: stdlib + remote (transport) + events (the pure leaf) + timeutil.
    Forbidden tier: the feature / orchestration / visibility layer in
    ``_FORBIDDEN_UPWARD_MODULES``.
    """

    def test_eventlog_does_not_import_upward(self) -> None:
        path = _substrate_file("eventlog.py")
        self.assertTrue(path.exists(), f"substrate file not found: {path}")

        source = path.read_text(encoding="utf-8")
        imported = _imported_submodules(source)

        upward_violations = imported & _FORBIDDEN_UPWARD_MODULES

        self.assertEqual(
            upward_violations,
            set(),
            f"eventlog.py must not import feature/orchestration/visibility "
            f"modules (that would be an upward dependency).  "
            f"Forbidden modules found: {sorted(upward_violations)}.  "
            f"Full imported set: {sorted(imported)}.  "
            f"Fix: move any feature-layer logic out of eventlog.py.",
        )


class SyntheticViolationDetectionTest(unittest.TestCase):
    """Non-vacuity: prove the guard ACTUALLY fires on upward imports.

    Without these tests the green real-file scans could pass simply because
    the helper parsed nothing, or the forbidden set was wrong.  Each case
    feeds a synthetic snippet and asserts the violation IS detected.
    """

    def test_detects_synthetic_upward_violation(self) -> None:
        # Two forms that a rogue developer might write to import an upward module.
        # Both must be caught.
        snippet_absolute = "from fulcra_coord.views import build_views\n"
        snippet_relative = "from . import cli\n"

        found_absolute = _imported_submodules(snippet_absolute)
        found_relative = _imported_submodules(snippet_relative)

        # views is in the forbidden set
        self.assertIn(
            "views",
            found_absolute,
            "_imported_submodules failed to extract 'views' from "
            f"'from fulcra_coord.views import build_views' — got {found_absolute!r}",
        )
        self.assertTrue(
            found_absolute & _FORBIDDEN_UPWARD_MODULES,
            f"'views' should be in _FORBIDDEN_UPWARD_MODULES but was not detected "
            f"as a violation.  Extracted: {found_absolute!r}",
        )

        # cli is in the forbidden set
        self.assertIn(
            "cli",
            found_relative,
            "_imported_submodules failed to extract 'cli' from "
            f"'from . import cli' — got {found_relative!r}",
        )
        self.assertTrue(
            found_relative & _FORBIDDEN_UPWARD_MODULES,
            f"'cli' should be in _FORBIDDEN_UPWARD_MODULES but was not detected "
            f"as a violation.  Extracted: {found_relative!r}",
        )

    def test_does_not_flag_legitimate_leaf_imports(self) -> None:
        """Regression: allowed imports must NOT appear in the forbidden set."""
        # These are the legitimate imports eventlog.py is allowed to make.
        snippet = (
            "from . import remote\n"
            "from fulcra_coord.timeutil import now_iso\n"
            "from .events import make_event\n"
            "import json\n"
            "from typing import Any\n"
        )
        found = _imported_submodules(snippet)

        # Allowed submodules must not be flagged as violations
        false_positives = found & _FORBIDDEN_UPWARD_MODULES
        self.assertEqual(
            false_positives,
            set(),
            f"Legitimate low-layer imports were incorrectly flagged as upward "
            f"violations: {sorted(false_positives)}.  "
            f"Extracted submodule set: {sorted(found)}.",
        )
        # And they must be correctly extracted
        self.assertIn("remote", found)
        self.assertIn("timeutil", found)
        self.assertIn("events", found)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
