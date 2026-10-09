"""Minimal Fulcra Files writer for local source imports."""
from __future__ import annotations

import os
import shlex
import subprocess
import tempfile
from pathlib import Path


class VaultError(RuntimeError):
    pass


def _cli() -> list[str]:
    configured = os.environ.get("FULCRA_CLI_COMMAND", "").strip()
    if configured:
        return shlex.split(configured)
    from fulcra_common.client import find_fulcra_cli

    found = find_fulcra_cli()
    if not found:
        raise VaultError("Fulcra CLI is unavailable. Reinstall Collect or install fulcra-api.")
    return [found]


def _detail(stderr: str, limit: int = 400) -> str:
    text = (stderr or "").strip()
    return text if len(text) <= limit else "..." + text[-limit:]


class VaultWriter:
    def __init__(self, *, timeout: int = 300):
        self.timeout = timeout

    def _upload(self, local: Path | str, remote: str) -> None:
        try:
            result = subprocess.run(
                [*_cli(), "file", "upload", str(local), remote],
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise VaultError(f"Fulcra file upload failed: {type(exc).__name__}") from exc
        if result.returncode != 0:
            detail = _detail(result.stderr).replace(str(local), "<local-file>")
            raise VaultError(f"Fulcra file upload failed: {detail}")

    def upload_file(self, source: Path | str, remote: str) -> None:
        self._upload(source, remote)

    def write_text(self, remote: str, content: str) -> None:
        tmp: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", suffix=".md", delete=False
            ) as handle:
                handle.write(content)
                tmp = handle.name
            self._upload(tmp, remote)
        finally:
            if tmp:
                try:
                    os.unlink(tmp)
                except FileNotFoundError:
                    pass
