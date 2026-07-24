"""Zero-model W6 wake-adapter legs.

The queued-file leg is host-local and intentionally carries only a keyed
``check the bus`` nudge.  SessionStart consumes it; it never interprets event
content.  The Routine leg records alignment under the router-owned namespace;
it explicitly does *not* create or target a cloud session.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from . import config, router
from .transport import run_bounded

_KEY = re.compile(r"^[A-Za-z0-9_./:-]{1,512}$")


def _slug(value: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9_.-]", "-", value).strip("-") or "identity"
    return f"{stem}-{hashlib.sha256(value.encode()).hexdigest()[:8]}"


def default_wake_root() -> Path:
    configured = os.environ.get("COORD_WAKE_DIR")
    if configured:
        return Path(configured).expanduser()
    state = os.environ.get("XDG_STATE_HOME")
    base = Path(state).expanduser() if state else Path.home() / ".local" / "state"
    return base / "coord-engine" / "wakes"


def wake_agent_dir(team: str, agent: str, *, root: Optional[Path] = None) -> Path:
    return (root or default_wake_root()) / _slug(team) / _slug(agent)


def _invocation(inv: dict[str, Any], adapter: str) -> tuple[str, str]:
    if inv.get("adapter") != adapter:
        raise ValueError(f"expected {adapter!r} invocation")
    agent, key = inv.get("agent"), inv.get("idempotency_key")
    if not isinstance(agent, str) or not agent:
        raise ValueError("wake invocation has no agent")
    if not isinstance(key, str) or not key:
        raise ValueError("wake invocation has no idempotency_key")
    if not _KEY.fullmatch(key):
        raise ValueError("wake invocation has an unsafe idempotency_key")
    return agent, key


def queue_wake_file(
    team: str,
    inv: dict[str, Any],
    *,
    root: Optional[Path] = None,
    now: Optional[datetime] = None,
) -> Path:
    """Atomically write one idempotency-keyed host-local nudge.

    Duplicate at-least-once deliveries self-overwrite the same filename.  Raw
    event prose is deliberately not persisted: the consumer only needs the key
    to know that an authoritative bus check is due.
    """
    agent, key = _invocation(inv, "queued-wake-file")
    directory = wake_agent_dir(team, agent, root=root)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{hashlib.sha256(key.encode()).hexdigest()}.json"
    stamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    doc = {
        "agent": agent,
        "key": key,
        "queued_at": stamp.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "team": team,
        "type": "coord-queued-wake",
    }
    fd, temporary = tempfile.mkstemp(prefix=".wake-", dir=directory)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(doc, handle, sort_keys=True)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return path


def consume_wake_files(
    team: str,
    agent: str,
    *,
    root: Optional[Path] = None,
    limit: int = 64,
) -> dict[str, Any]:
    """Consume valid exact-identity nudges once and return bounded hook context.

    Invalid/cross-identity files remain on disk and are reported, never silently
    discarded.  The stored event message is not replayed into model context.
    """
    directory = wake_agent_dir(team, agent, root=root)
    keys: list[str] = []
    errors: list[str] = []
    if directory.exists():
        for path in sorted(directory.glob("*.json"))[:limit]:
            # Claim the exact inode before reading it. A producer may publish a
            # duplicate for the same key with os.replace() while SessionStart is
            # consuming; renaming first means that later canonical pathname is
            # never unlinked by this consumer.
            claim = directory / (
                f".{path.name}.claim-{os.getpid()}-{secrets.token_hex(4)}")
            try:
                os.replace(path, claim)
            except FileNotFoundError:
                # Another SessionStart claimed it first.
                continue
            try:
                doc = json.loads(claim.read_text())
            except (OSError, ValueError):
                _quarantine_invalid_claim(claim, path)
                errors.append(f"{path.name}: invalid JSON")
                continue
            if not isinstance(doc, dict) or doc.get("type") != "coord-queued-wake":
                _quarantine_invalid_claim(claim, path)
                errors.append(f"{path.name}: invalid wake shape")
                continue
            if doc.get("team") != team or doc.get("agent") != agent:
                _quarantine_invalid_claim(claim, path)
                errors.append(f"{path.name}: identity mismatch")
                continue
            key = doc.get("key")
            if not isinstance(key, str) or not _KEY.fullmatch(key):
                _quarantine_invalid_claim(claim, path)
                errors.append(f"{path.name}: invalid key")
                continue
            keys.append(key)
            claim.unlink()
    context = ""
    if keys:
        context = (
            f"coord wake nudge: {len(keys)} queued wake(s) were consumed. "
            "Check the coordination bus with the authoritative briefing; "
            "no action is encoded in these wake files. Keys: "
            + ", ".join(keys[:8])
        )
    if errors:
        degraded = (
            f"coord queued-wake degraded: {len(errors)} unreadable wake file(s) "
            "remain for operator inspection."
        )
        context = f"{context}\n{degraded}".strip()
    return {"context": context[:2000], "count": len(keys), "errors": errors,
            "keys": keys}


def _quarantine_invalid_claim(claim: Path, canonical: Path) -> None:
    """Keep malformed input visible and never touch the producer's pathname."""
    quarantine = canonical.parent / ".quarantine"
    quarantine.mkdir(exist_ok=True)
    os.replace(
        claim,
        quarantine / f"{canonical.name}.invalid-{secrets.token_hex(8)}",
    )


