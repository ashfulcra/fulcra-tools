"""Build URLs to the local Fulcra Collect daemon's web UI.

The daemon's web port is user-configurable via [daemon] web_port in
config.toml; hardcoding 127.0.0.1:9292 silently breaks the menubar's
docs + Configure deep-links for users who change the default port.

This module reads ~/.config/fulcra-collect/web-url (written by the
daemon at startup, see collect/web.py — _web_url_path()) so the URL
respects the override. Falls back to constructing the URL from the
configured web_port if the file is unreadable (e.g., daemon not yet
started, or the file was wiped).

Why a file and not just call config.load() directly: the daemon may
in future bind to something other than 127.0.0.1 (e.g., a UNIX socket
proxy, or an ephemeral port if 9292 is taken), and the well-known
file is the daemon's authoritative answer for "where am I actually
listening". Reading config is the second-best fallback.
"""
from __future__ import annotations

from fulcra_collect import config as _config


def daemon_base_url() -> str:
    """Return the daemon's web base URL (e.g., 'http://127.0.0.1:9292').

    Prefers the well-known web-url file the daemon writes at startup;
    falls back to building 'http://127.0.0.1:<web_port>' from config
    when the file is missing or unreadable.
    """
    web_url_file = _config.config_dir() / "web-url"
    try:
        url = web_url_file.read_text(encoding="utf-8").strip()
        if url:
            return url
    except (OSError, FileNotFoundError):
        pass
    # Fallback: construct from configured port. config.load() tolerates
    # a missing config.toml and returns defaults, so this path is safe
    # even on a fresh install.
    cfg = _config.load()
    port = getattr(cfg, "web_port", _config.DEFAULT_WEB_PORT)
    return f"http://127.0.0.1:{port}"


def daemon_url(path: str = "/") -> str:
    """Return a full URL to a daemon route. `path` may start with '/' or not."""
    base = daemon_base_url().rstrip("/")
    if not path.startswith("/"):
        path = "/" + path
    return f"{base}{path}"
