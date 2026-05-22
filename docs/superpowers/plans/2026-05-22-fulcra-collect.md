# fulcra-collect Implementation Plan (1a — core + 3 reference adapters)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the headless `fulcra-collect` hub core — a background daemon that discovers helper plugins, schedules periodic imports, supervises long-running services, and exposes status/control over a local socket — plus one reference plugin of each kind (attention-relay, lastfm, dayone).

**Architecture:** One long-lived core process (`fulcra-collect daemon`) holding a plugin registry, scheduler, service supervisor, config, keychain credentials, per-plugin state, and a Unix-domain-socket control server. Plugin runs execute in isolated worker subprocesses that stream JSON-line progress back. Plugins are discovered via the `fulcra_collect.plugins` entry-point group.

**Tech Stack:** Python 3.11+, `click` (CLI), `keyring` (OS keychain), `tomllib`/`tomli-w` (config), stdlib `socket`/`subprocess`/`importlib.metadata`, `pytest`, the `uv` workspace.

**Spec:** `docs/superpowers/specs/2026-05-22-fulcra-collect-design.md`

**Scope note:** This is plan **1a**. It builds the core and three reference adapters (one service, one scheduled, one manual) to prove the plugin contract end-to-end. Plan **1b** adapts the remaining ~14 importers using the pattern this plan establishes.

---

## File Structure

New package `packages/collect/`:

| File | Responsibility |
|---|---|
| `pyproject.toml` | Package metadata, workspace deps. |
| `README.md` | Usage docs. |
| `fulcra_collect/__init__.py` | Package marker. |
| `fulcra_collect/__main__.py` | `python -m fulcra_collect` → the CLI. |
| `fulcra_collect/plugin.py` | The plugin API types: `Plugin`, `Permission`, `Credential`, `RunContext`, `PluginKind`. |
| `fulcra_collect/config.py` | The hub TOML config + the config-directory location. |
| `fulcra_collect/credentials.py` | `keyring` wrapper, namespaced per plugin. |
| `fulcra_collect/state.py` | Per-plugin persisted state (`PluginState`). |
| `fulcra_collect/registry.py` | Entry-point plugin discovery + validation. |
| `fulcra_collect/worker.py` | The worker-subprocess entrypoint. |
| `fulcra_collect/runner.py` | Spawn + supervise one run; record outcome. |
| `fulcra_collect/scheduler.py` | Pure "which scheduled plugins are due" logic. |
| `fulcra_collect/supervisor.py` | Pure service restart-decision logic. |
| `fulcra_collect/control.py` | The Unix-domain-socket control server + client. |
| `fulcra_collect/daemon.py` | Wires the core into the run loop. |
| `fulcra_collect/service_manager.py` | launchd/systemd installer for the daemon. |
| `fulcra_collect/cli.py` | The `fulcra-collect` Click CLI. |
| `tests/` | One test module per source module. |

Adapter plugins (added to existing packages, each registering an entry point):

| File | Responsibility |
|---|---|
| `packages/attention/fulcra_attention/collect_plugin.py` | The `attention-relay` service plugin. |
| `packages/media-helpers/fulcra_media/collect_plugins.py` | The `lastfm` scheduled plugin. |
| `packages/dayone/fulcra_dayone/collect_plugin.py` | The `dayone` manual plugin. |

All commands run from the monorepo root `/Users/Scanning/Developer/fulcra-tools`.

---

### Task 1: Scaffold the fulcra-collect package

**Files:**
- Create: `packages/collect/pyproject.toml`
- Create: `packages/collect/fulcra_collect/__init__.py`
- Create: `packages/collect/fulcra_collect/__main__.py`
- Create: `packages/collect/tests/__init__.py`
- Create: `packages/collect/tests/conftest.py`

- [ ] **Step 1: Create `packages/collect/pyproject.toml`**

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "fulcra-collect"
version = "0.1.0"
description = "Background hub that hosts the Fulcra local helpers as plugins."
requires-python = ">=3.11"
dependencies = [
    "click>=8.1",
    "keyring>=25",
    "tomli-w>=1.0",
]

[project.scripts]
fulcra-collect = "fulcra_collect.cli:cli"

[project.optional-dependencies]
dev = [
    "pytest>=7.4,<8",
    "ruff>=0.5",
]

[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-ra --strict-markers"

[tool.hatch.build.targets.wheel]
packages = ["fulcra_collect"]
```

- [ ] **Step 2: Create `packages/collect/fulcra_collect/__init__.py`**

```python
"""Background hub that hosts the Fulcra local helpers as plugins."""
```

- [ ] **Step 3: Create `packages/collect/fulcra_collect/__main__.py`**

```python
"""Enable `python -m fulcra_collect` — used to spawn worker subprocesses
with a PATH-independent interpreter path."""
from fulcra_collect.cli import cli

if __name__ == "__main__":
    cli()
```

- [ ] **Step 4: Create `packages/collect/tests/__init__.py`**

Empty file (makes `tests` an importable package so `from tests.conftest import ...` works):

```python
```

- [ ] **Step 5: Create `packages/collect/tests/conftest.py`**

```python
"""Shared test fixtures for fulcra-collect."""
from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def collect_home(tmp_path: Path, monkeypatch) -> Path:
    """Point the hub's config directory at a temp dir for the test."""
    home = tmp_path / "collect-home"
    home.mkdir()
    monkeypatch.setenv("FULCRA_COLLECT_HOME", str(home))
    return home
```

- [ ] **Step 6: Sync the workspace**

Run: `uv sync --all-extras`
Expected: success; output includes `fulcra-collect==0.1.0`. The root `pyproject.toml` already declares `members = ["packages/*"]`, so no root change is needed.

- [ ] **Step 7: Verify the package imports**

Run: `uv run --package fulcra-collect python -c "import fulcra_collect; print('ok')"`
Expected: prints `ok`.

- [ ] **Step 8: Commit**

```bash
git add packages/collect/pyproject.toml packages/collect/fulcra_collect packages/collect/tests uv.lock
git commit -m "chore(collect): scaffold the fulcra-collect package"
```

---

### Task 2: The plugin API types

**Files:**
- Create: `packages/collect/fulcra_collect/plugin.py`
- Test: `packages/collect/tests/test_plugin.py`

- [ ] **Step 1: Write the failing test**

Create `packages/collect/tests/test_plugin.py`:

```python
"""The plugin API types."""
from __future__ import annotations

from datetime import timedelta

import pytest

from fulcra_collect.plugin import Credential, Permission, Plugin


def _noop(ctx) -> None:
    pass


def test_scheduled_plugin_requires_default_interval():
    with pytest.raises(ValueError, match="default_interval"):
        Plugin(id="x", name="X", kind="scheduled", run=_noop)


def test_non_scheduled_plugin_rejects_default_interval():
    with pytest.raises(ValueError, match="default_interval"):
        Plugin(id="x", name="X", kind="manual", run=_noop,
               default_interval=timedelta(hours=1))


def test_unknown_kind_rejected():
    with pytest.raises(ValueError, match="kind"):
        Plugin(id="x", name="X", kind="weekly", run=_noop)


def test_valid_plugins_of_each_kind():
    svc = Plugin(id="relay", name="Relay", kind="service", run=_noop)
    sch = Plugin(id="lastfm", name="Last.fm", kind="scheduled", run=_noop,
                 default_interval=timedelta(hours=1))
    man = Plugin(id="dayone", name="Day One", kind="manual", run=_noop)
    assert svc.kind == "service"
    assert sch.default_interval == timedelta(hours=1)
    assert man.kind == "manual"


def test_permission_and_credential_are_simple_records():
    p = Permission(id="full-disk-access", explanation="needed to read the DB")
    c = Credential(key="lastfm-api-key", label="Last.fm API key",
                   help="https://www.last.fm/api/account/create")
    assert p.id == "full-disk-access"
    assert c.key == "lastfm-api-key"


def test_requires_network_defaults_true_and_is_overridable():
    online = Plugin(id="x", name="X", kind="manual", run=_noop)
    offline_ok = Plugin(id="y", name="Y", kind="manual", run=_noop,
                        requires_network=False)
    assert online.requires_network is True
    assert offline_ok.requires_network is False
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run --package fulcra-collect pytest packages/collect/tests/test_plugin.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'fulcra_collect.plugin'`.

- [ ] **Step 3: Create `packages/collect/fulcra_collect/plugin.py`**

```python
"""The fulcra-collect plugin API.

A plugin is a `Plugin` object discovered via the `fulcra_collect.plugins`
entry-point group. It declares metadata and a `run(ctx)` callable. The
hub builds the `RunContext` and supplies config, credentials, and state —
a plugin never reaches for those itself.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Literal

PluginKind = Literal["service", "scheduled", "manual"]
_KINDS = ("service", "scheduled", "manual")


@dataclass(frozen=True)
class Permission:
    """An OS permission a plugin needs. `explanation` is shown to the user
    by the sub-project-2 onboarding flow."""
    id: str
    explanation: str


@dataclass(frozen=True)
class Credential:
    """A secret a plugin needs. Stored in the OS keychain by the hub."""
    key: str
    label: str
    help: str


@dataclass(frozen=True)
class Plugin:
    """A hub plugin: metadata plus a `run(ctx)` callable.

    kind:
      "service"   — run(ctx) blocks (a long-lived server); supervised.
      "scheduled" — run(ctx) does one import pass; fired on default_interval.
      "manual"    — run(ctx) does one import pass; fired only on request.

    requires_network: when True (the default), the daemon skips this
    plugin's scheduled dispatch while the machine is offline — deferring
    it rather than running it into a guaranteed failure.
    """
    id: str
    name: str
    kind: PluginKind
    run: Callable[["RunContext"], None]
    default_interval: timedelta | None = None
    requires_network: bool = True
    required_permissions: tuple[Permission, ...] = ()
    required_credentials: tuple[Credential, ...] = ()

    def __post_init__(self) -> None:
        if self.kind not in _KINDS:
            raise ValueError(f"unknown kind {self.kind!r}; expected one of {_KINDS}")
        if self.kind == "scheduled" and self.default_interval is None:
            raise ValueError("scheduled plugin requires a default_interval")
        if self.kind != "scheduled" and self.default_interval is not None:
            raise ValueError("default_interval is only valid for a scheduled plugin")


@dataclass
class RunContext:
    """Passed into `Plugin.run`. The hub builds it in the worker process."""
    plugin_id: str
    config: dict
    credentials: dict[str, str]
    state: "object"        # a PluginState (fulcra_collect.state) — duck-typed here
    log: logging.Logger
    _emit: Callable[[dict], None] = field(repr=False)

    def progress(self, **fields: object) -> None:
        """Report structured progress back to the hub core."""
        self._emit({"type": "progress", **fields})

    def fulcra_token(self) -> str:
        """The Fulcra access token, via the existing fulcra-api auth path."""
        from fulcra_common import BaseFulcraClient
        return BaseFulcraClient().get_token()
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run --package fulcra-collect pytest packages/collect/tests/test_plugin.py -v`
Expected: PASS — 6 tests.

- [ ] **Step 5: Commit**

```bash
git add packages/collect/fulcra_collect/plugin.py packages/collect/tests/test_plugin.py
git commit -m "feat(collect): plugin API types"
```

---

### Task 3: Config — directory location and the TOML config file

**Files:**
- Create: `packages/collect/fulcra_collect/config.py`
- Test: `packages/collect/tests/test_config.py`

- [ ] **Step 1: Write the failing test**

Create `packages/collect/tests/test_config.py`:

```python
"""Hub config + config directory."""
from __future__ import annotations

from pathlib import Path

from fulcra_collect import config


def test_config_dir_honours_the_env_override(collect_home: Path):
    assert config.config_dir() == collect_home


def test_load_returns_empty_config_when_no_file(collect_home: Path):
    cfg = config.load()
    assert cfg.enabled == set()
    assert cfg.interval_overrides == {}
    assert cfg.plugin_settings == {}


def test_enable_disable_round_trip(collect_home: Path):
    cfg = config.load()
    cfg.enable("lastfm")
    cfg.enable("dayone")
    cfg.disable("dayone")
    config.save(cfg)
    reloaded = config.load()
    assert reloaded.enabled == {"lastfm"}


def test_interval_override_round_trip(collect_home: Path):
    cfg = config.load()
    cfg.set_interval("lastfm", 1800)
    config.save(cfg)
    assert config.load().interval_overrides == {"lastfm": 1800}


def test_plugin_settings_round_trip(collect_home: Path):
    cfg = config.load()
    cfg.plugin_settings["dayone"] = {"local_db": True}
    config.save(cfg)
    assert config.load().plugin_settings["dayone"] == {"local_db": True}
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run --package fulcra-collect pytest packages/collect/tests/test_config.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'fulcra_collect.config'`.

- [ ] **Step 3: Create `packages/collect/fulcra_collect/config.py`**

```python
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
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run --package fulcra-collect pytest packages/collect/tests/test_config.py -v`
Expected: PASS — 5 tests.

- [ ] **Step 5: Commit**

```bash
git add packages/collect/fulcra_collect/config.py packages/collect/tests/test_config.py
git commit -m "feat(collect): config directory + TOML config file"
```

---

### Task 4: Credentials — the keychain wrapper

**Files:**
- Create: `packages/collect/fulcra_collect/credentials.py`
- Test: `packages/collect/tests/test_credentials.py`

- [ ] **Step 1: Write the failing test**

Create `packages/collect/tests/test_credentials.py`:

```python
"""Keychain-backed credential storage."""
from __future__ import annotations

import keyring
import pytest
from keyring.backend import KeyringBackend


class InMemoryKeyring(KeyringBackend):
    """A keyring backend that stores secrets in a dict — for tests only."""
    priority = 1

    def __init__(self) -> None:
        super().__init__()
        self._store: dict[tuple[str, str], str] = {}

    def get_password(self, service, username):
        return self._store.get((service, username))

    def set_password(self, service, username, password):
        self._store[(service, username)] = password

    def delete_password(self, service, username):
        self._store.pop((service, username), None)


@pytest.fixture(autouse=True)
def _in_memory_keyring(monkeypatch):
    backend = InMemoryKeyring()
    monkeypatch.setattr(keyring, "get_keyring", lambda: backend)
    monkeypatch.setattr(keyring, "set_password", backend.set_password)
    monkeypatch.setattr(keyring, "get_password", backend.get_password)
    monkeypatch.setattr(keyring, "delete_password", backend.delete_password)
    return backend


def test_set_then_get_round_trips():
    from fulcra_collect import credentials
    credentials.set_secret("lastfm", "api-key", "SECRET123")
    assert credentials.get_secret("lastfm", "api-key") == "SECRET123"


def test_get_missing_returns_none():
    from fulcra_collect import credentials
    assert credentials.get_secret("lastfm", "absent") is None


def test_delete_removes_the_secret():
    from fulcra_collect import credentials
    credentials.set_secret("lastfm", "api-key", "SECRET123")
    credentials.delete_secret("lastfm", "api-key")
    assert credentials.get_secret("lastfm", "api-key") is None


def test_secrets_are_namespaced_per_plugin():
    from fulcra_collect import credentials
    credentials.set_secret("lastfm", "token", "A")
    credentials.set_secret("trakt", "token", "B")
    assert credentials.get_secret("lastfm", "token") == "A"
    assert credentials.get_secret("trakt", "token") == "B"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run --package fulcra-collect pytest packages/collect/tests/test_credentials.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'fulcra_collect.credentials'`.

- [ ] **Step 3: Create `packages/collect/fulcra_collect/credentials.py`**

```python
"""Plugin secrets, stored in the OS keychain via `keyring`.

Each secret is keyed by (plugin_id, credential key). The keyring service
name namespaces every entry under this app so it is distinct from any
other keychain item.
"""
from __future__ import annotations

import keyring

_SERVICE_PREFIX = "fulcra-collect"


def _service(plugin_id: str) -> str:
    return f"{_SERVICE_PREFIX}:{plugin_id}"


def set_secret(plugin_id: str, key: str, value: str) -> None:
    keyring.set_password(_service(plugin_id), key, value)


def get_secret(plugin_id: str, key: str) -> str | None:
    return keyring.get_password(_service(plugin_id), key)


def delete_secret(plugin_id: str, key: str) -> None:
    keyring.delete_password(_service(plugin_id), key)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run --package fulcra-collect pytest packages/collect/tests/test_credentials.py -v`
Expected: PASS — 4 tests.

- [ ] **Step 5: Commit**

```bash
git add packages/collect/fulcra_collect/credentials.py packages/collect/tests/test_credentials.py
git commit -m "feat(collect): keychain-backed credential storage"
```

---

### Task 5: Per-plugin state

**Files:**
- Create: `packages/collect/fulcra_collect/state.py`
- Test: `packages/collect/tests/test_state.py`

- [ ] **Step 1: Write the failing test**

Create `packages/collect/tests/test_state.py`:

```python
"""Per-plugin persisted state."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from fulcra_collect import state


def test_load_returns_fresh_state_when_no_file(collect_home: Path):
    st = state.load("lastfm")
    assert st.plugin_id == "lastfm"
    assert st.last_run is None
    assert st.consecutive_failures == 0
    assert st.watermark is None


def test_record_success_resets_failures_and_sets_outcome(collect_home: Path):
    st = state.load("lastfm")
    st.consecutive_failures = 3
    when = datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc)
    st.record_finish(outcome="done", when=when)
    assert st.last_outcome == "done"
    assert st.last_run == when
    assert st.last_error is None
    assert st.consecutive_failures == 0


