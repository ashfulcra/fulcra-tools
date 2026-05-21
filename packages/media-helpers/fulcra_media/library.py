"""Path argument resolution.

Importers accept either a local filesystem path or a `fulcra:/...` URI that
points into the user's Fulcra Library. The Library is implemented by the
fulcra-api CLI's `file-commands` branch (`fulcra file download`).
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

FULCRA_URI_PREFIX = "fulcra:"


def is_fulcra_uri(value: str) -> bool:
    return value.startswith(FULCRA_URI_PREFIX) if value else False


def resolve(path_or_uri: str) -> Path:
    """Return a local Path. Downloads to a tempfile if it's a fulcra: URI."""
    if is_fulcra_uri(path_or_uri):
        remote = path_or_uri[len(FULCRA_URI_PREFIX):]
        if not remote.startswith("/"):
            remote = "/" + remote
        suffix = Path(remote).suffix or ""
        tf = tempfile.NamedTemporaryFile(prefix="fulcra-media-", suffix=suffix, delete=False)
        tf.close()
        local = Path(tf.name)
        try:
            subprocess.run(
                ["fulcra", "file", "download", remote, str(local)],
                check=True,
                capture_output=True,
            )
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(
                f"fulcra file download {remote} failed (rc={exc.returncode}): "
                f"{exc.stderr!r}"
            ) from exc
        return local

    p = Path(path_or_uri).expanduser()
    if not p.exists():
        raise FileNotFoundError(p)
    return p
