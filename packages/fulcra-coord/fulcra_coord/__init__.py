"""fulcra-coord — shared agent coordination layer using Fulcra Files as a bus.

Remote root: configurable via FULCRA_COORD_REMOTE_ROOT (default: /coordination)
Cache root:  ${XDG_CACHE_HOME:-~/.cache}/fulcra-coord/
CLI:         FULCRA_CLI_COMMAND (default: fulcra-api or uv tool run fulcra-api)
"""

import os

__version__ = "0.1.0"

SCHEMA_VERSION = "fulcra.coordination.task.v1"
DEFAULT_REMOTE_ROOT = "/coordination"


def remote_root() -> str:
    """Return the canonical Fulcra Files coordination root, overridable via env."""
    root = os.environ.get("FULCRA_COORD_REMOTE_ROOT", DEFAULT_REMOTE_ROOT).strip()
    return "/" + (root or DEFAULT_REMOTE_ROOT).strip("/")


def task_file_path(task_id: str) -> str:
    return f"{remote_root()}/tasks/{task_id}.json"