def test_record_error_increments_failures(collect_home: Path):
    st = state.load("lastfm")
    when = datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc)
    st.record_finish(outcome="error", when=when, error="boom")
    st.record_finish(outcome="error", when=when, error="boom again")
    assert st.consecutive_failures == 2
    assert st.last_error == "boom again"


def test_state_round_trips_through_disk(collect_home: Path):
    st = state.load("lastfm")
    when = datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc)
    st.record_finish(outcome="done", when=when)
    st.watermark = "2026-05-22T11:59:00Z"
    state.save(st)
    reloaded = state.load("lastfm")
    assert reloaded.last_outcome == "done"
    assert reloaded.last_run == when
    assert reloaded.watermark == "2026-05-22T11:59:00Z"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run --package fulcra-collect pytest packages/collect/tests/test_state.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'fulcra_collect.state'`.

- [ ] **Step 3: Create `packages/collect/fulcra_collect/state.py`**

```python
"""Per-plugin persisted state — last run, last outcome, failure count,
and the plugin's own watermark string. One JSON file per plugin under
the hub state directory. This is the snapshot the CLI and the UI read.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from .config import config_dir


def _state_dir() -> Path:
    d = config_dir() / "state"
    d.mkdir(parents=True, exist_ok=True)
    return d


@dataclass
class PluginState:
    plugin_id: str
    last_run: datetime | None = None
    last_outcome: str | None = None      # "done" | "error" | "timeout"
    last_error: str | None = None
    consecutive_failures: int = 0
    watermark: str | None = None         # ISO string, plugin-defined

    def record_finish(self, *, outcome: str, when: datetime,
                       error: str | None = None) -> None:
        """Record a finished run. A non-"done" outcome increments the
        consecutive-failure count; "done" resets it."""
        self.last_run = when
        self.last_outcome = outcome
        self.last_error = error
        if outcome == "done":
            self.consecutive_failures = 0
        else:
            self.consecutive_failures += 1


def load(plugin_id: str) -> PluginState:
    path = _state_dir() / f"{plugin_id}.json"
    if not path.exists():
        return PluginState(plugin_id=plugin_id)
    doc = json.loads(path.read_text(encoding="utf-8"))
    lr = doc.get("last_run")
    return PluginState(
        plugin_id=plugin_id,
        last_run=datetime.fromisoformat(lr) if lr else None,
        last_outcome=doc.get("last_outcome"),
        last_error=doc.get("last_error"),
        consecutive_failures=doc.get("consecutive_failures", 0),
        watermark=doc.get("watermark"),
    )


def save(st: PluginState) -> None:
    doc = asdict(st)
    doc["last_run"] = st.last_run.isoformat() if st.last_run else None
    path = _state_dir() / f"{st.plugin_id}.json"
    path.write_text(json.dumps(doc, indent=2, sort_keys=True), encoding="utf-8")
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run --package fulcra-collect pytest packages/collect/tests/test_state.py -v`
Expected: PASS — 4 tests.

- [ ] **Step 5: Commit**

```bash
git add packages/collect/fulcra_collect/state.py packages/collect/tests/test_state.py
git commit -m "feat(collect): per-plugin persisted state"
```

---

### Task 6: The plugin registry

**Files:**
- Create: `packages/collect/fulcra_collect/registry.py`
- Test: `packages/collect/tests/test_registry.py`

The registry has two layers: a pure `load_plugins(entries)` that takes an
iterable of entry-point-like objects (each with `.name` and `.load()`),
and `discover()` which feeds it the real `fulcra_collect.plugins`
entry-point group. Tests drive the pure layer with fakes.

- [ ] **Step 1: Write the failing test**

Create `packages/collect/tests/test_registry.py`:

```python
"""Plugin discovery + validation."""
from __future__ import annotations

from datetime import timedelta

from fulcra_collect.plugin import Plugin
from fulcra_collect.registry import RegistryResult, load_plugins


class FakeEntry:
    """Stands in for an importlib.metadata EntryPoint."""
    def __init__(self, name, loader):
        self.name = name
        self._loader = loader

    def load(self):
        return self._loader()


def _plugin(pid):
    return Plugin(id=pid, name=pid, kind="manual", run=lambda ctx: None)


def test_load_plugins_collects_valid_plugins():
    entries = [
        FakeEntry("a", lambda: _plugin("a")),
        FakeEntry("b", lambda: _plugin("b")),
    ]
    result = load_plugins(entries)
    assert set(result.plugins) == {"a", "b"}
    assert result.errors == {}


def test_a_plugin_factory_callable_is_also_accepted():
    # An entry point may resolve to a Plugin OR a zero-arg callable -> Plugin.
    entries = [FakeEntry("a", lambda: (lambda: _plugin("a")))]
    result = load_plugins(entries)
    assert "a" in result.plugins


def test_an_entry_that_raises_on_load_is_recorded_not_fatal():
    def boom():
        raise RuntimeError("bad import")
    entries = [
        FakeEntry("good", lambda: _plugin("good")),
        FakeEntry("bad", boom),
    ]
    result = load_plugins(entries)
    assert set(result.plugins) == {"good"}
    assert "bad" in result.errors
    assert "bad import" in result.errors["bad"]


def test_an_entry_resolving_to_a_non_plugin_is_recorded():
    entries = [FakeEntry("notaplugin", lambda: "just a string")]
    result = load_plugins(entries)
    assert result.plugins == {}
    assert "notaplugin" in result.errors


def test_duplicate_plugin_ids_keep_the_first_and_record_the_clash():
    entries = [
        FakeEntry("x", lambda: _plugin("dup")),
        FakeEntry("y", lambda: _plugin("dup")),
    ]
    result = load_plugins(entries)
    assert set(result.plugins) == {"dup"}
    assert any("dup" in e for e in result.errors.values())
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run --package fulcra-collect pytest packages/collect/tests/test_registry.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'fulcra_collect.registry'`.

- [ ] **Step 3: Create `packages/collect/fulcra_collect/registry.py`**

```python
"""Plugin discovery. Plugins register under the `fulcra_collect.plugins`
entry-point group; each entry resolves to a `Plugin` (or a zero-arg
callable returning one). A plugin that fails to load, resolves to a
non-Plugin, or collides on id is excluded and recorded — never fatal.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from importlib.metadata import entry_points

