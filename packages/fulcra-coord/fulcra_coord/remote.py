"""Fulcra file operations — thin wrapper around the Fulcra CLI.

Backend resolution order:
  1. FULCRA_COORD_BACKEND env var (split on whitespace) — for testing
  2. FULCRA_CLI_COMMAND env var
  3. `fulcra-api` if found on PATH
  4. `uv tool run fulcra-api` (fallback)

All remote I/O goes through subprocesses. Tests inject a fake backend via
backend= parameter or FULCRA_COORD_BACKEND env var.

Timeout env vars:
  FULCRA_COORD_TIMEOUT_SECONDS          — read ops (default: 5)
  FULCRA_COORD_RECONCILE_TIMEOUT_SECONDS — reconcile (default: 90)
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Optional

from . import remote_root


# ---------------------------------------------------------------------------
# Backend resolution
# ---------------------------------------------------------------------------

def cli_base_cmd() -> list[str]:
    """Resolve the *base* Fulcra CLI invocation (NO subcommand appended).

    This is the single source of truth for "how do we shell the Fulcra CLI on
    this machine". File ops append ``file`` to it; annotation writes append
    ``create-data-type`` / ``delete-data-type``. Resolution order mirrors the
    documented backend precedence so every consumer honours the SAME configured
    CLI (e.g. ``FULCRA_CLI_COMMAND``) rather than each hardcoding ``fulcra``:

      1. ``FULCRA_CLI_COMMAND`` env var (explicit operator override)
      2. ``fulcra-api`` if found on PATH
      3. ``uv tool run fulcra-api`` (fallback)

    Note ``FULCRA_COORD_BACKEND`` is deliberately NOT consulted here: that
    override is the file-ops *fake backend* used in tests (it speaks the ``file``
    subcommand protocol of the emulator, not the real CLI's top-level command
    surface), so it has no meaning for annotation writes. Annotation tests inject
    their own base via ``FULCRA_CLI_COMMAND``."""
    env_cli = os.environ.get("FULCRA_CLI_COMMAND", "").strip()
    if env_cli:
        return env_cli.split()

    if shutil.which("fulcra-api"):
        return ["fulcra-api"]

    return ["uv", "tool", "run", "fulcra-api"]


def _backend_cmd() -> list[str]:
    """Return the base command list for Fulcra file operations."""
    # 1. Test override (fake backend emulator speaking the `file` protocol).
    env_test = os.environ.get("FULCRA_COORD_BACKEND", "").strip()
    if env_test:
        return env_test.split()

    # 2-4. Resolved real CLI base + the `file` subcommand.
    return cli_base_cmd() + ["file"]


def _read_timeout() -> int:
    return int(os.environ.get("FULCRA_COORD_TIMEOUT_SECONDS", "5"))


def _write_timeout() -> int:
    return max(15, _read_timeout())


def _reconcile_timeout() -> int:
    return int(os.environ.get("FULCRA_COORD_RECONCILE_TIMEOUT_SECONDS", "90"))


# ---------------------------------------------------------------------------
# Low-level wrappers
# ---------------------------------------------------------------------------

def stat(remote_path: str, *, backend: Optional[list[str]] = None) -> Optional[dict[str, Any]]:
    """Return parsed stat output for remote_path, or None on failure."""
    cmd = (backend or _backend_cmd()) + ["stat", remote_path]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_read_timeout(),
        )
        if result.returncode != 0:
            return None
        return _parse_stat(result.stdout)
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None


def download(
    remote_path: str,
    *,
    backend: Optional[list[str]] = None,
    timeout: Optional[int] = None,
) -> Optional[str]:
    """Download remote_path and return contents as string, or None on failure."""
    cmd = (backend or _backend_cmd()) + ["download", remote_path, "-"]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout or _read_timeout(),
        )
        if result.returncode != 0:
            return None
        return result.stdout
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None


def download_json(
    remote_path: str,
    *,
    backend: Optional[list[str]] = None,
    timeout: Optional[int] = None,
) -> Optional[dict[str, Any]]:
    """Download and parse JSON from remote_path."""
    text = download(remote_path, backend=backend, timeout=timeout)
    if text is None:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def upload(
    content: str,
    remote_path: str,
    *,
    backend: Optional[list[str]] = None,
    timeout: Optional[int] = None,
) -> bool:
    """Upload string content to remote_path. Returns True on success."""
    cmd = backend or _backend_cmd()
    tmp_path: Optional[str] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, prefix="fulcra-coord-"
        ) as tmp:
            tmp.write(content)
            tmp_path = tmp.name

        upload_cmd = cmd + ["upload", tmp_path, remote_path]
        result = subprocess.run(
            upload_cmd,
            capture_output=True,
            text=True,
            timeout=timeout or _write_timeout(),
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False
    finally:
        if tmp_path:
            try:
                Path(tmp_path).unlink(missing_ok=True)
            except Exception:
                pass


def upload_json(
    data: dict[str, Any],
    remote_path: str,
    *,
    backend: Optional[list[str]] = None,
    timeout: Optional[int] = None,
) -> bool:
    """Serialize data as JSON and upload to remote_path."""
    return upload(
        json.dumps(data, indent=2),
        remote_path,
        backend=backend,
        timeout=timeout,
    )


def list_files(
    prefix: str,
    *,
    backend: Optional[list[str]] = None,
    timeout: Optional[int] = None,
) -> list[str]:
    """List remote files under prefix. Returns list of paths."""
    cmd = (backend or _backend_cmd()) + ["list", prefix]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout or _read_timeout(),
        )
        if result.returncode != 0:
            return []
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return []


# ---------------------------------------------------------------------------
# Stat parsing
# ---------------------------------------------------------------------------

def _parse_stat(text: str) -> Optional[dict[str, Any]]:
    """Parse fulcra file stat output into a dict."""
    text = text.strip()
    if not text:
        return None

    # Try JSON first
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Parse human-readable text output
    data: dict[str, Any] = {"raw": text}
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if lines:
        first = lines[0]
        size_match = re.search(r"\((\d+)\s+bytes\)", first)
        if size_match:
            data["size"] = int(size_match.group(1))
        if first.startswith("/"):
            data["path_display"] = first.split(" (", 1)[0]
    for line in lines:
        if ":" in line:
            key, _, val = line.partition(":")
            norm_key = key.strip().lower().replace(" ", "_")
            norm_val: Any = val.strip()
            if norm_key == "version":
                data["version_id"] = norm_val
            elif norm_key == "uploaded":
                data["uploaded_at"] = norm_val
            elif norm_key == "previous_versions":
                try:
                    data["previous_versions"] = int(norm_val)
                except ValueError:
                    data["previous_versions"] = norm_val
            else:
                data[norm_key] = norm_val
    return data if len(data) > 1 else {"raw": text}


# ---------------------------------------------------------------------------
# Version change detection (optimistic concurrency)
# ---------------------------------------------------------------------------

def stat_changed(before: Optional[dict[str, Any]], after: Optional[dict[str, Any]]) -> bool:
    """Return True if the remote file changed between two stat calls.

    Strong identity keys (version_id / version / etag): if both sides have the
    key, an equal value is definitive proof of no change and we short-circuit.
    A differing value is definitive proof of change.

    Weak indicators (size, timestamps, previous_versions): equal values do NOT
    prove the file is unchanged (a re-upload can produce the same size); we
    therefore check ALL weak keys and return True if ANY of them differ.
    """
    if before is None and after is None:
        return False
    if before is None or after is None:
        return True

    # Strong identity keys — one match is definitive
    for key in ("version_id", "version", "etag"):
        bv = before.get(key)
        av = after.get(key)
        if bv is not None and av is not None:
            return bv != av

    # Weak indicators — any difference signals a change
    for key in ("size", "uploaded_at", "updated_at", "date_uploaded", "previous_versions"):
        bv = before.get(key)
        av = after.get(key)
        if bv is not None and av is not None and bv != av:
            return True

    if "raw" in before and "raw" in after:
        return before["raw"] != after["raw"]

    return False


# ---------------------------------------------------------------------------
# Remote path helpers
# ---------------------------------------------------------------------------

def task_remote_path(task_id: str) -> str:
    return f"{remote_root()}/tasks/{task_id}.json"


def view_remote_path(name: str) -> str:
    """name: index, active, next, recently-done, search-index"""
    if name == "index":
        return f"{remote_root()}/index.json"
    return f"{remote_root()}/views/{name}.json"


def workstream_remote_path(workstream: str) -> str:
    return f"{remote_root()}/workstreams/{workstream}.json"


def agent_remote_path(agent: str) -> str:
    return f"{remote_root()}/agents/{agent}.json"


# ---------------------------------------------------------------------------
# Auth / doctor helpers
# ---------------------------------------------------------------------------

def check_cli_available(backend: Optional[list[str]] = None) -> tuple[bool, str]:
    """Check if the Fulcra CLI file backend is reachable. Returns (ok, message)."""
    cmd = backend or _backend_cmd()
    # Probe the file subcommand itself. The base CLI can exist while the
    # installed build lacks Fulcra Files support, which would make every write
    # fail later with a misleading doctor result.
    try:
        result = subprocess.run(
            cmd + ["--help"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return True, " ".join(cmd)
        detail = (result.stderr.strip() or result.stdout.strip()).splitlines()
        suffix = f": {detail[0]}" if detail else ""
        return False, f"CLI file command returned code {result.returncode}{suffix}"
    except FileNotFoundError:
        return False, f"Command not found: {cmd[0]}"
    except (subprocess.TimeoutExpired, OSError) as e:
        return False, str(e)


def probe_reachable(backend: Optional[list[str]] = None) -> bool:
    """Cheap liveness probe: is the Fulcra remote reachable at all?

    WHY THIS EXISTS (distinct from stat/download returning None):
    ``stat``/``download`` collapse two very different conditions into the same
    ``None`` — "the file genuinely does not exist yet" (a fresh, reachable
    backend with no tasks) and "the backend could not be reached" (auth gone,
    CLI missing, network down). A reader that treats both as "empty" silently
    masks an outage as a successful empty result.

    This probe disambiguates by running the ``list`` subcommand against the
    coordination root: the backend process EXITING SUCCESSFULLY (returncode 0)
    proves the remote is reachable, regardless of whether any files came back.
    A non-zero exit, a missing CLI, a timeout, or any OS error means the remote
    is NOT reachable. We deliberately key off the *process* success rather than
    the (possibly empty) output, so an empty-but-reachable bus probes True.
    """
    cmd = (backend or _backend_cmd()) + ["list", remote_root()]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_read_timeout(),
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False


def check_remote_access(backend: Optional[list[str]] = None) -> tuple[bool, str]:
    """Try a stat on a well-known path to verify remote access."""
    probe_path = f"{remote_root()}/index.json"
    s = stat(probe_path, backend=backend)
    if s is not None:
        return True, f"Remote accessible ({probe_path})"
    return False, (
        f"Could not stat {probe_path} — check auth or FULCRA_COORD_REMOTE_ROOT. "
        f"On a fresh installation with no tasks yet, run 'fulcra-coord start' first to initialize the root."
    )
