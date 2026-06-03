"""Agent identity resolution + persistence for fulcra-coord.

Every bus operation needs to know "who am I" — the agent id that owns/directs/acks
tasks. Historically each command derived this ad-hoc (cli._derive_agent), which
diverged from the hook templates and made it impossible for a long-running session
to *declare* a stable id once and reuse it. This module centralizes resolution and
adds a persisted, global identity file so an agent can announce itself once.

Resolution order (resolve_agent), highest precedence first:
  1. explicit  — an `--agent`/`--from` value passed on the command line
  2. env       — $FULCRA_COORD_AGENT (the operator's session-scoped override)
  3. config    — the persisted identity (the handshake: declared once, reused),
                 scoped PER WORKING DIRECTORY
  4. derived   — claude-code:<hostname -s>:<cwd-basename> (environment best-effort)

The persisted identity is scoped PER CWD, not globally. The original global file
was the source of a clobber bug: every same-machine session's `identity set`
overwrote the others', so sibling sessions in different repos could not each
declare a stable id. Entries now live under
${XDG_CONFIG_HOME:-~/.config}/fulcra-coord/identities/<cwd-hash>.json (keyed by
the absolute cwd). A legacy global identity.json is still read as a fallback so
machines configured before the split keep resolving. The config dir is NOT
root-scoped (an agent's id is a property of the session/repo, not of the
coordination root) and lives under XDG_CONFIG_HOME so tests can isolate it and it
never collides with the cache.
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
from pathlib import Path
from typing import Optional


def config_root() -> Path:
    """Global config dir, mirroring cache.cache_root()'s XDG handling but for
    config (identity) rather than cache. Deliberately NOT root-scoped: an agent's
    identity is independent of which coordination root it writes to."""
    xdg = os.environ.get("XDG_CONFIG_HOME", "")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "fulcra-coord"


def _legacy_identity_path() -> Path:
    """The pre-per-cwd GLOBAL identity file.

    Historically the persisted identity was one global file. That was the source
    of the clobber bug: every same-machine session's ``identity set`` overwrote
    the others' (e.g. a ``:vercel`` session then a ``:birdnet-pi`` session
    clobbered the ``:fulcra-coord`` one). It is retained ONLY as a read-time
    fallback so existing setups keep resolving until they re-set per-cwd."""
    return config_root() / "identity.json"


def _identities_dir() -> Path:
    """Directory of per-cwd identity files (the clobber fix)."""
    return config_root() / "identities"


def _cwd_hash(cwd: Optional[str] = None) -> str:
    """Stable filesystem-safe key for the current working directory.

    The persisted identity is scoped to the ABSOLUTE cwd so two sibling sessions
    (different repos on the same machine) hold distinct identities and never
    clobber each other. Hashing the abspath keeps the filename short and portable
    regardless of how deep or oddly-charactered the path is."""
    abspath = os.path.abspath(cwd if cwd is not None else os.getcwd())
    return hashlib.sha1(abspath.encode()).hexdigest()[:16]


def identity_path(cwd: Optional[str] = None) -> Path:
    """Per-cwd identity file path. Each working directory gets its own entry
    under ``identities/<cwd-hash>.json`` so identities are scoped to the repo a
    session runs in, not shared globally across every same-machine session."""
    return _identities_dir() / f"{_cwd_hash(cwd)}.json"


def derived_agent() -> str:
    """The fallback agent id when nothing is declared: the same
    ``claude-code:<host>:<cwd-basename>`` shape the SessionStart hook computes, so
    the CLI and the hook agree on "who am I" without extra config. Environment
    best-effort — never raises."""
    try:
        host = socket.gethostname().split(".")[0]
    except Exception:
        host = "host"
    repo = os.path.basename(os.getcwd()) or "repo"
    return f"claude-code:{host}:{repo}"


def _read_identity_file(path: Path) -> Optional[str]:
    """Read an ``{"agent": ...}`` identity file, tolerating absence/corruption.

    A broken identity file must not wedge every command — return None so the
    caller falls through to the next resolution source."""
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    agent = data.get("agent")
    return agent if isinstance(agent, str) and agent.strip() else None


def read_identity(cwd: Optional[str] = None) -> Optional[str]:
    """Return the persisted agent id for the current cwd, or None.

    Reads the PER-CWD entry first; if there is none, falls back to the LEGACY
    global ``identity.json`` so machines configured before the per-cwd split keep
    resolving (the migration path). A per-cwd entry always wins over the legacy
    global file. Tolerant of malformed files at either layer."""
    per_cwd = _read_identity_file(identity_path(cwd))
    if per_cwd:
        return per_cwd
    return _read_identity_file(_legacy_identity_path())


def set_identity(agent_id: str, cwd: Optional[str] = None) -> Path:
    """Persist `agent_id` as the declared identity for the CURRENT cwd.

    Per-cwd (the clobber fix): writing the identity in one repo no longer
    overwrites a sibling session's identity in another repo. Survives across
    coordination roots and sessions until cleared."""
    path = identity_path(cwd)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"agent": agent_id}, indent=2))
    return path


def clear_identity(cwd: Optional[str] = None) -> bool:
    """Remove the persisted identity for the CURRENT cwd. Returns True if a file
    was removed. Per-cwd: clearing one repo's identity leaves other repos' intact.
    After clearing, resolve_agent falls back to the legacy global / env / derived."""
    path = identity_path(cwd)
    if path.exists():
        path.unlink()
        return True
    return False


# ---------------------------------------------------------------------------
# Human handle (situational awareness)
# ---------------------------------------------------------------------------
#
# The human operator is a first-class, addressable identity on the bus — the one
# everything is "blocked on ME" against. It defaults to the neutral handle
# ``human`` so the public repo carries no personal name, and is personalizable
# (this operator runs ``fulcra-coord human set ash``). Resolution mirrors
# ``resolve_agent``: env override first (a session-scoped pin), then a persisted
# config file (declared once, reused), then the neutral default. It is GLOBAL,
# not root-scoped: who the human is is a property of the machine, not of which
# coordination root a command happens to target.

#: Neutral default human handle. Personal handles are opt-in via env/config so
#: the shipped default leaks no name.
DEFAULT_HUMAN = "human"


def human_path() -> Path:
    return config_root() / "human"


def read_human() -> Optional[str]:
    """Return the persisted human handle, or None if unset/unreadable.

    Stored as a single trimmed line (not JSON): the human handle is one short
    token, so a plain file is simpler than a JSON object and matches the spec's
    ``${XDG_CONFIG_HOME:-~/.config}/fulcra-coord/human`` path. Tolerant of a
    missing/empty/unreadable file — a broken handle must never wedge a command,
    so we fall through to the default."""
    path = human_path()
    if not path.exists():
        return None
    try:
        raw = path.read_text().strip()
    except OSError:
        return None
    return raw or None


def set_human(handle: str) -> Path:
    """Persist `handle` as this machine's human operator handle. Global, so it
    survives across coordination roots and sessions until cleared."""
    path = human_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(handle.strip() + "\n")
    return path


def clear_human() -> bool:
    """Remove the persisted human handle. Returns True if a file was removed.
    After clearing, resolve_human falls back to env/default."""
    path = human_path()
    if path.exists():
        path.unlink()
        return True
    return False


def resolve_human_source() -> tuple[str, str]:
    """Resolve the human handle AND report its source.

    Returns (handle, source) where source is "env" | "config" | "default".
    Order: ``$FULCRA_COORD_HUMAN`` > persisted ``human`` file > ``DEFAULT_HUMAN``.
    Surfacing the source lets the ``human`` command explain *why* it resolved
    the way it did (mirrors ``resolve_agent_source``)."""
    env = os.environ.get("FULCRA_COORD_HUMAN", "").strip()
    if env:
        return env, "env"
    persisted = read_human()
    if persisted:
        return persisted, "config"
    return DEFAULT_HUMAN, "default"


def resolve_human() -> str:
    """The single "who is the human" entry point. Order: env > config > default
    (``human``). Used by needs-me, block --on-user, the SessionStart banner, and
    the listener so they all agree on whose plate "blocked on me" lands on."""
    return resolve_human_source()[0]


def resolve_agent_source(explicit: Optional[str] = None) -> tuple[str, str]:
    """Resolve the agent id AND report where it came from.

    Returns (agent_id, source) where source is one of
    "explicit" | "env" | "config" | "derived". The `identity` command surfaces the
    source so an operator can see *why* they are who they are (e.g. an env override
    silently shadowing a persisted id is a common confusion).
    """
    if explicit is not None and explicit.strip():
        return explicit, "explicit"
    env = os.environ.get("FULCRA_COORD_AGENT", "").strip()
    if env:
        return env, "env"
    persisted = read_identity()
    if persisted:
        return persisted, "config"
    return derived_agent(), "derived"


def resolve_agent(explicit: Optional[str] = None) -> str:
    """The single "who am I" entry point used everywhere an agent id is needed.

    Order: explicit `--agent` > $FULCRA_COORD_AGENT > persisted identity > derived.
    Replaces the ad-hoc cli._derive_agent so the CLI, listener, and identity
    command all agree on identity resolution.
    """
    return resolve_agent_source(explicit)[0]
