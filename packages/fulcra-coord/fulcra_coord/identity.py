"""Agent identity resolution + persistence for fulcra-coord.

Every bus operation needs to know "who am I" — the agent id that owns/directs/acks
tasks. Historically each command derived this ad-hoc (cli._derive_agent), which
diverged from the hook templates and made it impossible for a long-running session
to *declare* a stable id once and reuse it. This module centralizes resolution and
adds a persisted, global identity file so an agent can announce itself once.

Resolution order (resolve_agent), highest precedence first:
  1. explicit  — an `--agent`/`--from` value passed on the command line
  2. env       — $FULCRA_COORD_AGENT (the operator's session-scoped override)
  3. config    — the persisted identity file (the handshake: declared once, reused)
  4. derived   — claude-code:<hostname -s>:<cwd-basename> (environment best-effort)

The identity file is GLOBAL (not root-scoped like the cache): an agent's id is a
property of the session/host, not of which coordination root it happens to be
writing to. It lives under ${XDG_CONFIG_HOME:-~/.config}/fulcra-coord/identity.json
so tests can isolate it via XDG_CONFIG_HOME and it never collides with the cache.
"""

from __future__ import annotations

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


def identity_path() -> Path:
    return config_root() / "identity.json"


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


def read_identity() -> Optional[str]:
    """Return the persisted agent id, or None if no identity file / it's unreadable.

    Tolerant of a malformed file (corrupt JSON, missing key): a broken identity
    file must not wedge every command — we fall through to derived instead."""
    path = identity_path()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    agent = data.get("agent")
    return agent if isinstance(agent, str) and agent.strip() else None


def set_identity(agent_id: str) -> Path:
    """Persist `agent_id` as this host's declared identity (the handshake). Global,
    so it survives across coordination roots and sessions until cleared."""
    path = identity_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"agent": agent_id}, indent=2))
    return path


def clear_identity() -> bool:
    """Remove the persisted identity. Returns True if a file was removed. After
    clearing, resolve_agent falls back to env/derived."""
    path = identity_path()
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
