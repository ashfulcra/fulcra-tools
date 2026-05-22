"""The hub config directory and the TOML config file.

Config holds only non-secret data: which plugins are enabled, per-plugin
scheduling-interval overrides (seconds), and per-plugin settings. Secrets
live in the keychain (see credentials.py).
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

import tomli_w


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


@dataclass
class Config:
    enabled: set[str] = field(default_factory=set)
    interval_overrides: dict[str, int] = field(default_factory=dict)  # plugin id -> seconds
    plugin_settings: dict[str, dict] = field(default_factory=dict)

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
    doc = tomllib.loads(path.read_text(encoding="utf-8"))
    return Config(
        enabled=set(doc.get("enabled", [])),
        interval_overrides=dict(doc.get("interval_overrides", {})),
        plugin_settings=dict(doc.get("plugin_settings", {})),
    )


def save(cfg: Config) -> None:
    doc = {
        "enabled": sorted(cfg.enabled),
        "interval_overrides": cfg.interval_overrides,
        "plugin_settings": cfg.plugin_settings,
    }
    _config_path().write_text(tomli_w.dumps(doc), encoding="utf-8")