def align_routine(
    transport: Any,
    team: str,
    inv: dict[str, Any],
    *,
    eligible_at: str,
    aligned_at: Optional[str] = None,
) -> str:
    """Record work for an agent's *existing, self-armed* cloud Routine.

    This is alignment/bookkeeping, not an exact-session wake. Its evidence is
    the standard router delivery record, extended with Routine-specific fields.
    """
    agent, key = _invocation(inv, "routine-align")
    aligned_at = aligned_at or datetime.now(timezone.utc).isoformat(
        timespec="seconds").replace("+00:00", "Z")
    suffix = f":{agent}"
    if not key.endswith(suffix) or len(key) == len(suffix):
        raise ValueError(
            "routine-align idempotency_key is not <source_shard>:<agent>")
    entry = {
        "adapter": "routine-align",
        "agent": agent,
        "executor": router.DECISION_PLANE,
        "source_shard": key[:-len(suffix)],
    }
    record = {
        **router.delivery_record(entry, delivered_at=aligned_at),
        "eligible_at": eligible_at,
        "mode": "self-armed-routine",
        "no_session_created": True,
    }
    path = (
        f"{router.router_prefix(team)}delivered/"
        f"{router.record_filename(key)}"
    )
    if transport.write(path, json.dumps(record, sort_keys=True) + "\n") is False:
        raise RuntimeError(f"routine alignment write failed: {path}")
    return path


# --- host-local SCRIPT adapters (W5.5 invoker seam) --------------------------
#
# A host-local adapter is a small, host-provisioned SHELL SCRIPT invoked by the
# thin host executor through `cli._default_host_adapter_invoke`. Two rules make
# that safe:
#
#   1. NUDGE-ONLY (plan §2 content rule): the only things that cross the process
#      boundary are the agent id, the idempotency key and `NUDGE_REASON` — a
#      module CONSTANT. No per-event command, shell, URL or payload can reach an
#      adapter, because the argv is built from those three values and nothing
#      else. At-least-once delivery is therefore safe: N fires converge to one
#      bus check.
#   2. PROVISIONING IS EXPLICIT: scripts are located only under
#      `COORD_WAKE_ADAPTER_DIR`. An un-provisioned host reports `unconfigured`,
#      leaving the wake VISIBLY QUEUED (never a silent drop, never a burned
#      retry) — and, since the variable is unset by default, an engine that
#      merely has the repo checked out fires nothing.

#: Host provisioning switch: directory holding `<adapter>.sh`. Unset ⇒ this host
#: runs no host-local adapter script at all.
WAKE_ADAPTER_DIR_ENV = "COORD_WAKE_ADAPTER_DIR"

#: Hard upper bound (seconds) on one adapter run. A hung adapter must never
#: wedge the executor, so the process GROUP is killed at the bound and the wake
#: is reported `failed` (bounded retry → dead-letter), never left hanging.
WAKE_ADAPTER_TIMEOUT_ENV = "COORD_WAKE_ADAPTER_TIMEOUT"
DEFAULT_WAKE_ADAPTER_TIMEOUT = 10.0

