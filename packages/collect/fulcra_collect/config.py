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
from collections.abc import Mapping
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType

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


def _freeze(value):
    """Detach mutable settings and retain an immutable semantic baseline."""
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(value)
    return deepcopy(value)


def _empty_baseline():
    return _freeze(dict(enabled=set(), interval_overrides={}, plugin_settings={},
                        web_port=DEFAULT_WEB_PORT))


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
    account_transition: bool = False
    _rotated_epochs: set[str] = field(default_factory=set, repr=False)
    _baseline: Mapping = field(default_factory=_empty_baseline, init=False,
                               repr=False, compare=False)
    _enabled_intents: set[str] = field(default_factory=set, init=False, repr=False, compare=False)
    _interval_intents: set[str] = field(default_factory=set, init=False, repr=False, compare=False)
    _setting_intents: dict[str, set[str]] = field(default_factory=dict, init=False,
                                                repr=False, compare=False)

    def rotate_plugin_epoch(self, plugin_id: str) -> None:
        """Invalidate pending work after a credential or configuration transition."""
        self.plugin_epochs[plugin_id] = uuid.uuid4().hex
        self._rotated_epochs.add(plugin_id)

    def enable(self, plugin_id: str) -> None:
        self._enabled_intents.add(plugin_id)
        self.enabled.add(plugin_id)

    def disable(self, plugin_id: str) -> None:
        self._enabled_intents.add(plugin_id)
        self.enabled.discard(plugin_id)

    def set_interval(self, plugin_id: str, seconds: int) -> None:
        self._interval_intents.add(plugin_id)
        self.interval_overrides[plugin_id] = seconds

    def update_plugin_settings(self, plugin_id: str, values: Mapping) -> None:
        """Record every explicitly submitted field, including unchanged values."""
        self._setting_intents.setdefault(plugin_id, set()).update(values)
        self.plugin_settings.setdefault(plugin_id, {}).update(deepcopy(dict(values)))


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
    cfg = Config(
        enabled=set(doc.get("enabled", [])),
        interval_overrides=dict(doc.get("interval_overrides", {})),
        plugin_settings=dict(doc.get("plugin_settings", {})),
        web_port=web_port,
        plugin_epochs=dict(doc.get("plugin_epochs", {})),
        account_transition=doc.get("account_transition", False) is not False,
    )
    cfg._baseline = _snapshot(cfg)
    return cfg


def _snapshot(cfg: Config):
    return _freeze({key: getattr(cfg, key) for key in
                    ('enabled', 'interval_overrides', 'plugin_settings', 'web_port')})


class ConfigConflictError(RuntimeError):
    """An edited field changed since this configuration was loaded."""


_MISSING = object()


def _same(left, right):
    return left is right or (left is not _MISSING and right is not _MISSING
                             and _freeze(left) == _freeze(right))


def _merge_value(before, requested, current, location, *, explicit=False):
    if not explicit and _same(requested, before):
        return current
    if _same(current, before) or _same(current, requested):
        return requested
    raise ConfigConflictError(f'Configuration changed at {location}; reload and retry.')


def _merge_mapping(before, requested, current, location, *, nested=False,
                   touched=(), nested_touched=None):
    merged = deepcopy(dict(current))
    for key in before.keys() | requested.keys():
        old, new, now = (values.get(key, _MISSING) for values in
                         (before, requested, current))
        if nested and isinstance(new, Mapping) and isinstance(now, Mapping) and (
                old is _MISSING or isinstance(old, Mapping)):
            value = _merge_mapping({} if old is _MISSING else old, new, now,
                                   f'{location}.{key}',
                                   touched=(nested_touched or {}).get(key, ()))
        else:
            explicit = key in touched or bool((nested_touched or {}).get(key))
            value = _merge_value(old, new, now, f'{location}.{key}', explicit=explicit)
        if value is _MISSING:
            merged.pop(key, None)
        else:
            merged[key] = deepcopy(value)
    return merged


