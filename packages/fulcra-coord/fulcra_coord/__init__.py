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
__version__ = "0.15.15"

SCHEMA_VERSION = "fulcra.coordination.task.v1"
DEFAULT_REMOTE_ROOT = "/coordination"


def remote_root() -> str:
    """Return the canonical Fulcra Files coordination root, overridable via env."""
    root = os.environ.get("FULCRA_COORD_REMOTE_ROOT", DEFAULT_REMOTE_ROOT).strip()
    return "/" + (root or DEFAULT_REMOTE_ROOT).strip("/")


def task_file_path(task_id: str) -> str:
    return f"{remote_root()}/tasks/{task_id}.json"


def read_source() -> str:
    """Where reads reconstruct a task body from. 'file' (default) = the mutable
    tasks/<id>.json; 'events' = the event fold when complete, else file. Per-host
    env knob for the events read cutover; reversible by unsetting it.

    Default-off by design: this changes what a READ returns, so an operator must
    explicitly opt in (FULCRA_COORD_READ_SOURCE=events). Any unrecognised value
    degrades to 'file' so a typo can never silently flip the read path."""
    v = (os.environ.get("FULCRA_COORD_READ_SOURCE") or "file").strip().lower()
    return v if v in ("file", "events") else "file"


def env_float(name: str, default: float, override=None) -> float:
    """Resolve a float knob: explicit ``override`` > env var ``name`` > ``default``.

    The single source of truth for the "explicit arg > env > default, and a
    non-numeric env value falls back to the default" pattern that was copy-pasted
    across ~10 readers (staleness, presence grace, inbox age, the three retention
    windows, the two health thresholds, reroute minutes, accepted-stall hours).
    Centralizing it means a typo in any FULCRA_COORD_* value degrades to the
    default instead of crashing a read or reconcile tick — uniformly, in one place.

    Lives in the package root (alongside ``remote_root``/``task_file_path``) so
    views, cli, and remote can all import it with zero new coupling and no import
    cycle. ``override`` is coerced to float (matching the readers that did
    ``float(arg)``); a harmless normalization for the float-typed ones that
    returned it raw."""
    if override is not None:
        return float(override)
    raw = os.environ.get(name, "").strip()
    if raw:
        try:
            return float(raw)
        except ValueError:
            pass
    return float(default)


def env_int(name: str, default: int, override=None) -> int:
    """Resolve an int knob: explicit ``override`` > env var ``name`` > ``default``.

    The int sibling of ``env_float`` (read timeouts, the retention per-run cap).
    A non-numeric env value falls back to ``default`` rather than crashing — note
    this is a deliberate hardening for the read-timeout readers, which previously
    used a bare ``int(os.environ.get(...))`` that would raise on a garbage value.

    Parses the env value as an int directly (``int(raw)``), so a non-integer
    string like ``"2.5"`` falls back to the default. A caller that wants
    float-parse-then-truncate semantics (e.g. accept ``"3.9"`` as 3) must compose
    ``int(env_float(...))`` instead — that distinction is preserved at the call
    site, not papered over here."""
    if override is not None:
        return int(override)
    raw = os.environ.get(name, "").strip()
    if raw:
        try:
            return int(raw)
        except ValueError:
            pass
    return int(default)