from .plugin import Plugin

ENTRY_POINT_GROUP = "fulcra_collect.plugins"


@dataclass
class RegistryResult:
    plugins: dict[str, Plugin] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)  # entry name -> message


def load_plugins(entries) -> RegistryResult:
    """Resolve an iterable of entry-point-like objects (each with `.name`
    and `.load()`) into a RegistryResult."""
    result = RegistryResult()
    for entry in entries:
        try:
            obj = entry.load()
            if callable(obj) and not isinstance(obj, Plugin):
                obj = obj()
            if not isinstance(obj, Plugin):
                raise TypeError(f"entry {entry.name!r} resolved to {type(obj).__name__}, "
                                "expected a Plugin")
            if obj.id in result.plugins:
                result.errors[entry.name] = f"duplicate plugin id {obj.id!r}"
                continue
            result.plugins[obj.id] = obj
        except Exception as exc:  # noqa: BLE001 — a bad plugin must not crash the hub
            result.errors[entry.name] = f"{type(exc).__name__}: {exc}"
    return result


def discover() -> RegistryResult:
    """Discover plugins from the real entry-point group."""
    return load_plugins(entry_points(group=ENTRY_POINT_GROUP))
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run --package fulcra-collect pytest packages/collect/tests/test_registry.py -v`
Expected: PASS — 5 tests.

- [ ] **Step 5: Commit**

```bash
git add packages/collect/fulcra_collect/registry.py packages/collect/tests/test_registry.py
git commit -m "feat(collect): entry-point plugin registry"
```

---

### Task 7: The worker subprocess entrypoint

**Files:**
- Create: `packages/collect/fulcra_collect/worker.py`
- Test: `packages/collect/tests/test_worker.py`

The worker runs as `python -m fulcra_collect _worker <plugin-id>`. It
discovers the plugin, builds the `RunContext`, calls `run`, and writes
JSON-line events to stdout: zero or more `{"type":"progress",...}` lines,
then exactly one `{"type":"result","outcome":...}` line.

- [ ] **Step 1: Write the failing test**

Create `packages/collect/tests/test_worker.py`:

```python
"""The worker entrypoint — runs one plugin, emits JSON-line events."""
from __future__ import annotations

import io
import json
from pathlib import Path

from fulcra_collect import worker
from fulcra_collect.plugin import Plugin
from fulcra_collect.registry import RegistryResult


def _run_capturing(plugin: Plugin, collect_home: Path) -> list[dict]:
    """Run a plugin through the worker, return the emitted JSON events."""
    buf = io.StringIO()
    worker.run_plugin(plugin, out=buf)
    return [json.loads(line) for line in buf.getvalue().splitlines() if line]


def test_worker_emits_a_done_result_for_a_successful_run(collect_home: Path):
    plugin = Plugin(id="ok", name="OK", kind="manual", run=lambda ctx: None)
    events = _run_capturing(plugin, collect_home)
    assert events[-1] == {"type": "result", "outcome": "done",
                          "error": None, "watermark": None}


def test_worker_carries_the_watermark_set_by_the_plugin(collect_home: Path):
    def run(ctx):
        ctx.state.watermark = "2026-05-22T12:00:00Z"
    plugin = Plugin(id="wm", name="WM", kind="manual", run=run)
    events = _run_capturing(plugin, collect_home)
    assert events[-1]["watermark"] == "2026-05-22T12:00:00Z"


def test_worker_forwards_progress_events(collect_home: Path):
    def run(ctx):
        ctx.progress(done=1, total=3)
        ctx.progress(done=3, total=3)
    plugin = Plugin(id="p", name="P", kind="manual", run=run)
    events = _run_capturing(plugin, collect_home)
    progress = [e for e in events if e["type"] == "progress"]
    assert progress == [
        {"type": "progress", "done": 1, "total": 3},
        {"type": "progress", "done": 3, "total": 3},
    ]
    assert events[-1]["outcome"] == "done"


def test_worker_emits_an_error_result_when_run_raises(collect_home: Path):
    def run(ctx):
        raise RuntimeError("kaboom")
    plugin = Plugin(id="bad", name="Bad", kind="manual", run=run)
    events = _run_capturing(plugin, collect_home)
    assert events[-1]["type"] == "result"
    assert events[-1]["outcome"] == "error"
    assert "kaboom" in events[-1]["error"]


def test_main_reports_unknown_plugin_id(collect_home: Path, capsys):
    rc = worker.main(["no-such-plugin"], registry=RegistryResult())
    captured = capsys.readouterr()
    last = [l for l in captured.out.splitlines() if l][-1]
    import json as _json
    assert _json.loads(last)["outcome"] == "error"
    assert rc == 1
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run --package fulcra-collect pytest packages/collect/tests/test_worker.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'fulcra_collect.worker'`.

- [ ] **Step 3: Create `packages/collect/fulcra_collect/worker.py`**

```python
"""The worker subprocess: run one plugin, stream JSON-line events.

Invoked as `python -m fulcra_collect _worker <plugin-id>`. Writes zero or
more {"type":"progress",...} lines then exactly one
{"type":"result","outcome":"done"|"error",...} line to stdout. Runs in
its own process so a plugin's crash, hang, or dependencies are isolated.
"""
from __future__ import annotations

import json
import logging
import sys
import traceback
from typing import TextIO

from . import config, credentials, state
from .plugin import Plugin, RunContext
from .registry import RegistryResult, discover


def run_plugin(plugin: Plugin, *, out: TextIO) -> str:
    """Run one plugin, emitting JSON-line events to `out`. Returns the
    outcome ("done" | "error")."""
    def emit(event: dict) -> None:
        out.write(json.dumps(event) + "\n")
        out.flush()

    cfg = config.load()
    ctx = RunContext(
        plugin_id=plugin.id,
        config=cfg.plugin_settings.get(plugin.id, {}),
        credentials={
            c.key: credentials.get_secret(plugin.id, c.key)
            for c in plugin.required_credentials
        },
        state=state.load(plugin.id),
        log=logging.getLogger(f"fulcra_collect.plugin.{plugin.id}"),
        _emit=emit,
    )
    try:
        plugin.run(ctx)
    except Exception as exc:  # noqa: BLE001 — report, never propagate
        # The watermark is reported even on error: a plugin may advance it
        # partway through a run, and a partial advance must still persist.
        emit({"type": "result", "outcome": "error",
              "error": f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}",
              "watermark": getattr(ctx.state, "watermark", None)})
        return "error"
    # The plugin advanced ctx.state.watermark in this (worker) process; the
    # runner — the single state-writer in the core — persists it from here.
    emit({"type": "result", "outcome": "done", "error": None,
          "watermark": getattr(ctx.state, "watermark", None)})
    return "done"


def main(argv: list[str], *, registry: RegistryResult | None = None) -> int:
    """CLI entry for `_worker <plugin-id>`. Returns a process exit code."""
    reg = registry if registry is not None else discover()
    plugin_id = argv[0] if argv else ""
    plugin = reg.plugins.get(plugin_id)
    if plugin is None:
        sys.stdout.write(json.dumps({
            "type": "result", "outcome": "error",
            "error": f"unknown plugin id {plugin_id!r}",
        }) + "\n")
        return 1
    outcome = run_plugin(plugin, out=sys.stdout)
    return 0 if outcome == "done" else 1
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run --package fulcra-collect pytest packages/collect/tests/test_worker.py -v`
Expected: PASS — 5 tests.

- [ ] **Step 5: Commit**

```bash
git add packages/collect/fulcra_collect/worker.py packages/collect/tests/test_worker.py
git commit -m "feat(collect): worker subprocess entrypoint"
```

---

### Task 8: The runner — spawn a worker, record the outcome

**Files:**
- Create: `packages/collect/fulcra_collect/runner.py`
- Test: `packages/collect/tests/test_runner.py`

- [ ] **Step 1: Write the failing test**

Create `packages/collect/tests/test_runner.py`:

```python
"""The runner — spawns a worker subprocess for one run, records outcome."""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

from fulcra_collect import runner, state


def _python_worker(script: str) -> list[str]:
    """A command that runs `script` as the worker (emits its own JSON lines)."""
    return [sys.executable, "-c", script]


def test_runner_records_a_done_outcome(collect_home: Path):
    script = (
        "import json,sys;"
        "sys.stdout.write(json.dumps({'type':'result','outcome':'done','error':None})+chr(10))"
    )
    outcome = runner.run("p", _python_worker(script),
                         now=datetime(2026, 5, 22, tzinfo=timezone.utc))
    assert outcome == "done"
    st = state.load("p")
    assert st.last_outcome == "done"
    assert st.consecutive_failures == 0


def test_runner_records_an_error_outcome(collect_home: Path):
    script = (
        "import json,sys;"
        "sys.stdout.write(json.dumps({'type':'result','outcome':'error','error':'boom'})+chr(10))"
    )
    outcome = runner.run("p", _python_worker(script),
                         now=datetime(2026, 5, 22, tzinfo=timezone.utc))
    assert outcome == "error"
    st = state.load("p")
    assert st.last_outcome == "error"
    assert st.last_error == "boom"
    assert st.consecutive_failures == 1


def test_runner_times_out_a_hung_worker(collect_home: Path):
    script = "import time; time.sleep(30)"
    outcome = runner.run("p", _python_worker(script),
                         now=datetime(2026, 5, 22, tzinfo=timezone.utc),
                         timeout_s=1.0)
    assert outcome == "timeout"
    assert state.load("p").last_outcome == "timeout"


def test_runner_treats_a_worker_that_emits_no_result_as_error(collect_home: Path):
    script = "pass"  # exits cleanly but emits nothing
    outcome = runner.run("p", _python_worker(script),
                         now=datetime(2026, 5, 22, tzinfo=timezone.utc))
    assert outcome == "error"


def test_runner_persists_the_watermark_from_the_result(collect_home: Path):
    script = (
        "import json,sys;"
        "sys.stdout.write(json.dumps({'type':'result','outcome':'done',"
        "'error':None,'watermark':'2026-05-22T12:00:00Z'})+chr(10))"
    )
    runner.run("p", _python_worker(script),
               now=datetime(2026, 5, 22, tzinfo=timezone.utc))
    assert state.load("p").watermark == "2026-05-22T12:00:00Z"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run --package fulcra-collect pytest packages/collect/tests/test_runner.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'fulcra_collect.runner'`.

- [ ] **Step 3: Create `packages/collect/fulcra_collect/runner.py`**

```python
"""Execute one plugin run in a worker subprocess and record the outcome.

The runner spawns the worker, reads its JSON-line event stream, enforces
a per-run timeout, and writes the result to the plugin's PluginState.
"""
from __future__ import annotations

import json
import subprocess
from datetime import datetime

from . import state

DEFAULT_TIMEOUT_S = 15 * 60


def worker_command(plugin_id: str) -> list[str]:
    """The command that runs the worker for `plugin_id`. Uses the current
    interpreter via `-m` so it works under a launchd/systemd minimal PATH."""
    import sys
    return [sys.executable, "-m", "fulcra_collect", "_worker", plugin_id]


