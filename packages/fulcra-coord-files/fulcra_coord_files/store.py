"""Fulcra Files object-store transport — thin wrapper around the Fulcra CLI.

This module is the extracted, behavior-identical transport that used to live in
``fulcra_coord.remote``. It owns ONLY the wire layer: how we shell the Fulcra CLI
to put/get/stat/list/delete immutable blobs. Path-layout logic (which
``remote_root()``-anchored path a given record lives at) deliberately stays in
``fulcra_coord.remote`` — that is policy of the coordination bus, not of the
store.

THE NO-CAS CONTRACT (load-bearing — every safe caller depends on it):

The Fulcra Files store has **no compare-and-swap**. There is no atomic
"write only if the remote version still equals X". That single fact dictates
how anything built on this transport must behave:

  * The durable unit is an **immutable, uniquely-named blob that is never
    overwritten in place**. Concurrency safety comes from each writer owning a
    DISTINCT path (per-agent presence files, per-id archive index shards, a
    single per-day rolling marker claimed first-writer-wins) — NOT from locking
    a shared mutable object. A shared mutable index would let concurrent writers
    silently clobber each other's writes, because the store cannot reject a
    stale put.

  * ``stat`` / version is a **staleness HINT, not a correctness guarantee**.
    A matching version between two stats is strong evidence — not proof — that
    nothing changed (a re-upload can reproduce a byte-identical body and thus an
    identical size/hash). A DIFFERING version is proof that something changed.
    So ``stat_changed`` short-circuits on a strong identity match but treats
    weak indicators as one-directional: "differs => changed", never "equal =>
    unchanged". Read-modify-write against a shared path is inherently racy here
    and must be avoided rather than guarded.

Backend resolution order:
  1. FULCRA_COORD_BACKEND env var (split on whitespace) — for testing
  2. FULCRA_CLI_COMMAND env var
  3. ``fulcra-api`` if found on PATH
  4. ``uv tool run fulcra-api`` (fallback)

All remote I/O goes through subprocesses. Tests inject a fake backend via the
``backend=`` parameter or the ``FULCRA_COORD_BACKEND`` env var.

Timeout env vars:
  FULCRA_COORD_TIMEOUT_SECONDS           — read ops (default: 5)
  FULCRA_COORD_RECONCILE_TIMEOUT_SECONDS — reconcile (default: 90)
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Optional


def _env_int(name: str, default: int) -> int:
    """Read an integer env var, falling back to ``default`` on unset/invalid.

    Vendored from ``fulcra_coord.env_int`` so the transport stays free of any
    import back into the coordination package (which would create a dependency
    cycle: coord -> files -> coord). A non-numeric env value must degrade to the
    default rather than crash every read op on a typo'd value, which is the whole
    reason the callers below use this instead of a bare ``int()``.

    NOTE: the original ``fulcra_coord.env_int`` took a third ``override`` arg; it
    was intentionally dropped here because the transport's only callers
    (``_read_timeout`` / ``_reconcile_timeout``) never used it. Do not assume
    signature parity with ``fulcra_coord.env_int`` — re-add the param if a future
    caller needs it.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


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
    # _env_int (not a bare int()): a non-numeric override falls back to 5 instead
    # of raising and crashing every read op on a typo'd value.
    return _env_int("FULCRA_COORD_TIMEOUT_SECONDS", 5)


def _write_timeout() -> int:
    return max(15, _read_timeout())


def _reconcile_timeout() -> int:
    return _env_int("FULCRA_COORD_RECONCILE_TIMEOUT_SECONDS", 90)


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


def delete(remote_path: str, *, backend: Optional[list[str]] = None) -> bool:
    """Delete remote_path, wrapping ``fulcra file delete <PATH>``. Returns True on
    success, False on any failure (missing file, timeout, backend error).

    The platform delete is a SOFT-delete (the prior version is recoverable via
    ``fulcra file restore <VERSION_ID>``), which is what makes the retention
    prune of regenerable markers / dead presence safe. The archive MOVE relies on
    this returning honestly: _archive_task only deletes the hot copy AFTER it has
    verified the archived body landed, and a False here just leaves a recoverable
    duplicate for the next pass to finish — never a lost task. Best-effort: never
    raises, so a prune/move failure can't escape into the reconcile tick."""
    cmd = (backend or _backend_cmd()) + ["delete", remote_path]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_write_timeout(),
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False


