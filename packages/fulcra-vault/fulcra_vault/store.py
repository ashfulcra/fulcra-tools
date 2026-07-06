"""Fulcra Files store wrapper for vault text files."""
from __future__ import annotations

import json
import os
from pathlib import PurePosixPath
import shlex
import shutil
import subprocess
import tempfile
from typing import Callable

from .schema import VAULT_ROOT, fulcra_absolute_path


Runner = Callable[[list[str], int], subprocess.CompletedProcess]


class StoreError(RuntimeError):
    """Base class for vault store failures."""


class MissingFileError(StoreError):
    """Raised when a requested file is positively missing."""


class TransportError(StoreError):
    """Raised when the Fulcra Files transport fails."""


class FulcraVaultStore:
    def __init__(self, *, runner: Runner | None = None,
                 cli_base: list[str] | None = None,
                 read_timeout: int = 30,
                 write_timeout: int = 60):
        self._runner = runner or _default_runner
        self._cli_base = cli_base or _cli_base()
        self._read_timeout = read_timeout
        self._write_timeout = write_timeout

    def read_text(self, path: str) -> str:
        remote = remote_path(path)
        result = self._run(["download", remote, "-"], self._read_timeout)
        if result.returncode == 0:
            return result.stdout
        if _is_not_found(result.stderr):
            raise MissingFileError(remote)
        raise TransportError(_failure("download failed", remote, result))

    def write_text(self, path: str, content: str) -> None:
        remote = remote_path(path)
        tmp_path: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                suffix=".md",
                prefix="fulcra-vault-",
                delete=False,
            ) as tmp:
                tmp.write(content)
                tmp_path = tmp.name
            result = self._run(["upload", tmp_path, remote], self._write_timeout)
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except FileNotFoundError:
                    pass
        if result.returncode != 0:
            raise TransportError(_failure("upload failed", remote, result))

    def stat(self, path: str) -> dict | None:
        remote = remote_path(path)
        result = self._run(["stat", remote], self._read_timeout)
        if result.returncode == 0:
            return _parse_stat(result.stdout)
        if _is_not_found(result.stderr):
            return None
        raise TransportError(_failure("stat failed", remote, result))

    def list(self, prefix: str = VAULT_ROOT) -> list[str]:
        remote = remote_path(prefix, note=False)
        result = self._run(["list", remote], self._read_timeout)
        if result.returncode != 0:
            raise TransportError(_failure("list failed", remote, result))
        return [
            _normalize_list_entry(line, remote)
            for line in result.stdout.splitlines()
            if line.strip()
        ]

    def delete_explicit(self, path: str, *,
                        expected_stat: dict | None = None) -> bool:
        remote = remote_path(path)
        if expected_stat is not None and self.stat(remote) != expected_stat:
            raise TransportError(f"confirmation stat mismatch for {remote}")
        result = self._run(["delete", remote], self._write_timeout)
        if result.returncode == 0:
            return True
        if _is_not_found(result.stderr):
            raise MissingFileError(remote)
        raise TransportError(_failure("delete failed", remote, result))

    def _run(self, args: list[str], timeout: int) -> subprocess.CompletedProcess:
        cmd = [*self._cli_base, "file", *args]
        try:
            return self._runner(cmd, timeout)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise TransportError(f"transport command failed: {e}") from e


def remote_path(path: str, *, note: bool = True) -> str:
    if not isinstance(path, str) or not path.strip():
        raise ValueError("path must be a non-empty string")
    raw = path.strip().replace("\\", "/")
    if raw.startswith("/"):
        return raw
    if raw == VAULT_ROOT:
        return f"/{VAULT_ROOT}"
    if raw.startswith(f"{VAULT_ROOT}/"):
        return "/" + PurePosixPath(raw).as_posix()
    if note:
        return fulcra_absolute_path(raw)
    return "/" + PurePosixPath(raw).as_posix()


def _default_runner(cmd: list[str], timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _cli_base() -> list[str]:
    env_cli = os.environ.get("FULCRA_CLI_COMMAND", "").strip()
    if env_cli:
        # shlex so a space-containing CLI path stays one argv token; fall back
        # to naive split on malformed shell syntax rather than crashing.
        try:
            return shlex.split(env_cli)
        except ValueError:
            return env_cli.split()
    if shutil.which("fulcra-api"):
        return ["fulcra-api"]
    return ["uv", "tool", "run", "fulcra-api"]


def _parse_stat(text: str) -> dict:
    stripped = text.strip()
    if not stripped:
        return {}
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        return {"raw": stripped}
    return parsed if isinstance(parsed, dict) else {"raw": stripped}


def _normalize_list_entry(line: str, prefix: str) -> str:
    name = line.strip()  # `file list` emits one bare path per line; keep spaces
    if name.startswith("/"):
        return name
    if name.startswith(prefix.lstrip("/") + "/"):
        return "/" + name
    return prefix.rstrip("/") + "/" + name


def _is_not_found(stderr: str | None) -> bool:
    text = (stderr or "").lower()
    return "404" in text or "not found" in text or "no such file" in text


def _failure(label: str, remote: str, result: subprocess.CompletedProcess) -> str:
    reason = (result.stderr or result.stdout or f"exit {result.returncode}").strip()
    return f"{label} for {remote}: {reason}"