#: Adapters with a real script in-tree. Everything else in
#: `router.ADAPTERS_HOST_LOCAL` still reports `unconfigured` (W6 owns them).
SCRIPT_ADAPTERS = frozenset({"macos-notify"})

#: The ONLY reason text an adapter ever receives. Fixed bytes, never derived
#: from the queue entry — that is what makes the content rule structural.
NUDGE_REASON = "directed item on your bus - check your inbox / needs-me"

#: Accepted shape for the two identity fields that cross the boundary. Must
#: start alphanumeric, so a value can never be read as an option by the adapter
#: or by anything it hands its arguments to.
_ADAPTER_FIELD = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@-]{0,255}$")

#: Adapter stderr is quoted into a durable delivery/dead-letter record; keep it
#: bounded so a chatty failure cannot bloat the store.
_DETAIL_CHARS = 300


def adapter_script(adapter: str) -> Optional[Path]:
    """The provisioned script for ``adapter``, or ``None`` when this host is not
    provisioned for it (env unset, or no such file). ``None`` is the
    `unconfigured` path — deliberately indistinguishable from "not provisioned",
    because both mean the same thing operationally: leave the wake queued."""
    root = (os.environ.get(WAKE_ADAPTER_DIR_ENV) or "").strip()
    if not root:
        return None
    path = Path(root).expanduser() / f"{adapter}.sh"
    try:
        return path if path.is_file() else None
    except OSError:
        return None


def _detail(text: str) -> str:
    flat = " ".join((text or "").split())
    return flat[:_DETAIL_CHARS]


def run_script_adapter(inv: dict[str, Any]) -> tuple[str, str]:
    """Run a provisioned host-local adapter script → (status, detail), status one
    of ``delivered`` | ``failed`` | ``unconfigured``.

    - not provisioned / script absent / present-but-not-executable ⇒
      ``unconfigured``: the wake stays VISIBLY QUEUED and no retry is burned.
    - exit 0 ⇒ ``delivered``; any other exit, an un-spawnable script, or the
      timeout ⇒ ``failed`` (the executor's bounded retry → dead-letter path).
    - the invocation is reduced to ``--agent``/``--key``/``--reason`` with the
      reason fixed at ``NUDGE_REASON``; an agent id or key outside
      ``_ADAPTER_FIELD`` is refused BEFORE the script runs.
    """
    adapter = str(inv.get("adapter") or "")
    if adapter not in SCRIPT_ADAPTERS:
        return ("unconfigured",
                f"no host-local adapter script wired for {adapter!r} on this "
                f"executor yet")
    script = adapter_script(adapter)
    if script is None:
        return ("unconfigured",
                f"host not provisioned for {adapter!r}: no "
                f"${WAKE_ADAPTER_DIR_ENV}/{adapter}.sh — wake stays queued")
    if not os.access(script, os.X_OK):
        return ("unconfigured",
                f"{script} is present but not executable — wake stays queued")

    agent = str(inv.get("agent") or "")
    key = str(inv.get("idempotency_key") or "")
    if not (_ADAPTER_FIELD.match(agent) and _ADAPTER_FIELD.match(key)):
        return ("failed",
                f"agent id / idempotency key outside the accepted charset for "
                f"{adapter!r} — nothing was passed to the adapter")

    # The whole content surface. Nothing else from `inv` is ever read.
    argv = [str(script), "--agent", agent, "--key", key,
            "--reason", NUDGE_REASON]
    timeout = config.env_float(WAKE_ADAPTER_TIMEOUT_ENV,
                               DEFAULT_WAKE_ADAPTER_TIMEOUT)
    try:
        rc, out, err = run_bounded(argv, timeout)
    except subprocess.TimeoutExpired:
        return ("failed",
                f"{adapter} timed out after {timeout:g}s — process group "
                f"killed, wake not delivered")
    except OSError as exc:
        return ("failed", f"{adapter} could not be spawned: {_detail(str(exc))}")
    if rc == 0:
        return ("delivered", f"{adapter} posted a keyed nudge for {agent}")
    return ("failed", f"{adapter} exited {rc}: {_detail(err or out)}")
