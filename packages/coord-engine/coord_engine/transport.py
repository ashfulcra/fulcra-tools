"""Fulcra File Store transport for L1 — a thin wrapper over ``fulcra-api file``.

``file`` output is human TEXT (not JSON), so this module owns the parsers. The
subprocess methods (``list_dir``/``read``/``write``/``stat``/``delete``) form the
duck-typed interface ``reconcile`` depends on; tests substitute an in-memory fake.

Change detection uses the ``list`` minute-granular timestamp via EQUALITY (re-read
when it differs), the conservative reading of the documented minute-resolution
limit — sub-minute double-edits are re-scanned on the next pass.
"""

from __future__ import annotations

import os
import shlex
import signal
import subprocess
import tempfile
from typing import Any, Optional

DEFAULT_COMMAND = ("fulcra-api",)

#: Per-op HARD upper bound (seconds). Overridable via ``COORD_TRANSPORT_TIMEOUT``
#: or the constructor arg (which wins). Watchers run this tight (e.g. 8s) so the
#: engine's fold budgets buy real responsiveness instead of soft promises.
DEFAULT_TRANSPORT_TIMEOUT = 30.0

#: After the op timeout fires we SIGKILL the child's whole group, then give the
#: drain this long to complete; if it still won't, we abandon the pipes rather
#: than block. Keeps the effective bound at ``timeout`` + this small constant.
_TRANSPORT_GRACE = 2.0


def _transport_timeout() -> float:
    """Default per-op timeout, seconds. Env override ``COORD_TRANSPORT_TIMEOUT``
    — same parse-hardening as the fold budgets: unparseable, non-positive, NaN,
    or inf falls back to the default (a bad env value must never disable the
    bound or make an op hang). The constructor arg still takes precedence."""
    raw = os.environ.get("COORD_TRANSPORT_TIMEOUT")
    if raw is None:
        return DEFAULT_TRANSPORT_TIMEOUT
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_TRANSPORT_TIMEOUT
    if not (v > 0) or v == float("inf"):  # NaN, <=0, inf -> default
        return DEFAULT_TRANSPORT_TIMEOUT
    return v


def _kill_process_group(proc: "subprocess.Popen") -> None:
    """SIGKILL the child's WHOLE process group so a grandchild that inherited
    the stdout/stderr pipes dies with it (``start_new_session`` gave the child
    its own group). Guards the ``getpgid`` race (child already reaped) and the
    non-POSIX case (no ``killpg``): falls back to killing the direct child so we
    never leave it running."""
    if hasattr(os, "killpg") and hasattr(os, "getpgid"):
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            return
        except (ProcessLookupError, PermissionError, OSError):
            pass  # race/perms — fall through to a direct kill
    try:
        proc.kill()
    except OSError:
        pass


