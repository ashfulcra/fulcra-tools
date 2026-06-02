"""Resolve how the materialized hooks should invoke the fulcra-coord CLI.

WHY this exists (Gap 1): every materialized hook used to call the bare token
``fulcra-coord``. That only works when something put an entry point on PATH —
``pip install`` does, but ``uv tool`` and source/editable installs frequently do
NOT. When the bare name is unresolvable the hooks fail-safe to a silent no-op:
the installer reports success while the integration does nothing. We resolve a
concretely-callable invocation *at install time* and bake it into the scripts so
the hooks work regardless of how the package was installed.

WHY an argv list, not a string (C1): the resolved invocation can legitimately
contain a space — ``sys.executable`` on macOS is commonly
``~/Library/Application Support/.../python`` — and every consumer used to
word-split the resolved STRING unquoted, so a path with a space shattered into
broken argv tokens and the hook silently no-op'd (the exact failure Gap 1 was
meant to fix). The single source of truth is therefore :func:`resolve_cli_argv`,
which returns an explicit ``list[str]`` argv. Each surface materializes that
argv in a form that cannot word-split: a bash array, a plist ``<array>``, a
``shlex.quote``-joined cron line, or a JSON array literal for TypeScript.

The resolution order is deliberate:

  1. ``shutil.which("fulcra-coord")`` — if the entry point is genuinely on PATH,
     prefer its absolute path as a single-element argv. Absolute (not bare) so a
     hook that runs with a reduced PATH (launchd, cron, a stripped shell) still
     finds it.
  2. ``[sys.executable, "-m", "fulcra_coord"]`` — the universal fallback. The
     package is, by definition, importable in the interpreter running the
     installer, so ``python -m fulcra_coord`` is guaranteed to work even when no
     console-script was placed on PATH.

The placeholder ``__FULCRA_COORD_ARGV__`` lives literally in the committed hook
templates (and the parity copies under adapters/); the materializer substitutes
it with :func:`materialize_argv` (a shell-quoted token list for the bash array)
only when writing the scripts to disk, so parity stays green while installed
files get a real, space-safe command. :func:`resolve_cli_command` is retained as
the ``shlex.join`` of the argv purely for human-readable display (plan output,
warnings) — it is NOT consumed for execution anywhere.
"""
from __future__ import annotations

import shlex
import shutil
import sys

# The literal token the bash hook templates carry inside the FULCRA_COORD array.
# The materializer replaces it with materialize_argv(resolve_cli_argv()) before
# writing a script to disk. Kept distinct from the legacy single-string name so
# the array form is unambiguous in the templates.
PLACEHOLDER_ARGV = "__FULCRA_COORD_ARGV__"


def resolve_cli_argv() -> list[str]:
    """Return a concretely-callable fulcra-coord invocation as an explicit argv.

    Prefers an absolute on-PATH entry point (``[which_path]``); otherwise falls
    back to ``[sys.executable, "-m", "fulcra_coord"]``, which always works
    wherever the installer ran. Never returns the bare name ``fulcra-coord``
    (that is exactly the failure mode Gap 1 fixes). Returning a list — rather
    than a string the caller must split — is what makes a spaced ``argv[0]``
    (e.g. an interpreter under "Application Support") survive intact (C1).
    """
    on_path = shutil.which("fulcra-coord")
    if on_path:
        return [on_path]
    return [sys.executable, "-m", "fulcra_coord"]


def resolve_cli_command() -> str:
    """Human-readable, shell-quoted rendering of :func:`resolve_cli_argv`.

    Retained ONLY for display (installer plan output, operator-facing warnings).
    It is shell-safe to print, but no consumer parses it back for execution —
    every executing surface materializes the argv list directly so it cannot be
    word-split (C1).
    """
    return shlex.join(resolve_cli_argv())


def materialize_argv(argv: list[str]) -> str:
    """Render an argv as space-separated shell-quoted tokens for a bash array.

    Substituted into ``FULCRA_COORD=(__FULCRA_COORD_ARGV__)`` so the array's
    elements are the exact argv tokens — ``shlex.quote`` keeps a token with a
    space as one element rather than two. Callers expand it as
    ``"${FULCRA_COORD[@]}"`` so no word-splitting ever occurs.
    """
    return " ".join(shlex.quote(t) for t in argv)


def used_python_m_fallback() -> bool:
    """True when no on-PATH entry point exists, so resolve_cli_argv falls back
    to ``python -m``. Lets the installer print a warning that PATH wiring is
    missing (the install still works, but the operator may want to fix PATH)."""
    return shutil.which("fulcra-coord") is None
