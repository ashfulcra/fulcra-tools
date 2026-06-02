"""OpenClaw Track B — Plugin-SDK plugin installer (source materialization).

Track A (``openclaw.py``) installs file-based automation hooks by dropping files
into ``~/.openclaw/hooks/``. Track B is the higher-fidelity upgrade: a real
OpenClaw **Plugin-SDK plugin** (``adapters/openclaw/plugin/``) that registers the
in-process ``session_start`` / ``before_compaction`` / ``session_end`` lifecycle
hooks. Its unique differentiator is **deterministic per-session start/end**:
there is no file-based ``session:start`` automation event and session-end is
plugin-only, so true per-session ``session_start`` / ``session_end`` requires the
plugin. (``before_compaction`` is also exposed file-based as
``session:compact:before``, which Track A's compaction hook uses; the plugin
bundles the underscore-form ``before_compaction`` for single-plugin installs.)

WHY THIS IS ONLY A "MATERIALIZE" STEP (not a full install): a Plugin-SDK plugin
must be built (``tsc`` → ``dist/index.js``) and registered (``openclaw plugins
install .``). Both need ``npm``/the ``openclaw`` CLI, which ``fulcra-coord``
cannot and should not invoke for the user. So this installer lays down the exact
plugin source tree and prints the build+register steps; the plugin's ``README.md``
is the authoritative walkthrough.

The plugin sources are the single source of truth under
``adapters/openclaw/plugin/`` and are shipped inside the wheel (via the pyproject
``force-include`` to ``fulcra_coord/_data/openclaw_plugin``). ``_plugin_src_root``
resolves whichever location exists, so this works both from a source checkout
(tests) and from an installed wheel.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

# Files that make up the plugin source tree we copy verbatim. Listed explicitly
# (rather than "copy everything") so we never sweep in build artifacts like
# node_modules/ or dist/ that a prior local build may have left behind.
_PLUGIN_FILES = (
    "package.json",
    "openclaw.plugin.json",
    "tsconfig.json",
    "README.md",
    "src/index.ts",
    # Ambient SDK shim so `tsc` compiles without the real `openclaw` peer
    # installed (the peer is omitted on purpose — see `.npmrc` below).
    "src/openclaw-sdk.d.ts",
    # `.npmrc` carries `omit=peer`: keeps `npm install` from pulling the
    # `openclaw` runtime into node_modules, which `openclaw plugins install .`
    # would otherwise choke on (arc live finding, TASK-...-install-f0e6511a).
    ".npmrc",
    # Backstop so a stray node_modules can never be staged on install/pack.
    ".npmignore",
)

# Default install location for the materialized (unbuilt) plugin sources.
_DEFAULT_PLUGIN_SUBDIR = ("plugins", "fulcra-coord")


def _plugin_src_root() -> Path:
    """Locate the canonical plugin source tree.

    Precedence:
      1. The wheel-packaged copy at ``fulcra_coord/_data/openclaw_plugin``.
      2. The repo source-checkout copy at ``<repo>/adapters/openclaw/plugin``
         (used by tests and ``pip install -e .`` development installs).

    Returns the first that exists. Raising here would be a packaging bug, so we
    fall through to the source-checkout path as the last resort even if neither
    ``is_dir()`` (the caller surfaces a clean error if the tree is truly absent).
    """
    packaged = Path(__file__).resolve().parent / "_data" / "openclaw_plugin"
    if packaged.is_dir():
        return packaged
    repo = Path(__file__).resolve().parent.parent / "adapters" / "openclaw" / "plugin"
    return repo


def _default_plugin_dir() -> Path:
    """Default target dir for the materialized plugin, overridable via env.

    ``FULCRA_OPENCLAW_PLUGIN_DIR`` lets tests (and unusual installs) point the
    installer at an arbitrary tree without touching the real ``~/.openclaw/``.
    """
    env = os.environ.get("FULCRA_OPENCLAW_PLUGIN_DIR", "").strip()
    if env:
        return Path(env)
    return Path.home().joinpath(".openclaw", *_DEFAULT_PLUGIN_SUBDIR)


def install_openclaw_plugin(*, dry_run: bool = False, uninstall: bool = False,
                            plugin_dir: "str | Path | None" = None) -> dict[str, Any]:
    """Materialize (or remove) the Track B plugin source tree.

    Copies the plugin sources from ``_plugin_src_root`` into ``plugin_dir``
    (default ``~/.openclaw/plugins/fulcra-coord/`` or
    ``$FULCRA_OPENCLAW_PLUGIN_DIR``). This does NOT build or register the plugin
    — that is a manual ``npm install && npm run build && openclaw plugins
    install .`` step the CLI can't perform. The returned plan includes the
    ``build_steps`` to print so the user can finish the install.

    ``dry_run`` writes nothing but reports the plan; ``uninstall`` removes the
    whole materialized dir (it is wholly ours, so removal is unambiguous).
    """
    target = Path(plugin_dir) if plugin_dir is not None else _default_plugin_dir()
    src_root = _plugin_src_root()

    plan: dict[str, Any] = {
        "plugin_dir": str(target),
        "src_root": str(src_root),
        "uninstall": uninstall,
        "dry_run": dry_run,
        "writes": [],
        "removes": [],
        # The manual finish-the-install steps (the CLI can't run npm/tsc).
        "build_steps": [
            f"cd {target}",
            "npm install",
            "npm run build",
            "openclaw plugins install .",
        ],
    }

    if uninstall:
        if target.exists():
            plan["removes"].append(str(target))
        if not dry_run:
            shutil.rmtree(target, ignore_errors=True)
        return plan

    # Plan the per-file writes (so --dry-run can report them precisely).
    for rel in _PLUGIN_FILES:
        plan["writes"].append(str(target / rel))

    if dry_run:
        return plan

    # Materialize: copy each known source file, creating parent dirs as needed.
    for rel in _PLUGIN_FILES:
        src = src_root / rel
        dst = target / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        # copyfile (not copy2) — we don't care to preserve src mode/mtime, and
        # this keeps behavior identical whether src came from the wheel or repo.
        shutil.copyfile(src, dst)

    return plan