def run_bounded(
    argv: list[str], timeout: float, **popen_kw: Any
) -> "tuple[int, str, str]":
    """Run ``argv`` with a HARD upper bound of ``timeout`` + ``_TRANSPORT_GRACE``,
    no matter what the child's descendant tree does. Returns
    ``(returncode, stdout, stderr)``.

    ``subprocess.run(timeout=)`` is NOT enough: on ``TimeoutExpired`` it kills
    only the DIRECT child and (on POSIX) ``wait()``s on it alone, so a grandchild
    that inherited the pipes is left running — a leaked tree still holding the
    fds — and on non-POSIX the post-kill drain can block on it indefinitely. We
    put the child in its own session/process group and, on timeout, SIGKILL the
    whole group, then drain under a short grace; if even the grace drain won't
    complete we abandon the pipes rather than block. Raises the original
    ``TimeoutExpired`` on timeout and ``OSError`` if the binary can't be spawned
    — callers convert both to their documented failure mode."""
    proc = subprocess.Popen(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,  # child gets its OWN process group
        **popen_kw,
    )
    try:
        out, err = proc.communicate(timeout=timeout)
        return proc.returncode, out, err
    except subprocess.TimeoutExpired as exc:
        _kill_process_group(proc)
        try:
            proc.communicate(timeout=_TRANSPORT_GRACE)
        except subprocess.TimeoutExpired:
            # A descendant is still wedged holding the pipes: abandon the drain
            # rather than let it hang the caller past the bound. The group has
            # already had SIGKILL; the OS reclaims it.
            try:
                proc.kill()
            except OSError:
                pass
            for stream in (proc.stdout, proc.stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass
        raise exc


def _split_command() -> list[str]:
    raw = os.environ.get("FULCRA_CLI_COMMAND")
    if raw:
        # shlex, not str.split, so a CLI path containing spaces stays one argv
        # token. Malformed shell syntax (unbalanced quote) falls back to the
        # naive split rather than crashing command resolution.
        try:
            return shlex.split(raw)
        except ValueError:
            return raw.split()
    return list(DEFAULT_COMMAND)


def parse_list_output(text: str) -> list[dict[str, Any]]:
    """Parse ``fulcra-api file list`` text into entries.

    Line shape observed (v0.1.34): ``93B     2026-07-01 04:12PM UTC  probe.md``
    i.e. ``<size> <date> <time> <tz> <name>``. Directories may end with ``/``.
    Unparseable lines are skipped.
    """
    entries: list[dict[str, Any]] = []
    for line in (text or "").splitlines():
        if not line.strip():
            continue
        parts = line.split()
        if len(parts) < 5:
            # tolerate a bare "name/" directory line or odd formatting
            name = parts[-1] if parts else ""
            if name:
                entries.append(
                    {"name": name, "size": parts[0] if len(parts) > 1 else None,
                     "mtime": None, "is_dir": name.endswith("/")}
                )
            continue
        size = parts[0]
        mtime = f"{parts[1]} {parts[2]} {parts[3]}"
        name = " ".join(parts[4:])
        entries.append(
            {"name": name, "size": size, "mtime": mtime, "is_dir": name.endswith("/")}
        )
    return entries


def parse_stat_output(text: str) -> dict[str, Any]:
    """Parse ``fulcra-api file stat`` text into structured metadata."""
    out: dict[str, Any] = {"previous": []}
    for line in (text or "").splitlines():
        s = line.strip()
        if s.startswith("Uploaded:"):
            out["uploaded"] = s.split(":", 1)[1].strip()
        elif s.startswith("Version:"):
            out["version"] = s.split(":", 1)[1].strip()
        elif s.startswith("Previous Versions:"):
            try:
                out["previous_count"] = int(s.split(":", 1)[1].strip())
            except ValueError:
                out["previous_count"] = 0
        elif s.startswith("- "):
            toks = s[2:].split()
            if toks:
                out["previous"].append(
                    {"version": toks[0], "uploaded": toks[1] if len(toks) > 1 else None}
                )
        elif "(" in s and s.endswith(")") and "path" not in out:
            out["path"] = s.rsplit("(", 1)[0].strip()
    return out


class FulcraFileTransport:
    """Real transport backed by the ``fulcra-api file`` CLI."""

    def __init__(
        self, command: Optional[list[str]] = None, *, timeout: Optional[float] = None
    ):
        self.command = command or _split_command()
        # constructor arg wins (tests pin it); else the env-hardened default.
        self.timeout = timeout if timeout is not None else _transport_timeout()

    def updates(self, period: str) -> Optional[list]:
        """File-change feed via ``fulcra-api data-updates`` — the reconcile fast
        path's evidence source. Never raises: any failure returns None, which
        callers treat as "no evidence, do the full pass". The run is hard-bounded
        (``run_bounded``): a hung child tree returns None within the timeout, it
        cannot stall the fast path."""
        import json as _json
        try:
            rc, out, _err = run_bounded(
                [*self.command, "data-updates", period], self.timeout
            )
            if rc != 0:
                return None
            data = _json.loads(out)
            changes = data.get("file_changes")
            return changes if isinstance(changes, list) else None
        except Exception:
            return None

    def _run(self, args: list[str], **kw: Any) -> subprocess.CompletedProcess:
        """Invoke ``fulcra-api file <args>``, HARD-bounded by ``self.timeout``.

        The call runs through ``run_bounded``: the child gets its own process
        group and, on timeout, the whole group is SIGKILLed and the drain is
        grace-bounded, so no descendant of a hung ``fulcra-api`` can stretch the
        op past ``timeout`` + a small constant. Every fold budget in the engine
        depends on that per-op boundedness.

        A hung CLI call must not escape as a raw ``subprocess.TimeoutExpired`` —
        that bypasses the ``except TransportError`` guards in the folds and crashes
        never-crash surfaces (briefing/needs-me). Likewise a missing/unrunnable
        binary raises ``OSError`` (e.g. ``FileNotFoundError``) from ``subprocess``.
        Both are normalized to ``TransportError`` so the public contract holds:
        transport methods raise ``TransportError`` or honor their documented
        soft-failure return, and nothing else escapes.
        """
        argv = [*self.command, "file", *args]
        try:
            rc, out, err = run_bounded(argv, self.timeout, **kw)
        except subprocess.TimeoutExpired as exc:
            raise TransportError(
                f"timeout after {self.timeout}s: file {' '.join(args)}"
            ) from exc
        except OSError as exc:
            # binary missing / not executable / other exec-level failure
            raise TransportError(
                f"exec failed: file {' '.join(args)}: {exc}"
            ) from exc
        return subprocess.CompletedProcess(argv, rc, out, err)

    def list_dir(self, prefix: str) -> list[dict[str, Any]]:
        # contract: raises TransportError on any failure (incl. timeout/exec error,
        # which _run has already converted).
        cp = self._run(["list", prefix])
        if cp.returncode != 0:
            raise TransportError(f"list {prefix} failed: {cp.stderr.strip()}")
        # `fulcra-api file list` order is not guaranteed stable; sort by name so
        # every consumer sees a deterministic order (matters wherever "last wins").
        return sorted(parse_list_output(cp.stdout), key=lambda e: e.get("name") or "")

    def read(self, path: str) -> Optional[str]:
        # contract: None on any failure — timeout/exec error follow the same path
        # as a non-zero return code.
        try:
            cp = self._run(["download", path, "-"])
        except TransportError:
            return None
        if cp.returncode != 0:
            return None  # not found / error -> None (caller handles)
        return cp.stdout

    def write(self, path: str, content: str) -> bool:
        # contract: True on success, False on any REMOTE failure (incl. timeout/exec
        # error) — the upload subprocess. NOTE: staging the content to a local
        # tempfile happens first and can still raise OSError (disk full, bad perms);
        # that surfaces to the caller rather than returning False.
        with tempfile.NamedTemporaryFile(
            "w", suffix=".tmp", delete=False, encoding="utf-8"
        ) as fh:
            fh.write(content)
            local = fh.name
        try:
            cp = self._run(["upload", local, path])
            return cp.returncode == 0
        except TransportError:
            return False
        finally:
            try:
                os.unlink(local)
            except OSError:
                pass

    def stat(self, path: str) -> Optional[dict[str, Any]]:
        # contract: None on any failure (incl. timeout/exec error).
        try:
            cp = self._run(["stat", path])
        except TransportError:
            return None
        if cp.returncode != 0:
            return None
        return parse_stat_output(cp.stdout)

    def delete(self, path: str) -> bool:
        # contract: True on success, False on any failure (incl. timeout/exec error).
        try:
            return self._run(["delete", path]).returncode == 0
        except TransportError:
            return False


class TransportError(RuntimeError):
    pass