def list_files(
    prefix: str,
    *,
    backend: Optional[list[str]] = None,
    timeout: Optional[int] = None,
) -> list[str]:
    """List remote files under prefix. Returns NORMALIZED full remote paths
    (e.g. ``/coordination/health/<id>.json``) that ``download_json`` / ``delete``
    accept directly — never the CLI's raw display lines.

    Why this normalization exists: the real ``fulcra file list`` formats each
    line for humans as ``"<size>  <date> <tz>  <FILENAME>"`` (size + date +
    FILENAME-only, no path). Returning those lines verbatim — as 0.9.0 did —
    silently broke every list-based consumer in live (self-heal, presence
    reconcile/prune, retention pruning, ``search --archived``, the health
    command): they each fed the formatted string straight into
    ``download_json`` / ``delete``, which is not a path, so it resolved to
    None / a no-op. Tests stayed green only because the fake backend emits
    clean full paths. We reconstruct the path here so the two formats converge.

    Robust to BOTH shapes: take the filename as the LAST whitespace-delimited
    token (for the formatted line that's the trailing filename; for an
    already-clean path it's the whole path, since these are slug/id-based names
    with NO spaces — that no-spaces assumption is what makes last-token
    extraction safe). If that token is already a full path (starts with "/")
    or already begins with the prefix, pass it through unchanged; otherwise it's
    a bare filename from the real CLI, so join it onto the prefix.

    Best-effort: returns [] on any error (non-zero exit, timeout, missing CLI)."""
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
        normalized: list[str] = []
        for raw in result.stdout.splitlines():
            line = raw.strip()
            if not line:
                continue
            # Filenames are slug/id-based with no spaces, so the trailing
            # whitespace-delimited token is the filename (real CLI) or the
            # whole clean path (fake / already-normalized).
            name = line.split()[-1]
            if name.startswith("/") or name.startswith(prefix):
                normalized.append(name)
            else:
                normalized.append(prefix.rstrip("/") + "/" + name)
        return normalized
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return []


def list_json(
    prefix: str,
    *,
    backend: Optional[list[str]] = None,
    suffix: str = ".json",
    max_workers: int = 8,
) -> list[tuple[str, dict[str, Any]]]:
    """List ``prefix`` and PARALLEL-download every file ending in ``suffix``,
    returning ``[(path, record), ...]`` for each path whose JSON parsed to a dict.

    This is the shared "enumerate a bus directory and read each record" primitive
    behind self-reported per-host/per-agent state (presence reconcile + prune,
    health load + prune, the archive cold-index). Before this helper each consumer
    open-coded the same ``for path in list_files(...): download_json(...)`` loop
    SERIALLY — N+1 round-trips per call, on the reconcile hot path.

    PERF: downloads run in a ThreadPoolExecutor, the exact pattern proven in
    cli._load_all_tasks — each ``download`` is one independent subprocess with no
    shared mutable state, so the pool is safe and collapses N serial ~1.3s
    round-trips into a single batch's wall-time. This is the per-tick win for
    presence reconcile, health load, and the retention prunes.

    ORDER-PRESERVING: results are returned in ``list_files`` order (not completion
    order), so the helper is a behavior-exact drop-in for the serial loops it
    replaces — a caller that aggregates/dedups, and any test that asserts on the
    sequence, both see the identical ordering they did before.

    BEST-EFFORT, per-item isolated: a failed listing yields ``[]``; a single
    download/parse failure (or an unexpected raise inside the pool) drops just that
    item; a non-dict payload is dropped. Never raises into the caller — the same
    contract every existing consumer relied on. Each consumer keeps its OWN record
    predicate (``rec.get("agent")``, ``is_prunable_*``, ...); this helper only owns
    the list + parallel-read + dict-guard.
    """
    try:
        paths = [p for p in list_files(prefix, backend=backend) if p.endswith(suffix)]
    except Exception:
        return []
    if not paths:
        return []
    results: dict[str, dict[str, Any]] = {}
    workers = min(max_workers, max(2, len(paths)))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(download_json, path, backend=backend): path for path in paths
        }
        for fut in concurrent.futures.as_completed(futures):
            path = futures[fut]
            try:
                rec = fut.result()
            except Exception:
                rec = None
            if isinstance(rec, dict):
                results[path] = rec
    return [(path, results[path]) for path in paths if path in results]


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


