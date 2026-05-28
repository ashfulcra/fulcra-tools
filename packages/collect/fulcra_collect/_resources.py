"""Frozen-aware resource location.

The daemon serves bundled data files (the web-ui SPA, the in-app docs).
In a dev checkout those live in the workspace tree; in a frozen app
(Briefcase/py2app/pip-installed wheel) they ride *inside* the
``fulcra_collect`` package as force-included package data under
``_bundled/`` (see the collect pyproject's
``[tool.hatch.build.targets.wheel.force-include]``). This module is the
single place that knows the difference, so the rest of the daemon never
has to care whether it's running frozen — and the bundled location is
relative to the installed package, so it works under any packager
rather than depending on a bundler-specific ``Resources/`` layout.
"""
from __future__ import annotations

import sys
from pathlib import Path

_PKG_DIR = Path(__file__).resolve().parent          # .../fulcra_collect
_WORKSPACE_ROOT = _PKG_DIR.parents[2]               # fulcra_collect → collect → packages → root
_BUNDLED = _PKG_DIR / "_bundled"                    # force-included data (frozen only)


def is_frozen() -> bool:
    """True inside a frozen bundle.

    py2app/PyInstaller set ``sys.frozen``. Briefcase doesn't, but it
    installs the app as a normal package tree where the force-included
    ``_bundled/`` data is present — so we also treat "the _bundled dir
    exists next to this module" as frozen. Either signal means: read
    bundled package data, not the dev workspace.
    """
    return bool(getattr(sys, "frozen", False)) or _BUNDLED.is_dir()


def frontend_dir() -> Path:
    """Directory holding the built web-ui SPA (index.html + static/).

    Frozen: the force-included copy inside the package. Dev: the live
    workspace copy, so local edits to the SPA show up without a rebuild.
    """
    if is_frozen():
        return _BUNDLED / "web-ui" / "dist"
    return _WORKSPACE_ROOT / "packages" / "web-ui" / "dist"


def docs_dir() -> Path:
    """Directory holding the in-app docs (e.g. how-do-i-get-my-data.md)."""
    if is_frozen():
        return _BUNDLED / "docs"
    return _WORKSPACE_ROOT / "docs"
