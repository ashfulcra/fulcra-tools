"""The hub config directory and the TOML config file.

Config holds only non-secret data: which plugins are enabled, per-plugin
scheduling-interval overrides (seconds), and per-plugin settings. Secrets
live in the keychain (see credentials.py).
"""
from __future__ import annotations

import fcntl
import os
import tempfile
import uuid
from contextlib import contextmanager
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
    plugin_epochs: dict[str, str] = field(default_factory=dict)
    _rotated_epochs: set[str] = field(default_factory=set, repr=False)

    def rotate_plugin_epoch(self, plugin_id: str) -> None:
        """Invalidate pending work after a credential or configuration transition."""
        self.plugin_epochs[plugin_id] = uuid.uuid4().hex
        self._rotated_epochs.add(plugin_id)

    def enable(self, plugin_id: str) -> None:
        self.enabled.add(plugin_id)

    def disable(self, plugin_id: str) -> None:
        self.enabled.discard(plugin_id)

    def set_interval(self, plugin_id: str, seconds: int) -> None:
        self.interval_overrides[plugin_id] = seconds


def load() -> Config:
    # Writers atomically replace the path, so this read observes one complete
    # old or new document without blocking behind a slow save.
    return _load_path(_config_path())


def _load_path(path: Path) -> Config:
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
        plugin_epochs=dict(doc.get("plugin_epochs", {})),
    )


@contextmanager
def _save_lock(path: Path):
    # Keep this inode permanently: unlinking a lock file lets overlapping writers
    # acquire different locks. The owner-private directory and file cover every
    # process saving this config, including CLI and daemon credential updates.
    fd = os.open(path.with_suffix(".lock"), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _atomic_write(path: Path, text: str) -> None:
    fd, temporary = tempfile.mkstemp(prefix=".config-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        # Failed writes/replacements must retain the old file and not leave
        # source settings in a temporary file. Successful replace removed it.
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def save(cfg: Config) -> None:
    path = _config_path()
    with _save_lock(path):
        _save_locked(cfg, path)


def _save_locked(cfg: Config, path: Path) -> None:
    # Read the existing document to preserve any comments and custom
    # sections the user may have added. If the file doesn't exist yet,
    # start from an empty tomlkit document.
    if path.exists():
        doc = tomlkit.parse(path.read_text(encoding="utf-8"))
    else:
        doc = tomlkit.document()

    old_enabled = set(doc.get("enabled", []))
    old_settings = dict(doc.get("plugin_settings", {}))
    epochs = dict(doc.get("plugin_epochs", {}))
    # A long-lived daemon Config may predate a credential change. Only
    # explicit rotations may replace the newer value already on disk.
    for plugin_id in cfg._rotated_epochs:
        epochs[plugin_id] = cfg.plugin_epochs[plugin_id]
    for plugin_id in old_enabled | cfg.enabled | set(old_settings) | set(cfg.plugin_settings):
        if ((plugin_id in old_enabled) != (plugin_id in cfg.enabled)
                or old_settings.get(plugin_id) != cfg.plugin_settings.get(plugin_id)):
            epochs[plugin_id] = uuid.uuid4().hex
    doc["plugin_epochs"] = epochs
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

    _atomic_write(path, tomlkit.dumps(doc))
    cfg.plugin_epochs = epochs
    cfg._rotated_epochs.clear()


def invalidate_plugin_work(plugin_id: str) -> None:
    """Persist a credential transition even when settings have not changed."""
    path = _config_path()
    with _save_lock(path):
        cfg = _load_path(path)
        cfg.rotate_plugin_epoch(plugin_id)
        _save_locked(cfg, path)


def invalidate_all_plugin_work() -> None:
    """Revoke pending plugin work before an interactive Fulcra account change."""
    path = _config_path()
    with _save_lock(path):
        cfg = _load_path(path)
        for plugin_id in cfg.enabled | set(cfg.plugin_settings) | set(cfg.plugin_epochs):
            cfg.rotate_plugin_epoch(plugin_id)
        _save_locked(cfg, path)