def _merge_settings(cfg: Config, current: Config):
    before = cfg._baseline
    enabled = set(current.enabled)
    for plugin_id in before['enabled'] | cfg.enabled | cfg._enabled_intents:
        value = _merge_value(plugin_id in before['enabled'], plugin_id in cfg.enabled,
                             plugin_id in current.enabled, f'enabled.{plugin_id}',
                             explicit=plugin_id in cfg._enabled_intents)
        (enabled.add if value else enabled.discard)(plugin_id)
    return dict(
        enabled=enabled,
        interval_overrides=_merge_mapping(before['interval_overrides'], cfg.interval_overrides,
                                         current.interval_overrides, 'interval_overrides',
                                         touched=cfg._interval_intents),
        plugin_settings=_merge_mapping(before['plugin_settings'], cfg.plugin_settings,
                                       current.plugin_settings, 'plugin_settings', nested=True,
                                       nested_touched=cfg._setting_intents),
        web_port=_merge_value(before['web_port'], cfg.web_port, current.web_port, 'web_port'),
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


def _save_locked(cfg: Config, path: Path, *, account_transition: bool | None = None) -> None:
    # Read the existing document to preserve any comments and custom
    # sections the user may have added. If the file doesn't exist yet,
    # start from an empty tomlkit document.
    if path.exists():
        doc = tomlkit.parse(path.read_text(encoding="utf-8"))
    else:
        doc = tomlkit.document()

    # Apply only edits relative to the detached load/save baseline. The lock
    # protects this merge as well as publication; unrelated stale saves cannot
    # restore an enabled flag or remove settings another caller just saved.
    merged = _merge_settings(cfg, _load_path(path))

    # Ordinary saves preserve the gate already on disk, even when a stale
    # Config carries False. Only the transition helper passes this override.
    if account_transition is not None:
        doc["account_transition"] = account_transition

    old_enabled = set(doc.get("enabled", []))
    old_settings = dict(doc.get("plugin_settings", {}))
    epochs = dict(doc.get("plugin_epochs", {}))
    # A long-lived daemon Config may predate a credential change. Only
    # explicit rotations may replace the newer value already on disk.
    for plugin_id in cfg._rotated_epochs:
        epochs[plugin_id] = cfg.plugin_epochs[plugin_id]
    for plugin_id in old_enabled | merged['enabled'] | set(old_settings) | set(merged['plugin_settings']):
        if ((plugin_id in old_enabled) != (plugin_id in merged['enabled'])
                or old_settings.get(plugin_id) != merged['plugin_settings'].get(plugin_id)):
            epochs[plugin_id] = uuid.uuid4().hex
    doc["plugin_epochs"] = epochs
    doc["enabled"] = sorted(merged['enabled'])
    doc["interval_overrides"] = merged['interval_overrides']
    doc["plugin_settings"] = merged['plugin_settings']
    # Only persist web_port when it differs from the default — this keeps
    # the default config file small and avoids writing a `[daemon]` table
    # the user never asked for. If the user wrote `web_port = 9292`
    # explicitly we silently drop it; that's fine, the loader uses the
    # default when the field is absent.
    if merged['web_port'] != DEFAULT_WEB_PORT:
        daemon_table = doc.get("daemon")
        if daemon_table is None:
            daemon_table = tomlkit.table()
            doc["daemon"] = daemon_table
        daemon_table["web_port"] = merged['web_port']
    elif 'daemon' in doc:
        doc['daemon'].pop('web_port', None)

    _atomic_write(path, tomlkit.dumps(doc))
    for key, value in merged.items():
        setattr(cfg, key, deepcopy(value))
    cfg._baseline = _snapshot(cfg)
    cfg.plugin_epochs = epochs
    cfg.account_transition = doc.get("account_transition", False) is not False
    cfg._rotated_epochs.clear()
    cfg._enabled_intents.clear()
    cfg._interval_intents.clear()
    cfg._setting_intents.clear()


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


def _persist_account_transition(path: Path, blocked: bool) -> None:
    with _save_lock(path):
        cfg = _load_path(path)
        for plugin_id in cfg.enabled | set(cfg.plugin_settings) | set(cfg.plugin_epochs):
            cfg.rotate_plugin_epoch(plugin_id)
        _save_locked(cfg, path, account_transition=blocked)


@contextmanager
def fulcra_account_transition():
    """Gate task writes across an interactive token change and revoke both epochs.

    A separate transition lock serializes overlapping account changes. The config
    save lock is released during token mutation, so Keychain UI cannot block
    config reads or ordinary settings saves. Failure (including process exit) leaves
    the durable gate closed until a later interactive transition completes.
    """
    path = _config_path()
    with _save_lock(path.with_name(".account-transition.toml")):
        _persist_account_transition(path, True)
        yield
        # This runs only after successful mutation. If this final write fails,
        # atomic replacement leaves the previously persisted True gate intact.
        _persist_account_transition(path, False)