def run(plugin_id: str, command: list[str], *, now: datetime,
        timeout_s: float = DEFAULT_TIMEOUT_S) -> str:
    """Run one plugin via `command`, record the outcome, return it
    ("done" | "error" | "timeout")."""
    outcome = "error"
    error: str | None = "worker emitted no result"
    watermark: str | None = None
    try:
        proc = subprocess.run(
            command, capture_output=True, text=True, timeout=timeout_s,
        )
        for line in proc.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "result":
                outcome = event.get("outcome", "error")
                error = event.get("error")
                watermark = event.get("watermark")
    except subprocess.TimeoutExpired:
        outcome = "timeout"
        error = f"worker exceeded {timeout_s:.0f}s"

    st = state.load(plugin_id)
    # Persist the watermark the plugin advanced in the worker process. The
    # runner is the single writer of plugin state in the core process, so
    # the watermark crosses the worker boundary via the result event.
    if watermark is not None:
        st.watermark = watermark
    st.record_finish(outcome=outcome, when=now, error=error)
    state.save(st)
    return outcome
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run --package fulcra-collect pytest packages/collect/tests/test_runner.py -v`
Expected: PASS — 5 tests.

- [ ] **Step 5: Commit**

```bash
git add packages/collect/fulcra_collect/runner.py packages/collect/tests/test_runner.py
git commit -m "feat(collect): run executor with timeout + outcome recording"
```

---

### Task 9: The scheduler

**Files:**
- Create: `packages/collect/fulcra_collect/scheduler.py`
- Test: `packages/collect/tests/test_scheduler.py`

`due_plugins` is a pure function: given the scheduled plugins, their
state, the config, and the current time, return the ids that should run
now. The daemon loop calls it; the unit tests drive it with fixed times.

- [ ] **Step 1: Write the failing test**

Create `packages/collect/tests/test_scheduler.py`:

```python
"""The scheduler — pure 'which scheduled plugins are due' logic."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fulcra_collect.config import Config
from fulcra_collect.plugin import Plugin
from fulcra_collect.scheduler import due_plugins
from fulcra_collect.state import PluginState

T0 = datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc)


def _scheduled(pid, hours):
    return Plugin(id=pid, name=pid, kind="scheduled", run=lambda ctx: None,
                  default_interval=timedelta(hours=hours))


def test_a_never_run_enabled_plugin_is_due():
    plugins = {"lastfm": _scheduled("lastfm", 1)}
    cfg = Config(enabled={"lastfm"})
    due = due_plugins(plugins, cfg, states={}, now=T0)
    assert due == ["lastfm"]


def test_a_disabled_plugin_is_never_due():
    plugins = {"lastfm": _scheduled("lastfm", 1)}
    cfg = Config(enabled=set())
    assert due_plugins(plugins, cfg, states={}, now=T0) == []


def test_a_plugin_run_recently_is_not_due():
    plugins = {"lastfm": _scheduled("lastfm", 1)}
    cfg = Config(enabled={"lastfm"})
    states = {"lastfm": PluginState("lastfm", last_run=T0 - timedelta(minutes=30))}
    assert due_plugins(plugins, cfg, states, now=T0) == []


def test_a_plugin_past_its_interval_is_due():
    plugins = {"lastfm": _scheduled("lastfm", 1)}
    cfg = Config(enabled={"lastfm"})
    states = {"lastfm": PluginState("lastfm", last_run=T0 - timedelta(hours=2))}
    assert due_plugins(plugins, cfg, states, now=T0) == ["lastfm"]


def test_interval_override_is_respected():
    plugins = {"lastfm": _scheduled("lastfm", 6)}  # default 6h
    cfg = Config(enabled={"lastfm"}, interval_overrides={"lastfm": 600})  # 10 min
    states = {"lastfm": PluginState("lastfm", last_run=T0 - timedelta(minutes=15))}
    assert due_plugins(plugins, cfg, states, now=T0) == ["lastfm"]


def test_manual_and_service_plugins_are_never_scheduled():
    plugins = {
        "dayone": Plugin(id="dayone", name="d", kind="manual", run=lambda c: None),
        "relay": Plugin(id="relay", name="r", kind="service", run=lambda c: None),
    }
    cfg = Config(enabled={"dayone", "relay"})
    assert due_plugins(plugins, cfg, states={}, now=T0) == []


def test_a_long_sleep_yields_exactly_one_catch_up_run():
    # Overdue by 50 intervals (a machine asleep for ~2 days) — still ONE run.
    plugins = {"lastfm": _scheduled("lastfm", 1)}
    cfg = Config(enabled={"lastfm"})
    states = {"lastfm": PluginState("lastfm", last_run=T0 - timedelta(hours=50))}
    assert due_plugins(plugins, cfg, states, now=T0) == ["lastfm"]


def test_offline_excludes_network_requiring_plugins():
    plugins = {"lastfm": _scheduled("lastfm", 1)}  # requires_network defaults True
    cfg = Config(enabled={"lastfm"})
    assert due_plugins(plugins, cfg, states={}, now=T0, online=False) == []


def test_offline_still_runs_plugins_that_do_not_need_network():
    podcasts = Plugin(id="podcasts", name="Podcasts", kind="scheduled",
                      run=lambda c: None, default_interval=timedelta(hours=1),
                      requires_network=False)
    cfg = Config(enabled={"podcasts"})
    assert due_plugins({"podcasts": podcasts}, cfg, states={}, now=T0,
                       online=False) == ["podcasts"]
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run --package fulcra-collect pytest packages/collect/tests/test_scheduler.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'fulcra_collect.scheduler'`.

- [ ] **Step 3: Create `packages/collect/fulcra_collect/scheduler.py`**

```python
"""Scheduling — a pure function deciding which scheduled plugins are due.

The daemon loop calls `due_plugins` periodically; keeping the decision
pure (no clock, no I/O) makes it directly testable with fixed times.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from .config import Config
from .plugin import Plugin
from .state import PluginState


def effective_interval(plugin: Plugin, cfg: Config) -> timedelta:
    """The plugin's scheduling interval — the user override if set,
    otherwise its declared default."""
    override = cfg.interval_overrides.get(plugin.id)
    if override is not None:
        return timedelta(seconds=override)
    assert plugin.default_interval is not None  # guaranteed for kind=scheduled
    return plugin.default_interval


def due_plugins(plugins: dict[str, Plugin], cfg: Config,
                states: dict[str, PluginState], now: datetime,
                online: bool = True) -> list[str]:
    """Return the ids of enabled scheduled plugins whose next run is due.

    A plugin is due when `now - last_run >= interval` — so a plugin
    overdue by many intervals (a long sleep) is returned exactly ONCE,
    not once per missed interval; the single run back-fills the gap via
    its watermark. When `online` is False, plugins with
    `requires_network` are skipped — deferred, not failed — so offline
    time never burns the degraded-failure budget.
    """
    due: list[str] = []
    for pid, plugin in sorted(plugins.items()):
        if plugin.kind != "scheduled" or pid not in cfg.enabled:
            continue
        if plugin.requires_network and not online:
            continue
        st = states.get(pid)
        if st is None or st.last_run is None:
            due.append(pid)
            continue
        if now - st.last_run >= effective_interval(plugin, cfg):
            due.append(pid)
    return due
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run --package fulcra-collect pytest packages/collect/tests/test_scheduler.py -v`
Expected: PASS — 9 tests.

- [ ] **Step 5: Commit**

```bash
git add packages/collect/fulcra_collect/scheduler.py packages/collect/tests/test_scheduler.py
git commit -m "feat(collect): scheduler — due-plugin computation, sleep + offline aware"
```

---

### Task 10: The service supervisor

**Files:**
- Create: `packages/collect/fulcra_collect/supervisor.py`
- Test: `packages/collect/tests/test_supervisor.py`

The restart decision is pure: given a service's recent restart timestamps
and the time it just exited, decide whether to restart and after what
backoff delay, or mark it degraded (crash-loop).

- [ ] **Step 1: Write the failing test**

Create `packages/collect/tests/test_supervisor.py`:

```python
"""The supervisor — pure service restart-decision logic."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fulcra_collect.supervisor import RestartDecision, decide_restart

T0 = datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc)


def test_first_crash_restarts_with_base_backoff():
    d = decide_restart(recent_exits=[T0], now=T0)
    assert d.should_restart is True
    assert d.backoff_seconds == 1.0  # base


def test_backoff_grows_exponentially_with_repeated_crashes():
    exits = [T0 - timedelta(seconds=10), T0 - timedelta(seconds=5), T0]
    d = decide_restart(recent_exits=exits, now=T0)
    assert d.should_restart is True
    assert d.backoff_seconds == 4.0  # 1 * 2 ** (3 - 1)


def test_a_crash_loop_marks_degraded_and_stops_restarting():
    # 6 crashes inside the 60s window -> crash loop.
    exits = [T0 - timedelta(seconds=s) for s in (50, 40, 30, 20, 10, 0)]
    d = decide_restart(recent_exits=exits, now=T0)
    assert d.should_restart is False
    assert d.degraded is True


def test_old_exits_outside_the_window_do_not_count():
    # Five ancient crashes + one fresh one -> treated as a first crash.
    old = [T0 - timedelta(hours=h) for h in (5, 4, 3, 2, 1)]
    d = decide_restart(recent_exits=old + [T0], now=T0)
    assert d.should_restart is True
    assert d.degraded is False
    assert d.backoff_seconds == 1.0
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run --package fulcra-collect pytest packages/collect/tests/test_supervisor.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'fulcra_collect.supervisor'`.

- [ ] **Step 3: Create `packages/collect/fulcra_collect/supervisor.py`**

```python
"""Service supervision — the restart decision for a service plugin whose
worker subprocess has exited.

The decision is pure: given the timestamps of recent exits within a
window, decide whether to restart (and after what backoff) or to declare
a crash loop and stop. The daemon owns the actual subprocesses and the
sleeping; this module owns only the policy.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

CRASH_WINDOW = timedelta(seconds=60)
CRASH_LOOP_THRESHOLD = 6   # exits within CRASH_WINDOW -> degraded
BASE_BACKOFF_S = 1.0
MAX_BACKOFF_S = 60.0


@dataclass
class RestartDecision:
    should_restart: bool
    backoff_seconds: float
    degraded: bool


def decide_restart(recent_exits: list[datetime], now: datetime) -> RestartDecision:
    """Decide what to do after a service worker exit. `recent_exits` is the
    exit timestamps so far (most recent last), including the one that just
    happened."""
    in_window = [t for t in recent_exits if now - t <= CRASH_WINDOW]
    if len(in_window) >= CRASH_LOOP_THRESHOLD:
        return RestartDecision(should_restart=False, backoff_seconds=0.0,
                               degraded=True)
    backoff = min(BASE_BACKOFF_S * 2 ** (len(in_window) - 1), MAX_BACKOFF_S)
    return RestartDecision(should_restart=True, backoff_seconds=backoff,
                           degraded=False)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run --package fulcra-collect pytest packages/collect/tests/test_supervisor.py -v`
Expected: PASS — 4 tests.

- [ ] **Step 5: Commit**

```bash
git add packages/collect/fulcra_collect/supervisor.py packages/collect/tests/test_supervisor.py
git commit -m "feat(collect): service supervisor restart policy"
```

---

### Task 11: The control socket

**Files:**
- Create: `packages/collect/fulcra_collect/control.py`
- Test: `packages/collect/tests/test_control.py`

A Unix-domain-socket server. Protocol: the client connects, sends one
JSON object followed by a newline, reads one JSON object followed by a
newline, closes. The server is given a `handler(request: dict) -> dict`.

- [ ] **Step 1: Write the failing test**

Create `packages/collect/tests/test_control.py`:

```python
"""The control socket — newline-delimited JSON request/response over a UDS."""
from __future__ import annotations

import threading
from pathlib import Path

from fulcra_collect.control import ControlServer, send_request


