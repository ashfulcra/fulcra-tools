"""The hub config directory and the TOML config file.

Config holds only non-secret data: which plugins are enabled, per-plugin
scheduling-interval overrides (seconds), and per-plugin settings. Secrets
live in the keychain (see credentials.py).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import tomlkit


def config_dir() -> Path:
    """The hub config directory. `FULCRA_COLLECT_HOME` overrides the
    default `~/.config/fulcra-collect` (used by tests and power users)."""
    override = os.environ.get("FULCRA_COLLECT_HOME")
    base = Path(override) if override else Path.home() / ".config" / "fulcra-collect"
    base.mkdir(parents=True, exist_ok=True)
    # It holds the control socket and per-plugin state files — restrict to
    # the owner. Done unconditionally (like state._state_dir) so a
    # pre-existing, loosely-permissioned dir is tightened on every call.
    base.chmod(0o700)
    return base


def _config_path() -> Path:
    return config_dir() / "config.toml"


# Default TCP port the daemon's HTTP server binds to. Chosen far from
# common conflict ports (8000/8080/8888) and far from any other Fulcra
# loopback service. Stable across daemon restarts so OAuth redirect URIs
# (which are baked into the third-party app registration) and the
# attention browser extension (which posts to a known endpoint) don't
# break every time the daemon restarts. Override via `[daemon] web_port`
# in `config.toml` if 9292 collides on the user's machine.
DEFAULT_WEB_PORT = 9292


@dataclass
class Config:
    enabled: set[str] = field(default_factory=set)
    interval_overrides: dict[str, int] = field(default_factory=dict)  # plugin id -> seconds
    plugin_settings: dict[str, dict] = field(default_factory=dict)
    # Daemon-wide settings (currently just web_port). Kept on the top-level
    # Config rather than nested under plugin_settings because the web server
    # is part of the daemon, not a plugin.
    web_port: int = DEFAULT_WEB_PORT

    def enable(self, plugin_id: str) -> None:
        self.enabled.add(plugin_id)

    def disable(self, plugin_id: str) -> None:
        self.enabled.discard(plugin_id)

    def set_interval(self, plugin_id: str, seconds: int) -> None:
        self.interval_overrides[plugin_id] = seconds


def load() -> Config:
    path = _config_path()
    if not path.exists():
        return Config()
    doc = tomlkit.parse(path.read_text(encoding="utf-8"))
    # `[daemon] web_port = N` overrides the default. Stored under a
    # `[daemon]` table so a future daemon-wide setting can sit alongside
    # without leaking into plugin_settings. Tolerate the table being
    # missing (older config files) or the field being absent.
    daemon_section = doc.get("daemon", {}) or {}
    try:
        web_port = int(daemon_section.get("web_port", DEFAULT_WEB_PORT))
    except (TypeError, ValueError):
        web_port = DEFAULT_WEB_PORT
    return Config(
        enabled=set(doc.get("enabled", [])),
        interval_overrides=dict(doc.get("interval_overrides", {})),
        plugin_settings=dict(doc.get("plugin_settings", {})),
        web_port=web_port,
    )


def save(cfg: Config) -> None:
    path = _config_path()
    # Read the existing document to preserve any comments and custom
    # sections the user may have added. If the file doesn't exist yet,
    # start from an empty tomlkit document.
    if path.exists():
        doc = tomlkit.parse(path.read_text(encoding="utf-8"))
    else:
        doc = tomlkit.document()

    doc["enabled"] = sorted(cfg.enabled)
    doc["interval_overrides"] = cfg.interval_overrides
    doc["plugin_settings"] = cfg.plugin_settings
    # Only persist web_port when it differs from the default — this keeps
    # the default config file small and avoids writing a `[daemon]` table
    # the user never asked for. If the user wrote `web_port = 9292`
    # explicitly we silently drop it; that's fine, the loader uses the
    # default when the field is absent.
    if cfg.web_port != DEFAULT_WEB_PORT:
        daemon_table = doc.get("daemon")
        if daemon_table is None:
            daemon_table = tomlkit.table()
            doc["daemon"] = daemon_table
        daemon_table["web_port"] = cfg.web_port

    path.write_text(tomlkit.dumps(doc), encoding="utf-8")
