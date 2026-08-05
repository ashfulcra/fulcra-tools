#!/usr/bin/env python3
"""Single source of truth for what the frozen macOS bundle must contain.

The release build has to agree with itself in three places:

  1. ``[tool.briefcase.app.fulcra-menubar] requires`` — what Briefcase installs;
  2. the wheel-build loop in ``build_macos_app.sh`` — monorepo packages aren't on
     PyPI, so each needs a local wheel in ``wheelhouse/`` or pip can't resolve it;
  3. the post-build presence guard — Briefcase can exit 0 with an EMPTY
     ``app_packages``, so the build must prove each package actually landed.

Those were three hand-maintained lists, and they drifted: ``fulcra-purpleair``
was added to (1) and to neither (2) nor (3), so the release build still could
not install it (found in review of PR #455). This module derives (2) and (3)
from (1) so the drift class is gone rather than patched once.

Distribution name -> import name is NOT a mechanical underscore substitution
(``fulcra-media-helpers`` ships ``fulcra_media``; ``fulcra-csv-importer`` ships
``fulcra_csv``), so the import name is read from each package's own
``[tool.hatch.build.targets.wheel] packages`` rather than guessed.

Usage (consumed by build_macos_app.sh):
    python3 bundle_manifest.py --dists     # workspace dist names, for `uv build`
    python3 bundle_manifest.py --imports   # import names, for the presence guard
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import tomllib

REPO = Path(__file__).resolve().parents[3]
MENUBAR_PYPROJECT = REPO / "packages" / "menubar" / "pyproject.toml"

#: Requirement specifiers are stripped down to the bare distribution name.
_SPEC_CHARS = "<>=!~[; "


def _dist_name(requirement: str) -> str:
    name = requirement.strip()
    for ch in _SPEC_CHARS:
        name = name.split(ch, 1)[0]
    return name.strip()


def briefcase_requires() -> list[str]:
    """Every requirement Briefcase installs into the macOS app."""
    data = tomllib.loads(MENUBAR_PYPROJECT.read_text())
    reqs = data["tool"]["briefcase"]["app"]["fulcra-menubar"]["requires"]
    return [_dist_name(r) for r in reqs]


def _workspace_packages() -> dict[str, str]:
    """Map workspace distribution name -> import (wheel) package name."""
    found: dict[str, str] = {}
    for pyproject in sorted((REPO / "packages").glob("*/pyproject.toml")):
        data = tomllib.loads(pyproject.read_text())
        dist = (data.get("project") or {}).get("name")
        if not dist:
            continue
        wheel = (
            data.get("tool", {})
            .get("hatch", {})
            .get("build", {})
            .get("targets", {})
            .get("wheel", {})
            .get("packages")
        )
        # Fall back to the directory-derived module only when a package doesn't
        # declare its wheel packages; every current workspace member does.
        found[dist] = wheel[0] if wheel else dist.replace("-", "_")
    return found


def bundle_workspace_packages() -> list[tuple[str, str]]:
    """The (dist, import) pairs the bundle needs local wheels + proof for.

    A requirement counts when it is BOTH listed in Briefcase ``requires`` and a
    package in this monorepo — i.e. exactly the set that is not on PyPI.
    """
    workspace = _workspace_packages()
    return [(d, workspace[d]) for d in briefcase_requires() if d in workspace]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dists", action="store_true", help="workspace dist names")
    g.add_argument("--imports", action="store_true", help="bundle import names")
    args = ap.parse_args()

    pairs = bundle_workspace_packages()
    if not pairs:
        print("bundle_manifest: no workspace packages resolved", file=sys.stderr)
        return 1
    for dist, imp in pairs:
        print(dist if args.dists else imp)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