def test_request_response_round_trip(tmp_path: Path):
    sock = tmp_path / "control.sock"

    def handler(req: dict) -> dict:
        return {"echo": req}

    server = ControlServer(sock, handler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        server.wait_ready(timeout=2.0)
        reply = send_request(sock, {"cmd": "status"})
        assert reply == {"echo": {"cmd": "status"}}
    finally:
        server.shutdown()
        t.join(timeout=2.0)


def test_handler_exception_becomes_an_error_reply(tmp_path: Path):
    sock = tmp_path / "control.sock"

    def handler(req: dict) -> dict:
        raise RuntimeError("handler broke")

    server = ControlServer(sock, handler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        server.wait_ready(timeout=2.0)
        reply = send_request(sock, {"cmd": "status"})
        assert reply["ok"] is False
        assert "handler broke" in reply["error"]
    finally:
        server.shutdown()
        t.join(timeout=2.0)


def test_send_request_to_a_dead_socket_raises(tmp_path: Path):
    import pytest
    with pytest.raises(ConnectionError):
        send_request(tmp_path / "nonexistent.sock", {"cmd": "status"})
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run --package fulcra-collect pytest packages/collect/tests/test_control.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'fulcra_collect.control'`.

- [ ] **Step 3: Create `packages/collect/fulcra_collect/control.py`**

```python
"""The control plane — a Unix-domain-socket server with a newline-delimited
JSON request/response protocol. Filesystem-permissioned; no TCP port.
The `fulcra-collect` CLI (and later the menubar UI) are its clients.
"""
from __future__ import annotations

import json
import socket
import threading
from collections.abc import Callable
from pathlib import Path

Handler = Callable[[dict], dict]


def _read_line(conn: socket.socket) -> bytes:
    chunks: list[bytes] = []
    while True:
        b = conn.recv(4096)
        if not b:
            break
        chunks.append(b)
        if b.endswith(b"\n"):
            break
    return b"".join(chunks)


class ControlServer:
    """Serves one handler over a UDS. `serve_forever` blocks; call it in a
    thread. `shutdown` stops it."""

    def __init__(self, socket_path: Path, handler: Handler) -> None:
        self._path = Path(socket_path)
        self._handler = handler
        self._sock: socket.socket | None = None
        self._stop = threading.Event()
        self._ready = threading.Event()

    def wait_ready(self, timeout: float) -> None:
        if not self._ready.wait(timeout):
            raise TimeoutError("control server did not become ready")

    def serve_forever(self) -> None:
        if self._path.exists():
            self._path.unlink()
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(str(self._path))
        self._sock.listen(8)
        self._sock.settimeout(0.2)
        self._ready.set()
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except socket.timeout:
                continue
            with conn:
                self._serve_one(conn)
        self._sock.close()
        if self._path.exists():
            self._path.unlink()

    def _serve_one(self, conn: socket.socket) -> None:
        raw = _read_line(conn)
        try:
            request = json.loads(raw.decode() or "{}")
            reply = self._handler(request)
        except Exception as exc:  # noqa: BLE001 — a bad request must not kill the server
            reply = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        conn.sendall(json.dumps(reply).encode() + b"\n")

    def shutdown(self) -> None:
        self._stop.set()


def send_request(socket_path: Path, request: dict, *, timeout: float = 5.0) -> dict:
    """Connect to a ControlServer, send one request, return its reply."""
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    conn.settimeout(timeout)
    try:
        conn.connect(str(socket_path))
    except (FileNotFoundError, ConnectionRefusedError, OSError) as exc:
        raise ConnectionError(f"fulcra-collect daemon not reachable at {socket_path}") from exc
    try:
        conn.sendall(json.dumps(request).encode() + b"\n")
        return json.loads(_read_line(conn).decode())
    finally:
        conn.close()
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run --package fulcra-collect pytest packages/collect/tests/test_control.py -v`
Expected: PASS — 3 tests.

- [ ] **Step 5: Commit**

```bash
git add packages/collect/fulcra_collect/control.py packages/collect/tests/test_control.py
git commit -m "feat(collect): Unix-domain-socket control plane"
```

---

### Task 12: The status snapshot + daemon request handler

**Files:**
- Create: `packages/collect/fulcra_collect/daemon.py`
- Test: `packages/collect/tests/test_daemon.py`

The daemon's testable core is `handle_request` — the control-socket
handler — and `status_snapshot`. The blocking run loop (`serve`) is a
thin shell exercised in Task 18's end-to-end check, not unit-tested.

- [ ] **Step 1: Write the failing test**

Create `packages/collect/tests/test_daemon.py`:

```python
"""The daemon request handler + status snapshot."""
from __future__ import annotations

from pathlib import Path

from fulcra_collect.config import Config
from fulcra_collect.daemon import Daemon
from fulcra_collect.plugin import Plugin
from fulcra_collect.registry import RegistryResult


def _registry() -> RegistryResult:
    r = RegistryResult()
    r.plugins["lastfm"] = Plugin(id="lastfm", name="Last.fm", kind="scheduled",
                                 run=lambda c: None,
                                 default_interval=__import__("datetime").timedelta(hours=1))
    r.plugins["dayone"] = Plugin(id="dayone", name="Day One", kind="manual",
                                 run=lambda c: None)
    r.errors["brokenplugin"] = "ImportError: bad"
    return r


def test_status_lists_every_plugin_with_enabled_flag(collect_home: Path):
    d = Daemon(registry=_registry(), config=Config(enabled={"lastfm"}))
    reply = d.handle_request({"cmd": "status"})
    assert reply["ok"] is True
    by_id = {p["id"]: p for p in reply["plugins"]}
    assert by_id["lastfm"]["enabled"] is True
    assert by_id["dayone"]["enabled"] is False
    assert by_id["lastfm"]["kind"] == "scheduled"


def test_status_reports_registry_load_errors(collect_home: Path):
    d = Daemon(registry=_registry(), config=Config())
    reply = d.handle_request({"cmd": "status"})
    assert reply["load_errors"] == {"brokenplugin": "ImportError: bad"}


def test_unknown_command_is_an_error_reply(collect_home: Path):
    d = Daemon(registry=_registry(), config=Config())
    reply = d.handle_request({"cmd": "frobnicate"})
    assert reply["ok"] is False
    assert "frobnicate" in reply["error"]


def test_run_command_rejects_an_unknown_plugin(collect_home: Path):
    d = Daemon(registry=_registry(), config=Config())
    reply = d.handle_request({"cmd": "run", "plugin": "nope"})
    assert reply["ok"] is False


def test_run_command_triggers_a_known_plugin(collect_home: Path):
    d = Daemon(registry=_registry(), config=Config())
    triggered: list[str] = []
    d._trigger = lambda pid: triggered.append(pid)  # injected for the test
    reply = d.handle_request({"cmd": "run", "plugin": "dayone"})
    assert reply["ok"] is True
    assert triggered == ["dayone"]


def test_reload_command_rereads_config(collect_home: Path):
    from fulcra_collect import config as config_mod
    d = Daemon(registry=_registry(), config=Config())
    cfg = config_mod.load()
    cfg.enable("lastfm")
    config_mod.save(cfg)
    reply = d.handle_request({"cmd": "reload"})
    assert reply["ok"] is True
    assert "lastfm" in d.config.enabled
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run --package fulcra-collect pytest packages/collect/tests/test_daemon.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'fulcra_collect.daemon'`.

- [ ] **Step 3: Create `packages/collect/fulcra_collect/daemon.py`**

```python
"""The hub daemon: holds the registry + config, answers control-socket
requests, and (in `serve`) runs the scheduler + supervisor loop.

The request handler and status snapshot are pure enough to unit-test;
`serve` is a thin loop wired in Task 18's end-to-end check.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

from . import config as config_mod
from . import runner, state
from .config import Config
from .control import ControlServer
from .registry import RegistryResult, discover
from .scheduler import due_plugins


def _control_socket_path():
    return config_mod.config_dir() / "control.sock"


def is_online(*, timeout: float = 2.0) -> bool:
    """Best-effort connectivity probe — can a TCP connection to a
    well-known host be opened? Used to defer (not fail) network-requiring
    scheduled plugins while the machine is offline."""
    import socket
    for host, port in (("1.1.1.1", 53), ("8.8.8.8", 53)):
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            continue
    return False


class Daemon:
    def __init__(self, registry: RegistryResult | None = None,
                 config: Config | None = None) -> None:
        self.registry = registry if registry is not None else discover()
        self.config = config if config is not None else config_mod.load()

    # ---- control-socket request handling -------------------------------

    def handle_request(self, request: dict) -> dict:
        cmd = request.get("cmd")
        if cmd == "status":
            return self._status()
        if cmd == "run":
            return self._run(request.get("plugin", ""))
        if cmd == "reload":
            self.config = config_mod.load()
            return {"ok": True}
        return {"ok": False, "error": f"unknown command {cmd!r}"}

    def _status(self) -> dict:
        plugins = []
        for pid, plugin in sorted(self.registry.plugins.items()):
            st = state.load(pid)
            plugins.append({
                "id": pid,
                "name": plugin.name,
                "kind": plugin.kind,
                "enabled": pid in self.config.enabled,
                "last_run": st.last_run.isoformat() if st.last_run else None,
                "last_outcome": st.last_outcome,
                "last_error": st.last_error,
                "consecutive_failures": st.consecutive_failures,
            })
        return {"ok": True, "plugins": plugins,
                "load_errors": dict(self.registry.errors)}

    def _run(self, plugin_id: str) -> dict:
        if plugin_id not in self.registry.plugins:
            return {"ok": False, "error": f"unknown plugin {plugin_id!r}"}
        self._trigger(plugin_id)
        return {"ok": True}

    def _trigger(self, plugin_id: str) -> None:
        """Fire one run of a plugin. Overridden in tests."""
        runner.run(plugin_id, runner.worker_command(plugin_id),
                   now=datetime.now(timezone.utc))

    # ---- the run loop --------------------------------------------------

    def serve(self, *, tick_seconds: float = 30.0) -> None:
        """Run the daemon: serve the control socket and, each tick, fire
        any scheduled plugin that is due. Blocks until the process is
        signalled. (Service-plugin supervision is wired in Task 18.)

        The tick uses a short relative sleep, so a system sleep suspends
        it and it resumes on wake — a machine asleep for hours catches up
        within one tick of waking, each overdue plugin firing once. While
        the machine is offline, network-requiring plugins are skipped
        (deferred), not run into a failure."""
        import threading
        server = ControlServer(_control_socket_path(), self.handle_request)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            while True:
                states = {pid: state.load(pid) for pid in self.registry.plugins}
                online = is_online()
                for pid in due_plugins(self.registry.plugins, self.config,
                                       states, datetime.now(timezone.utc),
                                       online=online):
                    self._trigger(pid)
                time.sleep(tick_seconds)
        finally:
            server.shutdown()
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run --package fulcra-collect pytest packages/collect/tests/test_daemon.py -v`
Expected: PASS — 6 tests.

- [ ] **Step 5: Commit**

```bash
git add packages/collect/fulcra_collect/daemon.py packages/collect/tests/test_daemon.py
git commit -m "feat(collect): daemon — request handler, status, run loop"
```

---

### Task 13: The service installer

**Files:**
- Create: `packages/collect/fulcra_collect/service_manager.py`
- Test: `packages/collect/tests/test_service_manager.py`

Generalizes `fulcra_attention/service_manager.py` to install the
`fulcra-collect` daemon as the single background agent.

- [ ] **Step 1: Write the failing test**

Create `packages/collect/tests/test_service_manager.py`:

```python
"""The launchd/systemd installer for the fulcra-collect daemon."""
from __future__ import annotations

from fulcra_collect import service_manager


def test_launchd_plist_runs_the_daemon():
    plist = service_manager.render_launchd_plist(executable="/opt/venv/bin/fulcra-collect")
    assert "com.fulcra.collect" in plist
    assert "/opt/venv/bin/fulcra-collect" in plist
    assert "<string>daemon</string>" in plist
    assert "RunAtLoad" in plist and "KeepAlive" in plist


def test_systemd_unit_runs_the_daemon():
    unit = service_manager.render_systemd_unit(executable="/opt/venv/bin/fulcra-collect")
    assert "ExecStart=/opt/venv/bin/fulcra-collect daemon" in unit
    assert "Restart=always" in unit


def test_install_writes_the_file_for_this_platform(tmp_path, monkeypatch):
    written = {}

    def fake_write(path, content):
        written["path"] = path
        written["content"] = content

    monkeypatch.setattr(service_manager, "_write_unit", fake_write)
    path = service_manager.install(executable="/opt/venv/bin/fulcra-collect")
    assert written["content"]
    assert path == written["path"]
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run --package fulcra-collect pytest packages/collect/tests/test_service_manager.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'fulcra_collect.service_manager'`.

- [ ] **Step 3: Create `packages/collect/fulcra_collect/service_manager.py`**

```python
"""Install the fulcra-collect daemon as an OS-level user service.

macOS: a launchd user agent. Linux: a systemd user unit. Adapted from
fulcra_attention/service_manager.py — same shape, the hub's daemon.
"""
from __future__ import annotations

import os
import platform
from pathlib import Path

LAUNCHD_LABEL = "com.fulcra.collect"
SYSTEMD_NAME = "fulcra-collect"


def launchd_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


def systemd_unit_path() -> Path:
    return Path.home() / ".config" / "systemd" / "user" / f"{SYSTEMD_NAME}.service"


def render_launchd_plist(*, executable: str) -> str:
    log_dir = Path.home() / "Library" / "Logs" / "fulcra-collect"
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{LAUNCHD_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{executable}</string>
        <string>daemon</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>{log_dir}/daemon.out.log</string>
    <key>StandardErrorPath</key>
    <string>{log_dir}/daemon.err.log</string>
</dict>
</plist>
"""


def render_systemd_unit(*, executable: str) -> str:
    return f"""[Unit]
Description=Fulcra Collect hub daemon
After=network.target

[Service]
Type=simple
ExecStart={executable} daemon
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
"""


def _write_unit(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    os.chmod(path, 0o644)


def install(*, executable: str) -> Path:
    """Render and write the service file for this platform; return its path."""
    system = platform.system()
    if system == "Darwin":
        path = launchd_plist_path()
        content = render_launchd_plist(executable=executable)
    elif system == "Linux":
        path = systemd_unit_path()
        content = render_systemd_unit(executable=executable)
    else:
        raise RuntimeError(f"unsupported platform: {system!r}")
    _write_unit(path, content)
    return path
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run --package fulcra-collect pytest packages/collect/tests/test_service_manager.py -v`
Expected: PASS — 3 tests.

- [ ] **Step 5: Commit**

```bash
git add packages/collect/fulcra_collect/service_manager.py packages/collect/tests/test_service_manager.py
git commit -m "feat(collect): launchd/systemd installer for the daemon"
```

---

### Task 14: The CLI

**Files:**
- Create: `packages/collect/fulcra_collect/cli.py`
- Test: `packages/collect/tests/test_cli.py`

- [ ] **Step 1: Write the failing test**

Create `packages/collect/tests/test_cli.py`:

```python
"""The fulcra-collect CLI."""
from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from fulcra_collect import config as config_mod
from fulcra_collect.cli import cli


def test_enable_then_disable_update_config(collect_home: Path):
    runner = CliRunner()
    assert runner.invoke(cli, ["enable", "lastfm"]).exit_code == 0
    assert config_mod.load().enabled == {"lastfm"}
    assert runner.invoke(cli, ["disable", "lastfm"]).exit_code == 0
    assert config_mod.load().enabled == set()


def test_set_interval_writes_an_override(collect_home: Path):
    res = CliRunner().invoke(cli, ["set-interval", "lastfm", "1800"])
    assert res.exit_code == 0
    assert config_mod.load().interval_overrides == {"lastfm": 1800}


def test_status_reports_when_the_daemon_is_not_running(collect_home: Path):
    res = CliRunner().invoke(cli, ["status"])
    # No daemon -> a clean message, non-zero exit, not a traceback.
    assert res.exit_code != 0
    assert "daemon" in res.output.lower()


def test_status_prints_a_snapshot_from_a_stub_daemon(collect_home: Path, monkeypatch):
    snapshot = {"ok": True, "plugins": [
        {"id": "lastfm", "name": "Last.fm", "kind": "scheduled", "enabled": True,
         "last_run": None, "last_outcome": None, "last_error": None,
         "consecutive_failures": 0},
    ], "load_errors": {}}
    monkeypatch.setattr("fulcra_collect.cli.send_request", lambda *a, **k: snapshot)
    res = CliRunner().invoke(cli, ["status"])
    assert res.exit_code == 0
    assert "lastfm" in res.output


def test_set_credential_stores_into_the_keychain(collect_home: Path, monkeypatch):
    stored = {}
    monkeypatch.setattr("fulcra_collect.cli.credentials.set_secret",
                        lambda pid, key, val: stored.update({(pid, key): val}))
    res = CliRunner().invoke(cli, ["set-credential", "lastfm", "api-key"],
                             input="SECRET123\n")
    assert res.exit_code == 0
    assert stored == {("lastfm", "api-key"): "SECRET123"}
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run --package fulcra-collect pytest packages/collect/tests/test_cli.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'fulcra_collect.cli'`.

- [ ] **Step 3: Create `packages/collect/fulcra_collect/cli.py`**

```python
"""The `fulcra-collect` command-line interface.

`daemon` and `install` act locally; the rest talk to a running daemon
over the control socket. `_worker` is the internal worker entrypoint.
"""
from __future__ import annotations

import shutil
import sys

import click

from . import config as config_mod
from . import credentials, worker
from .control import send_request
from .daemon import Daemon


def _socket_path():
    return config_mod.config_dir() / "control.sock"


@click.group()
def cli() -> None:
    """Background hub for the Fulcra local helpers."""


@cli.command()
def daemon() -> None:
    """Run the hub core in the foreground (the launchd/systemd entrypoint)."""
    Daemon().serve()


@cli.command()
def install() -> None:
    """Install the launchd/systemd user agent for the daemon."""
    from . import service_manager
    exe = shutil.which("fulcra-collect") or "fulcra-collect"
    path = service_manager.install(executable=exe)
    click.echo(f"installed service file: {path}")


@cli.command()
def status() -> None:
    """Show every plugin's kind, enabled flag, and last run."""
    try:
        reply = send_request(_socket_path(), {"cmd": "status"})
    except ConnectionError:
        raise click.ClickException(
            "fulcra-collect daemon is not running — start it with "
            "`fulcra-collect daemon` or install it with `fulcra-collect install`."
        )
    for p in reply["plugins"]:
        flag = "on " if p["enabled"] else "off"
        last = p["last_outcome"] or "never run"
        click.echo(f"  [{flag}] {p['id']:<20} {p['kind']:<10} {last}")
    for name, err in reply.get("load_errors", {}).items():
        click.echo(f"  load error: {name}: {err}", err=True)


@cli.command()
@click.argument("plugin_id")
def run(plugin_id: str) -> None:
    """Trigger one run of a plugin now."""
    reply = send_request(_socket_path(), {"cmd": "run", "plugin": plugin_id})
    if not reply.get("ok"):
        raise click.ClickException(reply.get("error", "run failed"))
    click.echo(f"triggered: {plugin_id}")


def _toggle(plugin_id: str, *, on: bool) -> None:
    cfg = config_mod.load()
    cfg.enable(plugin_id) if on else cfg.disable(plugin_id)
    config_mod.save(cfg)
    try:
        send_request(_socket_path(), {"cmd": "reload"})
    except ConnectionError:
        pass  # daemon not running — config is saved; it'll read it on next start


@cli.command()
@click.argument("plugin_id")
def enable(plugin_id: str) -> None:
    """Enable a plugin."""
    _toggle(plugin_id, on=True)
    click.echo(f"enabled: {plugin_id}")


@cli.command()
@click.argument("plugin_id")
def disable(plugin_id: str) -> None:
    """Disable a plugin."""
    _toggle(plugin_id, on=False)
    click.echo(f"disabled: {plugin_id}")


@cli.command(name="set-interval")
@click.argument("plugin_id")
@click.argument("seconds", type=int)
def set_interval(plugin_id: str, seconds: int) -> None:
    """Override a scheduled plugin's cadence (in seconds)."""
    cfg = config_mod.load()
    cfg.set_interval(plugin_id, seconds)
    config_mod.save(cfg)
    try:
        send_request(_socket_path(), {"cmd": "reload"})
    except ConnectionError:
        pass
    click.echo(f"{plugin_id}: interval set to {seconds}s")


@cli.command(name="set-credential")
@click.argument("plugin_id")
@click.argument("key")
def set_credential(plugin_id: str, key: str) -> None:
    """Store a plugin secret in the OS keychain (prompts, hidden input)."""
    value = click.prompt(f"{plugin_id}/{key}", hide_input=True)
    credentials.set_secret(plugin_id, key, value)
    click.echo(f"stored {plugin_id}/{key}")


@cli.command(name="_worker", hidden=True)
@click.argument("plugin_id")
def _worker(plugin_id: str) -> None:
    """Internal — the worker-subprocess entrypoint."""
    sys.exit(worker.main([plugin_id]))
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run --package fulcra-collect pytest packages/collect/tests/test_cli.py -v`
Expected: PASS — 5 tests.

- [ ] **Step 5: Run the full fulcra-collect suite**

Run: `uv run --package fulcra-collect pytest packages/collect -q`
Expected: PASS — all core tests (plugin 6, config 5, credentials 4, state 4, registry 5, worker 5, runner 5, scheduler 9, supervisor 4, control 3, daemon 6, service_manager 3, cli 5 = 64).

- [ ] **Step 6: Commit**

```bash
git add packages/collect/fulcra_collect/cli.py packages/collect/tests/test_cli.py
git commit -m "feat(collect): fulcra-collect CLI"
```

---

### Task 15: The attention-relay plugin (service)

**Files:**
- Create: `packages/attention/fulcra_attention/collect_plugin.py`
- Modify: `packages/attention/pyproject.toml`
- Test: `packages/attention/tests/test_collect_plugin.py`

The attention relay's existing entrypoint (`fulcra_attention/cli.py`
`relay`) does: load `relay.json`, load state, build `FulcraClient` +
`ReceiverContext`, `make_server(...)`, `serve_forever()`. The plugin's
`run(ctx)` does the same — a blocking service.

- [ ] **Step 1: Write the failing test**

Create `packages/attention/tests/test_collect_plugin.py`:

```python
"""The attention-relay fulcra-collect plugin."""
from __future__ import annotations

from fulcra_attention.collect_plugin import PLUGIN


def test_plugin_metadata_is_a_service():
    assert PLUGIN.id == "attention-relay"
    assert PLUGIN.kind == "service"
    assert PLUGIN.default_interval is None


def test_plugin_declares_the_loopback_server_permission():
    perm_ids = {p.id for p in PLUGIN.required_permissions}
    assert "network-loopback-server" in perm_ids


def test_run_starts_and_stops_the_relay_server(monkeypatch, tmp_path):
    """run(ctx) builds the relay server and calls serve_forever. Stub
    make_server so the test doesn't actually bind a socket forever."""
    served = {}

    class FakeServer:
        def serve_forever(self):
            served["ran"] = True

    monkeypatch.setattr("fulcra_attention.collect_plugin.make_server",
                        lambda **kw: FakeServer())
    monkeypatch.setattr("fulcra_attention.collect_plugin._load_relay_config",
                        lambda ctx: {"bearer_token": "t", "port": 8771})

    class FakeState:
        attention_definition_id = "def-1"

    monkeypatch.setattr("fulcra_attention.collect_plugin.load_state",
                        lambda: FakeState())

    import logging
    from fulcra_collect.plugin import RunContext
    ctx = RunContext(plugin_id="attention-relay", config={}, credentials={},
                     state=None, log=logging.getLogger("t"), _emit=lambda e: None)
    PLUGIN.run(ctx)
    assert served["ran"] is True
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run --package fulcra-attention pytest packages/attention/tests/test_collect_plugin.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'fulcra_attention.collect_plugin'`.

- [ ] **Step 3: Create `packages/attention/fulcra_attention/collect_plugin.py`**

```python
"""fulcra-collect plugin: the attention relay as a supervised service.

run(ctx) builds the loopback relay HTTP server and serves it forever.
The hub's supervisor keeps this alive in a worker subprocess.
"""
from __future__ import annotations

from fulcra_collect.plugin import Credential, Permission, Plugin, RunContext

from .fulcra import FulcraClient
from .relay import ReceiverContext, make_server
from .state import DEFAULT_PATH
from .state import load as _state_load


def load_state():
    return _state_load(DEFAULT_PATH)


def _load_relay_config(ctx: RunContext) -> dict:
    """The relay's bearer token + port. The token is a hub credential;
    the port falls back to 8771 (the value the Chrome extension expects)."""
    return {
        "bearer_token": ctx.credentials.get("bearer-token") or "",
        "port": int(ctx.config.get("port", 8771)),
    }


def run(ctx: RunContext) -> None:
    cfg = _load_relay_config(ctx)
    state = load_state()
    if not state.attention_definition_id:
        raise RuntimeError("attention not bootstrapped — run `fulcra-attention bootstrap`")
    client = FulcraClient()
    receiver = ReceiverContext(client=client, state=state,
                               bearer_token=cfg["bearer_token"])
    server = make_server(host="127.0.0.1", port=cfg["port"], context=receiver)
    ctx.log.info("attention relay listening on 127.0.0.1:%s", cfg["port"])
    server.serve_forever()


PLUGIN = Plugin(
    id="attention-relay",
    name="Attention relay",
    kind="service",
    run=run,
    required_permissions=(
        Permission(id="network-loopback-server",
                   explanation="Runs a local server on 127.0.0.1:8771 that the "
                               "Fulcra Attention browser extension posts to."),
    ),
    required_credentials=(
        Credential(key="bearer-token", label="Relay bearer token",
                   help="The token the browser extension sends; from "
                        "~/.config/fulcra-attention/relay.json"),
    ),
)
```

- [ ] **Step 4: Register the entry point — modify `packages/attention/pyproject.toml`**

Add this section to `packages/attention/pyproject.toml` (after the existing `[project.scripts]` block):

```toml
[project.entry-points."fulcra_collect.plugins"]
attention-relay = "fulcra_attention.collect_plugin:PLUGIN"
```

Also add `fulcra-collect` to the package's dependencies. In the existing `dependencies = [ ... ]` list in `packages/attention/pyproject.toml`, add the line:

```toml
    "fulcra-collect",
```

And add to the existing `[tool.uv.sources]` table:

```toml
fulcra-collect = { workspace = true }
```

- [ ] **Step 5: Run test + re-sync**

Run: `uv sync --all-extras && uv run --package fulcra-attention pytest packages/attention/tests/test_collect_plugin.py -v`
Expected: PASS — 3 tests.

- [ ] **Step 6: Commit**

```bash
git add packages/attention/fulcra_attention/collect_plugin.py packages/attention/pyproject.toml packages/attention/tests/test_collect_plugin.py uv.lock
git commit -m "feat(attention): fulcra-collect service plugin for the relay"
```

---

### Task 16: The lastfm plugin (scheduled)

**Files:**
- Create: `packages/media-helpers/fulcra_media/collect_plugins.py`
- Modify: `packages/media-helpers/pyproject.toml`
- Test: `packages/media-helpers/tests/test_collect_plugins.py`

The Last.fm importer (`fulcra_media.importers.lastfm`) provides
`fetch_recent_tracks(creds, since, max_pages)` and `normalize_history`.
The plugin's `run(ctx)` resolves the API key from `ctx.credentials`, the
`since` from `ctx.state.watermark`, fetches, normalizes, runs the import,
and advances the watermark.

- [ ] **Step 1: Write the failing test**

Create `packages/media-helpers/tests/test_collect_plugins.py`:

```python
"""The Last.fm fulcra-collect plugin."""
from __future__ import annotations

import logging

from fulcra_collect.plugin import RunContext
from fulcra_collect.state import PluginState

from fulcra_media.collect_plugins import LASTFM_PLUGIN


def test_lastfm_plugin_metadata_is_scheduled():
    assert LASTFM_PLUGIN.id == "lastfm"
    assert LASTFM_PLUGIN.kind == "scheduled"
    assert LASTFM_PLUGIN.default_interval is not None
    assert {c.key for c in LASTFM_PLUGIN.required_credentials} == {"api-key"}


def test_run_fetches_normalizes_imports_and_advances_watermark(monkeypatch):
    calls = {}

    monkeypatch.setattr("fulcra_media.collect_plugins.fetch_recent_tracks",
                        lambda creds, since, max_pages: [{"raw": 1}])
    monkeypatch.setattr("fulcra_media.collect_plugins.normalize_history",
                        lambda raw: ["event-1"])

    class FakeResult:
        posted = 1
        skipped_existing = 0
        verified = 1

    class FakeClient:
        def ensure_tag(self, name, state):
            calls["ensure_tag"] = name
        def run_import(self, events, state, check_only=False):
            calls["imported"] = list(events)
            return FakeResult()

    monkeypatch.setattr("fulcra_media.collect_plugins.FulcraClient",
                        lambda: FakeClient())
    monkeypatch.setattr("fulcra_media.collect_plugins.newest_event_iso",
                        lambda events: "2026-05-22T12:00:00Z")

    st = PluginState("lastfm")
    ctx = RunContext(plugin_id="lastfm", config={}, credentials={"api-key": "K"},
                     state=st, log=logging.getLogger("t"), _emit=lambda e: None)
    LASTFM_PLUGIN.run(ctx)

    assert calls["imported"] == ["event-1"]
    assert calls["ensure_tag"] == "lastfm"
    assert st.watermark == "2026-05-22T12:00:00Z"


def test_run_raises_a_clear_error_when_the_api_key_is_missing(monkeypatch):
    st = PluginState("lastfm")
    ctx = RunContext(plugin_id="lastfm", config={}, credentials={},
                     state=st, log=logging.getLogger("t"), _emit=lambda e: None)
    import pytest
    with pytest.raises(RuntimeError, match="api-key"):
        LASTFM_PLUGIN.run(ctx)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run --package fulcra-media-helpers pytest packages/media-helpers/tests/test_collect_plugins.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'fulcra_media.collect_plugins'`.

- [ ] **Step 3: Create `packages/media-helpers/fulcra_media/collect_plugins.py`**

```python
"""fulcra-collect plugins exported by fulcra-media-helpers.

This module currently exposes the Last.fm scheduled plugin (plan 1a);
plan 1b adds the rest of the media importers here.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fulcra_collect.plugin import Credential, Plugin, RunContext

from .fulcra import FulcraClient
from .importers.lastfm import fetch_recent_tracks, normalize_history
from .state import STATE_PATH
from .state import load as _state_load


def newest_event_iso(events: list) -> str | None:
    """The newest start_time across `events`, as an ISO string — the new
    watermark. None when there are no events."""
    if not events:
        return None
    return max(e.start_time for e in events).isoformat()


def _run_lastfm(ctx: RunContext) -> None:
    api_key = ctx.credentials.get("api-key")
    if not api_key:
        raise RuntimeError("lastfm: credential 'api-key' is not set — "
                           "run `fulcra-collect set-credential lastfm api-key`")
    creds = {"api_key": api_key}

    # `since`: one hour before the stored watermark, to catch late
    # server-side reordering. No watermark -> full backfill.
    since: datetime | None = None
    if ctx.state.watermark:
        since = datetime.fromisoformat(
            ctx.state.watermark.replace("Z", "+00:00")
        ) - timedelta(hours=1)

    raw = list(fetch_recent_tracks(creds, since=since, max_pages=None))
    events = list(normalize_history(raw))
    ctx.progress(stage="fetched", count=len(events))

    media_state = _state_load(STATE_PATH)
    client = FulcraClient()
    client.ensure_tag("lastfm", media_state)
    result = client.run_import(events, media_state)
    ctx.progress(stage="imported", posted=result.posted,
                 skipped=result.skipped_existing)

    if result.posted > 0:
        new_wm = newest_event_iso(events)
        if new_wm:
            ctx.state.watermark = new_wm


LASTFM_PLUGIN = Plugin(
    id="lastfm",
    name="Last.fm scrobbles",
    kind="scheduled",
    run=_run_lastfm,
    default_interval=timedelta(hours=1),
    required_credentials=(
        Credential(key="api-key", label="Last.fm API key",
                   help="Create one at https://www.last.fm/api/account/create"),
    ),
)
```

- [ ] **Step 4: Register the entry point — modify `packages/media-helpers/pyproject.toml`**

Add this section to `packages/media-helpers/pyproject.toml` (after the existing `[project.scripts]` block):

```toml
[project.entry-points."fulcra_collect.plugins"]
lastfm = "fulcra_media.collect_plugins:LASTFM_PLUGIN"
```

Add `fulcra-collect` to the `dependencies = [ ... ]` list:

```toml
    "fulcra-collect",
```

And add to the existing `[tool.uv.sources]` table:

```toml
fulcra-collect = { workspace = true }
```

- [ ] **Step 5: Run test + re-sync**

Run: `uv sync --all-extras && uv run --package fulcra-media-helpers pytest packages/media-helpers/tests/test_collect_plugins.py -v`
Expected: PASS — 3 tests.

- [ ] **Step 6: Commit**

```bash
git add packages/media-helpers/fulcra_media/collect_plugins.py packages/media-helpers/pyproject.toml packages/media-helpers/tests/test_collect_plugins.py uv.lock
git commit -m "feat(media-helpers): fulcra-collect scheduled plugin for Last.fm"
```

---

### Task 17: The dayone plugin (manual)

**Files:**
- Create: `packages/dayone/fulcra_dayone/collect_plugin.py`
- Modify: `packages/dayone/pyproject.toml`
- Test: `packages/dayone/tests/test_collect_plugin.py`

The Day One importer pipeline is `readers.read` → `filter.select` →
`convert.to_event` → `DayOneFulcraClient.run_import`. The plugin's
`run(ctx)` reads its source from `ctx.config` — `local_db: true` or an
export-file `path` — and runs the pipeline. It is a manual plugin: the
hub only fires it on `fulcra-collect run dayone`.

- [ ] **Step 1: Write the failing test**

Create `packages/dayone/tests/test_collect_plugin.py`:

```python
"""The Day One fulcra-collect plugin."""
from __future__ import annotations

import logging

import pytest

from fulcra_collect.plugin import RunContext
from fulcra_collect.state import PluginState

from fulcra_dayone.collect_plugin import PLUGIN


def _ctx(config: dict) -> RunContext:
    return RunContext(plugin_id="dayone", config=config, credentials={},
                      state=PluginState("dayone"), log=logging.getLogger("t"),
                      _emit=lambda e: None)


def test_plugin_metadata_is_manual():
    assert PLUGIN.id == "dayone"
    assert PLUGIN.kind == "manual"
    assert PLUGIN.default_interval is None


def test_local_db_mode_runs_the_pipeline(monkeypatch):
    seen = {}
    monkeypatch.setattr("fulcra_dayone.collect_plugin.read",
                        lambda source, local_db, db_path: ["entry"])
    monkeypatch.setattr("fulcra_dayone.collect_plugin.select",
                        lambda entries, **kw: list(entries))
    monkeypatch.setattr("fulcra_dayone.collect_plugin.to_event",
                        lambda e: f"event-{e}")

    class FakeResult:
        posted = 1
        skipped_existing = 0
        verified = 1

    class FakeClient:
        def ensure_journal_definition(self):
            return "def-journal"
        def ensure_tag(self, name):
            return f"tag-{name}"
        def run_import(self, events, definition_id, tag_id_for):
            seen["events"] = list(events)
            seen["definition_id"] = definition_id
            return FakeResult()

    monkeypatch.setattr("fulcra_dayone.collect_plugin.DayOneFulcraClient",
                        lambda: FakeClient())

    PLUGIN.run(_ctx({"local_db": True}))
    assert seen["events"] == ["event-entry"]
    assert seen["definition_id"] == "def-journal"


def test_missing_source_config_raises_a_clear_error():
    with pytest.raises(RuntimeError, match="local_db.*path"):
        PLUGIN.run(_ctx({}))
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run --package fulcra-dayone pytest packages/dayone/tests/test_collect_plugin.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'fulcra_dayone.collect_plugin'`.

- [ ] **Step 3: Create `packages/dayone/fulcra_dayone/collect_plugin.py`**

```python
"""fulcra-collect plugin: import Day One entries (manual).

run(ctx) reads its source from ctx.config — either {"local_db": true}
or {"path": "<export .zip or folder>"} — runs the read -> select ->
convert -> run_import pipeline, and reports counts via ctx.progress.
A manual plugin: the hub fires it only on `fulcra-collect run dayone`.
"""
from __future__ import annotations

from pathlib import Path

from fulcra_collect.plugin import Plugin, RunContext

from .client import DayOneFulcraClient
from .convert import to_event
from .filter import select
from .readers import read


def run(ctx: RunContext) -> None:
    local_db = bool(ctx.config.get("local_db", False))
    path_setting = ctx.config.get("path")
    if not local_db and not path_setting:
        raise RuntimeError(
            "dayone: set either `local_db = true` or `path = \"<export>\"` in "
            "this plugin's settings (config.toml [plugin_settings.dayone])"
        )
    source = Path(path_setting) if path_setting else None
    db_path = Path(ctx.config["db_path"]) if ctx.config.get("db_path") else None

    entries = read(source, local_db=local_db, db_path=db_path)
    selected = select(entries)  # plan 1a imports all entries; filters are a 1b concern
    ctx.progress(stage="read", count=len(selected))
    if not selected:
        return

    events = [to_event(e) for e in selected]
    client = DayOneFulcraClient()
    definition_id = client.ensure_journal_definition()
    tag_names = sorted({t for e in selected for t in e.tags})
    tag_id_for = {name: client.ensure_tag(name) for name in tag_names}
    result = client.run_import(events, definition_id=definition_id,
                               tag_id_for=tag_id_for)
    ctx.progress(stage="imported", posted=result.posted,
                 skipped=result.skipped_existing)


PLUGIN = Plugin(
    id="dayone",
    name="Day One journal",
    kind="manual",
    run=run,
)
```

- [ ] **Step 4: Register the entry point — modify `packages/dayone/pyproject.toml`**

Add this section to `packages/dayone/pyproject.toml` (after the existing `[project.scripts]` block):

```toml
[project.entry-points."fulcra_collect.plugins"]
dayone = "fulcra_dayone.collect_plugin:PLUGIN"
```

Add `fulcra-collect` to the `dependencies = [ ... ]` list:

```toml
    "fulcra-collect",
```

And add to the existing `[tool.uv.sources]` table:

```toml
fulcra-collect = { workspace = true }
```

- [ ] **Step 5: Run test + re-sync**

Run: `uv sync --all-extras && uv run --package fulcra-dayone pytest packages/dayone/tests/test_collect_plugin.py -v`
Expected: PASS — 3 tests.

- [ ] **Step 6: Commit**

```bash
git add packages/dayone/fulcra_dayone/collect_plugin.py packages/dayone/pyproject.toml packages/dayone/tests/test_collect_plugin.py uv.lock
git commit -m "feat(dayone): fulcra-collect manual plugin for Day One import"
```

---

### Task 18: README, end-to-end discovery check, full verification

**Files:**
- Create: `packages/collect/README.md`
- Test: `packages/collect/tests/test_end_to_end.py`

- [ ] **Step 1: Write the end-to-end discovery test**

Create `packages/collect/tests/test_end_to_end.py`:

```python
"""End-to-end: the hub discovers the three reference plugins that the
sibling packages register, and the daemon reports them."""
from __future__ import annotations

from pathlib import Path

from fulcra_collect.config import Config
from fulcra_collect.daemon import Daemon
from fulcra_collect.registry import discover


def test_registry_discovers_the_three_reference_plugins():
    result = discover()
    # The three plan-1a adapters register real entry points in their
    # packages' pyproject.toml; with the workspace synced they are found.
    assert "attention-relay" in result.plugins
    assert "lastfm" in result.plugins
    assert "dayone" in result.plugins
    assert result.plugins["attention-relay"].kind == "service"
    assert result.plugins["lastfm"].kind == "scheduled"
    assert result.plugins["dayone"].kind == "manual"


def test_daemon_status_lists_the_discovered_plugins(collect_home: Path):
    d = Daemon(registry=discover(), config=Config(enabled={"lastfm"}))
    reply = d.handle_request({"cmd": "status"})
    ids = {p["id"] for p in reply["plugins"]}
    assert {"attention-relay", "lastfm", "dayone"} <= ids
```

- [ ] **Step 2: Run the test to verify it passes**

Run: `uv run --package fulcra-collect pytest packages/collect/tests/test_end_to_end.py -v`
Expected: PASS — 2 tests. (If discovery finds nothing, run `uv sync --all-extras` so the sibling packages' entry points are registered, then re-run.)

- [ ] **Step 3: Create `packages/collect/README.md`**

```markdown
# fulcra-collect

The background hub for the Fulcra local helper tools. It discovers
helper *plugins*, schedules the periodic imports, supervises the
long-running services (the attention relay, the media webhook), stores
their credentials in the OS keychain, and exposes status and control
over a local socket.

This package is **sub-project 1**: the headless core. The menubar/tray
UI and the signed installer are later sub-projects.

## How it works

- Plugins are discovered via the `fulcra_collect.plugins` entry-point
  group — any installed package that registers there is found.
- Each plugin declares a kind: `service` (a long-lived server),
  `scheduled` (a periodic import), or `manual` (run on request).
- The `fulcra-collect daemon` process runs the scheduler and the service
  supervisor; each plugin run executes in an isolated worker subprocess.

## Usage

\`\`\`bash
fulcra-collect install            # install the launchd/systemd agent
fulcra-collect daemon             # (or) run the hub in the foreground
fulcra-collect status             # every plugin: kind, enabled, last run
fulcra-collect enable lastfm      # enable a plugin
fulcra-collect set-credential lastfm api-key
fulcra-collect set-interval lastfm 1800
fulcra-collect run dayone         # trigger a manual plugin now
\`\`\`

## Develop

\`\`\`bash
uv sync --all-extras
uv run --package fulcra-collect pytest packages/collect
\`\`\`
```

- [ ] **Step 4: Run the whole workspace test suite**

Run each and confirm zero failures/errors:
- `uv run --package fulcra-common pytest packages/fulcra-common -q`
- `uv run --package fulcra-attention pytest packages/attention -q`
- `uv run --package fulcra-media-helpers pytest packages/media-helpers -q`
- `uv run --package fulcra-csv-importer pytest packages/csv-importer -q`
- `uv run --package fulcra-dayone pytest packages/dayone -q`
- `uv run --package fulcra-collect pytest packages/collect -q`

Expected: every package passes. fulcra-collect ≈ 66 tests (64 core + 2 end-to-end); the four sibling packages each gain 3 plugin tests; fulcra-common unchanged.

- [ ] **Step 5: Commit**

```bash
git add packages/collect/README.md packages/collect/tests/test_end_to_end.py
git commit -m "docs(collect): README + end-to-end plugin-discovery check"
```

---

## Self-Review

**1. Spec coverage:**
- Package `packages/collect/`, module `fulcra_collect`, CLI `fulcra-collect` — Task 1. ✓
- Plugin API (`Plugin`, `Permission`, `Credential`, `RunContext`, `PluginKind`) — Task 2. ✓
- Entry-point discovery (`fulcra_collect.plugins` group), bad-plugin exclusion — Task 6. ✓
- `config.py` (TOML, enabled, interval overrides, settings) — Task 3. ✓
- `credentials.py` (keychain) — Task 4. ✓
- `state.py` (per-plugin last-run/outcome/error/failures/watermark) — Task 5. ✓
- `scheduler.py` (per-plugin cadence + override, manual never auto-fires) — Task 9. ✓
- Sleep / offline / backfill (spec §"Sleep, offline, and backfill"): interval-since-last-run so a long sleep yields one catch-up run (Task 9 `test_a_long_sleep_yields_exactly_one_catch_up_run`); `requires_network` + `online` gate defers offline runs (Task 2 field, Task 9 offline tests, Task 12 `is_online` + `serve`); the watermark/backfill contract is carried worker→result-event→runner so it persists across the process boundary (Tasks 7, 8 + `test_runner_persists_the_watermark_from_the_result`). ✓
- `supervisor.py` (backoff restart, crash-loop → degraded) — Task 10. ✓
- `runner.py` + `worker.py` (worker subprocess, JSON-line progress, timeout, watermark persistence) — Tasks 7, 8. ✓
- `control.py` (UDS protocol) — Task 11. ✓
- `daemon.py` (registry + config + scheduler + control, status snapshot) — Task 12. ✓
- `service_manager.py` (launchd/systemd) — Task 13. ✓
- CLI commands `daemon`/`install`/`status`/`run`/`enable`/`disable`/`set-credential`/`set-interval`/`_worker` — Task 14. ✓
- Permissions declared in plugin metadata — Task 2 (`Permission`), Task 15 (relay declares `network-loopback-server`). ✓
- Error handling: bad-plugin exclusion (Task 6), run error → failure count (Tasks 5, 8), timeout (Task 8), crash-loop (Task 10), missing credential fail-fast (Tasks 16, 17). ✓
- Three reference plugins, one per kind — Tasks 15 (service), 16 (scheduled), 17 (manual). ✓
- Testing with fakes/mocks, `keyring` in-memory backend, entry-point discovery — throughout; end-to-end in Task 18. ✓
- Out of scope (menubar UI, packaging, remaining adapters) — correctly excluded; remaining adapters are plan 1b.
- *Spec note:* the spec lists `RunContext.fulcra_token()`; Task 2 implements it as a thin call to `BaseFulcraClient().get_token()`. The three reference adapters happen to use their package's own `FulcraClient` (which resolves the token internally), so `fulcra_token()` is available but unused by them — kept for the contract and for future plugins.
- *Spec note:* the spec's permission "best-effort probe" / satisfied-status check is represented by the `Permission` declaration (Task 2) and is surfaced in status; an active probe is deferred — plan 1b/sub-project 2 territory, since the consumer of a probe result is the onboarding UI. The plan does not claim to implement the probe.

**2. Placeholder scan:** No "TBD"/"implement later"/"similar to Task N". Every code step shows complete code. The dayone plugin's `select(entries)` with no filters is deliberate and commented (plan 1a imports all entries; filters are 1b). No vague "add error handling" — each error path has explicit code and a test.

**3. Type consistency:** `Plugin(id, name, kind, run, default_interval, required_permissions, required_credentials)` is constructed identically in Tasks 2, 6, 9, 10, 12, 15, 16, 17. `RunContext(plugin_id, config, credentials, state, log, _emit)` is constructed identically in Tasks 2, 7, 15, 16, 17 — every reference-plugin test builds it with the same six fields. `PluginState(plugin_id, last_run, last_outcome, last_error, consecutive_failures, watermark)` and its `record_finish(outcome=, when=, error=)` are consistent across Tasks 5, 8, 9, 12. `RegistryResult(plugins, errors)` consistent across Tasks 6, 7, 12, 18. `Config(enabled, interval_overrides, plugin_settings)` with `enable`/`disable`/`set_interval` consistent across Tasks 3, 9, 12, 14. `runner.run(plugin_id, command, *, now, timeout_s)` and `runner.worker_command(plugin_id)` consistent across Tasks 8, 12. `send_request(socket_path, request)` / `ControlServer(socket_path, handler)` consistent across Tasks 11, 12, 14. `due_plugins(plugins, cfg, states, now, online=True)` consistent between its definition (Task 9) and the daemon's call site (Task 12). The worker's result event — `{"type":"result","outcome",...,"watermark"}` — is emitted in Task 7 and consumed in Task 8 with matching keys.