def check_file_commands(backend: Optional[list[str]] = None) -> tuple[bool, str]:
    """Probe whether the resolved Fulcra CLI exposes the ``file`` command group.

    WHY THIS EXISTS (the #1 fresh-agent onboarding failure):
    The public PyPI ``fulcra-api`` build (e.g. 0.1.32) does NOT ship the ``file``
    command group, yet the entire coordination bus is driven by ``fulcra file``
    ops (upload/download/stat/list). A freshly-onboarded agent that pip-installs
    ``fulcra-api`` and runs ``fulcra-coord`` then sees every bus op fail
    *silently* with no clear signal why. This probe gives doctor a dedicated,
    legible signal so the failure points straight at the fix: install a
    file-capable build (the ``file-management`` branch of
    ``fulcradynamics/fulcra-api-python`` — see docs/fulcra-cli-branch.md).

    Distinct from ``check_cli_available``: that helper probes whatever
    ``_backend_cmd()`` resolves to, which in tests is the *fake backend* (it
    speaks the ``file`` subcommand protocol directly and has no top-level
    ``file`` group). This helper deliberately targets the **resolved real CLI
    base** (``cli_base_cmd()``) + ``file --help``, the same base every real file
    op shells, so it answers exactly "does the installed CLI have ``file``?".

    Robust by contract: a missing binary, a non-zero exit, a timeout, or any OS
    error all degrade to ``(False, message)`` — this must never raise, so doctor
    can call it without a guard and never crash on a hung or absent CLI.

    Returns ``(ok, message)`` where ``message`` names the probed base on success
    or describes the failure otherwise.
    """
    base = backend or cli_base_cmd()
    cmd = base + ["file", "--help"]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return True, " ".join(base)
        detail = (result.stderr.strip() or result.stdout.strip()).splitlines()
        suffix = f": {detail[0]}" if detail else ""
        return False, f"`{' '.join(base)} file` returned code {result.returncode}{suffix}"
    except FileNotFoundError:
        return False, f"Command not found: {base[0]}"
    except (subprocess.TimeoutExpired, OSError) as e:
        return False, f"file probe failed: {e}"
    except Exception as e:  # pragma: no cover - defensive: never crash doctor
        return False, f"file probe error: {e}"


def probe_reachable(backend: Optional[list[str]] = None, *, root: str = "/coordination") -> bool:
    """Cheap liveness probe: is the Fulcra remote reachable at all?

    WHY THIS EXISTS (distinct from stat/download returning None):
    ``stat``/``download`` collapse two very different conditions into the same
    ``None`` — "the file genuinely does not exist yet" (a fresh, reachable
    backend with no tasks) and "the backend could not be reached" (auth gone,
    CLI missing, network down). A reader that treats both as "empty" silently
    masks an outage as a successful empty result.

    This probe disambiguates by running the ``list`` subcommand against the
    coordination ``root``: the backend process EXITING SUCCESSFULLY (returncode
    0) proves the remote is reachable, regardless of whether any files came back.
    A non-zero exit, a missing CLI, a timeout, or any OS error means the remote
    is NOT reachable. We deliberately key off the *process* success rather than
    the (possibly empty) output, so an empty-but-reachable bus probes True.

    ``root`` is injected by ``fulcra_coord.remote`` (which binds it to the
    bus's ``remote_root()``) so this transport stays free of coordination-bus
    path policy. It defaults to ``/coordination`` for standalone use.
    """
    cmd = (backend or _backend_cmd()) + ["list", root]
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


def check_remote_access(
    backend: Optional[list[str]] = None, *, probe_path: str = "/coordination/index.json"
) -> tuple[bool, str]:
    """Try a stat on a well-known path to verify remote access.

    ``probe_path`` is injected by ``fulcra_coord.remote`` (binding it to
    ``{remote_root()}/index.json``) so the transport carries no bus path policy.
    Defaults to ``/coordination/index.json`` for standalone use.
    """
    s = stat(probe_path, backend=backend)
    if s is not None:
        return True, f"Remote accessible ({probe_path})"
    return False, (
        f"Could not stat {probe_path} — check auth or FULCRA_COORD_REMOTE_ROOT. "
        f"On a fresh installation with no tasks yet, run 'fulcra-coord start' first to initialize the root."
    )
