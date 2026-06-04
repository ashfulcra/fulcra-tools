"""fulcra-coord — shared agent coordination layer using Fulcra Files as a bus.

Remote root: configurable via FULCRA_COORD_REMOTE_ROOT (default: /coordination)
Cache root:  ${XDG_CACHE_HOME:-~/.cache}/fulcra-coord/
CLI:         FULCRA_CLI_COMMAND (default: fulcra-api or uv tool run fulcra-api)
"""

import os

# Single source of truth for the version (pyproject derives it via
# [tool.hatch.version]). Bump on every user-visible change so `--version` is
# accurate AND so `uv tool install --force` actually rebuilds — uv skips the
# rebuild when the version is unchanged, which is what silently froze older
# installs at an old subcommand set. SemVer-ish: minor for additive surfaces.
__version__ = "0.5.6"

SCHEMA_VERSION = "fulcra.coordination.task.v1"
DEFAULT_REMOTE_ROOT = "/coordination"


def remote_root() -> str:
    """Return the canonical Fulcra Files coordination root, overridable via env."""
    root = os.environ.get("FULCRA_COORD_REMOTE_ROOT", DEFAULT_REMOTE_ROOT).strip()
    return "/" + (root or DEFAULT_REMOTE_ROOT).strip("/")


def task_file_path(task_id: str) -> str:
    return f"{remote_root()}/tasks/{task_id}.json"
