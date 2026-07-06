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
import subprocess
import tempfile
from typing import Any, Optional

DEFAULT_COMMAND = ("fulcra-api",)


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

    def __init__(self, command: Optional[list[str]] = None, *, timeout: float = 30.0):
        self.command = command or _split_command()
        self.timeout = timeout

    def updates(self, period: str) -> Optional[list]:
        """File-change feed via ``fulcra-api data-updates`` — the reconcile fast
        path's evidence source. Never raises: any failure returns None, which
        callers treat as "no evidence, do the full pass"."""
        import json as _json
        try:
            proc = subprocess.run(
                [*self.command, "data-updates", period],
                capture_output=True, text=True, timeout=self.timeout,
            )
            if proc.returncode != 0:
                return None
            data = _json.loads(proc.stdout)
            changes = data.get("file_changes")
            return changes if isinstance(changes, list) else None
        except Exception:
            return None

    def _run(self, args: list[str], **kw: Any) -> subprocess.CompletedProcess:
        return subprocess.run(
            [*self.command, "file", *args],
            capture_output=True, text=True, timeout=self.timeout, **kw,
        )

    def list_dir(self, prefix: str) -> list[dict[str, Any]]:
        cp = self._run(["list", prefix])
        if cp.returncode != 0:
            raise TransportError(f"list {prefix} failed: {cp.stderr.strip()}")
        # `fulcra-api file list` order is not guaranteed stable; sort by name so
        # every consumer sees a deterministic order (matters wherever "last wins").
        return sorted(parse_list_output(cp.stdout), key=lambda e: e.get("name") or "")

    def read(self, path: str) -> Optional[str]:
        cp = self._run(["download", path, "-"])
        if cp.returncode != 0:
            return None  # not found / error -> None (caller handles)
        return cp.stdout

    def write(self, path: str, content: str) -> bool:
        with tempfile.NamedTemporaryFile(
            "w", suffix=".tmp", delete=False, encoding="utf-8"
        ) as fh:
            fh.write(content)
            local = fh.name
        try:
            cp = self._run(["upload", local, path])
            return cp.returncode == 0
        finally:
            try:
                os.unlink(local)
            except OSError:
                pass

    def stat(self, path: str) -> Optional[dict[str, Any]]:
        cp = self._run(["stat", path])
        if cp.returncode != 0:
            return None
        return parse_stat_output(cp.stdout)

    def delete(self, path: str) -> bool:
        return self._run(["delete", path]).returncode == 0


class TransportError(RuntimeError):
    pass
