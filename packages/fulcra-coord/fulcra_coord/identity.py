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
the realpath of the cwd). The config dir is NOT root-scoped (an agent's id is a
property of the session/repo, not of the coordination root) and lives under
XDG_CONFIG_HOME so tests can isolate it and it never collides with the cache.

The legacy global identity.json is DELIBERATELY NOT in the resolution path
(I-1). It was a silent fallback for un-set cwds, but that meant a single
stale/clobbered global (e.g. another tool's ``codex:host:main``) leaked in as the
resolved identity for EVERY repo with no per-cwd entry — masquerading as a
declared "config" identity. Removing it makes the safe ``derived`` id the default
for an un-set cwd. The file is still read via ``read_legacy_identity`` ONLY to
surface a migration hint (``identity show``) and to power ``identity migrate``,
never to resolve.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import subprocess
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
    clobbered the ``:fulcra-coord`` one). It is NO LONGER part of resolution
    (I-1) — kept only so ``read_legacy_identity`` can surface a migration hint."""
    return config_root() / "identity.json"


def _identities_dir() -> Path:
    """Directory of per-cwd identity files (the clobber fix)."""
    return config_root() / "identities"


def _cwd_hash(cwd: Optional[str] = None) -> str:
    """Stable filesystem-safe key for the current working directory.

    The persisted identity is scoped to the REALPATH of the cwd so two sibling
    sessions (different repos on the same machine) hold distinct identities and
    never clobber each other. Hashing keeps the filename short and portable
    regardless of how deep or oddly-charactered the path is.

    M-3: realpath (not abspath) so a symlinked path and its real target resolve
    to the SAME entry — a session entered via a symlink (e.g. /tmp on macOS is a
    symlink to /private/tmp, or a worktree symlinked into place) must see the
    identity it declared under the canonical path, not a phantom separate one."""
    real = os.path.realpath(cwd if cwd is not None else os.getcwd())
    return hashlib.sha1(real.encode()).hexdigest()[:16]


def identity_path(cwd: Optional[str] = None) -> Path:
    """Per-cwd identity file path. Each working directory gets its own entry
    under ``identities/<cwd-hash>.json`` so identities are scoped to the repo a
    session runs in, not shared globally across every same-machine session."""
    return _identities_dir() / f"{_cwd_hash(cwd)}.json"


#: Hostnames that are NOT a stable machine identity — the generic mDNS/DHCP
#: fallbacks ``socket.gethostname()`` returns when the system name is unset.
#: Deriving an agent id from one of these silently mints a DIFFERENT identity
#: than the same machine produces under a real name, spawning phantom agents.
_GENERIC_HOSTS = frozenset({"", "mac", "localhost", "local", "localdomain", "host"})


def _stable_hostname() -> str:
    """A best-effort STABLE short hostname for identity derivation.

    ``socket.gethostname()`` is unreliable on macOS: when the system ``HostName``
    is unset it returns the transient mDNS/DHCP fallback ``Mac.localdomain`` ->
    the generic ``Mac``. Deriving an agent id from that diverges from the
    identity the same machine produces when the network/HostName differs, so a
    single session running ``fulcra-coord`` from several cwds mints multiple
    phantom ``claude-code:Mac:<dir>`` agents — they then self-contend an
    exclusive role's lease and split inbox/wake delivery (2026-06-19 incident).

    When ``gethostname`` yields a generic value, fall back to a stable source
    (macOS ``scutil --get LocalHostName``/``ComputerName``) so one machine
    derives ONE consistent host. Best-effort: never raises; on non-macOS or any
    failure it returns whatever ``gethostname`` gave (or ``"host"``)."""
    try:
        host = socket.gethostname().split(".")[0].strip()
    except Exception:
        host = ""
    if host and host.lower() not in _GENERIC_HOSTS:
        return host
    for key in ("LocalHostName", "ComputerName"):
        try:
            out = subprocess.run(
                ["scutil", "--get", key], capture_output=True, text=True, timeout=5)
            if out.returncode != 0:
                continue
            # ComputerName may contain spaces/apostrophes ("Jane's MacBook Pro");
            # normalize to the hostname-safe charset the rest of the id uses.
            name = re.sub(r"[^A-Za-z0-9-]+", "-", out.stdout.strip().split(".")[0]).strip("-")
            if name and name.lower() not in _GENERIC_HOSTS:
                return name
        except Exception:
            continue
    return host or "host"


def derived_agent() -> str:
    """The fallback agent id when nothing is declared: the same
    ``claude-code:<host>:<cwd-basename>`` shape the SessionStart hook computes, so
    the CLI and the hook agree on "who am I" without extra config. Uses a STABLE
    hostname (see ``_stable_hostname``) so a junk ``gethostname`` does not mint a
    phantom ``Mac:<dir>`` identity. Environment best-effort — never raises."""
    host = _stable_hostname()
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
    """Return the PER-CWD persisted agent id for the current cwd, or None.

    I-1: reads ONLY the per-cwd entry. The legacy global ``identity.json`` is no
    longer a fallback — a stale global must not silently resolve as the identity
    for an un-set cwd (that masked a clobbered global as a declared id across
    every repo). Tolerant of a malformed per-cwd file."""
    return _read_identity_file(identity_path(cwd))


def read_legacy_identity() -> Optional[str]:
    """Return the legacy GLOBAL identity, or None — for HINTING ONLY.

    The legacy global file is no longer part of resolution (see I-1 / module
    docstring). This accessor exists so ``identity show`` can tell an operator a
    stale global still exists ("…no longer used automatically; run identity set")
    and so ``identity migrate`` can copy it into the per-cwd entry. It must NEVER
    be wired back into ``read_identity`` / ``resolve_agent``."""
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
    After clearing, resolve_agent falls back to env / derived (NOT the legacy
    global, which is no longer in the resolution path — see I-1)."""
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
