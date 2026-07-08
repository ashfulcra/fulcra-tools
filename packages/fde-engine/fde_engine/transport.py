"""Fulcra File Store transport — a thin wrapper over ``fulcra-api file``.

Deliberately a fresh copy of coord-engine's proven transport shape, trimmed to
what fde-engine needs (list/read/write/delete). The two engines share a pattern,
not an import: coupling the FDE engine to the coord bus package would drag the
whole coordination surface into a tool that must work standalone.

``fulcra-api file`` output is human TEXT, so this module owns the parser. The
methods form the duck-typed interface every command depends on; tests
substitute the in-memory fake in ``fde_engine_test_helpers``.
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
        # token; malformed shell syntax falls back to the naive split.
        try:
            return shlex.split(raw)
        except ValueError:
            return raw.split()
    return list(DEFAULT_COMMAND)


def parse_list_output(text: str) -> list[dict[str, Any]]:
    """Parse ``fulcra-api file list`` text: ``<size> <date> <time> <tz> <name>``,
    with bare ``name/`` lines for directories. Unparseable lines are skipped."""
    entries: list[dict[str, Any]] = []
    for line in (text or "").splitlines():
        if not line.strip():
            continue
        parts = line.split()
        if len(parts) < 5:
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


class TransportError(RuntimeError):
    pass


class FulcraFileTransport:
    """Real transport backed by the ``fulcra-api file`` CLI."""

    def __init__(self, command: Optional[list[str]] = None, *, timeout: float = 30.0):
        self.command = command or _split_command()
        self.timeout = timeout

    def _run(self, args: list[str]) -> subprocess.CompletedProcess:
        return subprocess.run(
            [*self.command, "file", *args],
            capture_output=True, text=True, timeout=self.timeout,
        )

    def list_dir(self, prefix: str) -> list[dict[str, Any]]:
        cp = self._run(["list", prefix])
        if cp.returncode != 0:
            raise TransportError(f"list {prefix} failed: {cp.stderr.strip()}")
        # Sort by name: list order isn't guaranteed stable, and deterministic
        # order matters everywhere a fold says "last wins".
        return sorted(parse_list_output(cp.stdout), key=lambda e: e.get("name") or "")

    def read(self, path: str) -> Optional[str]:
        cp = self._run(["download", path, "-"])
        if cp.returncode != 0:
            return None  # not found / error -> None (caller decides)
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

    def delete(self, path: str) -> bool:
        return self._run(["delete", path]).returncode == 0
