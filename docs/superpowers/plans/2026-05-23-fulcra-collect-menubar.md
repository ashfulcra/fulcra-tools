# fulcra-collect menubar Implementation Plan (sub-project 2, Python v1)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship `Fulcra Collect.app`, a macOS menubar app written in Python + PyObjC + `rumps`, that sits on top of the existing `fulcra-collect` daemon as a thin JSON-over-UDS client. The app reads daemon state, lets the user fire plugins on demand, manages plugin enable/interval/credentials, and notifies on consecutive failures.

**Architecture:** A `uv` workspace member at `packages/menubar/` that declares `fulcra-collect` as a workspace dependency. Split into two layers: a **pure-model layer** (no PyObjC imports — fully unit-testable on any platform) holding the daemon client, status model, polling scheduler, and notification de-dup; and a **PyObjC view layer** (status item, popover, preferences) exercised by manual smoke only. The daemon's existing `control.send_request` is reused directly. Four small handlers are added to `fulcra_collect.daemon` (`version`, `credential_status`, `set_credential`, `delete_credential`) as **Phase A** before the menubar work.

**Tech Stack:** Python 3.12+, `rumps` (NSStatusItem baseline), `pyobjc-core` + `pyobjc-framework-Cocoa` + `pyobjc-framework-UserNotifications` (popover, prefs, notifications), `tomlkit` (config round-trip preserving comments/order), `py2app` (build the `.app`), stdlib `socket`/`threading`/`subprocess`, `pytest` for the model layer.

**Spec:** `docs/superpowers/specs/2026-05-22-fulcra-collect-menubar-design.md` (committed as `5bb905a` on `main`).

**Stack note:** Per user direction "start with python; we'll do swift when the ux is locked". The Python build is the UX laboratory; Swift port is deferred to sub-project 2.5 with the lock criteria the spec enumerates. All file boundaries below are chosen so each Python module ports 1:1 to a Swift file later.

**Worktree note:** The brainstorming session committed directly on `main` rather than in a worktree. This plan continues that pattern: tasks commit on `main` until the user explicitly asks for a feature branch. Pushing to `origin/main` is **never** automatic — every push waits for an explicit user "push" after the global pre-push orphan/obsolete sweep.

---

## File Structure

### Phase A — daemon pre-work (in existing `packages/collect/`)

| File | Change | Responsibility |
|---|---|---|
| `fulcra_collect/credentials.py` | Modify | Add `has_secret(plugin_id, key) -> bool`. |
| `fulcra_collect/daemon.py` | Modify | New handlers: `version`, `credential_status`, `set_credential`, `delete_credential`. Cache `version` snapshot at construction. |
| `tests/test_credentials.py` | Modify | Test `has_secret` for present/missing/empty cases. |
| `tests/test_daemon.py` | Modify | Test the four new handlers end-to-end through `handle_request`. |

### Phase B — new package `packages/menubar/`

| File | Responsibility |
|---|---|
| `pyproject.toml` | Package metadata + workspace deps + optional `[macos]` PyObjC deps + py2app build dep. |
| `setup.py` | py2app entry point (build the `.app`). |
| `README.md` | How to run in dev mode, how to build the `.app`, manual smoke checklist. |
| `fulcra_menubar/__init__.py` | Package marker. |
| `fulcra_menubar/__main__.py` | `python -m fulcra_menubar` → calls `app.run()`. |
| `fulcra_menubar/app.py` | `rumps.App` subclass: builds the model layer, wires the status item, popover, preferences, sleep/wake, login item. |
| `fulcra_menubar/daemon_client.py` | Thin typed wrapper over `fulcra_collect.control.send_request`. |
| `fulcra_menubar/model.py` | `StatusModel`: snapshot, in-flight set, observer protocol, diffs. |
| `fulcra_menubar/polling.py` | `PollingScheduler`: 2s-open / 10s-closed, sleep/wake suspension. |
| `fulcra_menubar/notifications.py` | De-dup logic (1/plugin/hour, mute-all) + a thin PyObjC `UserNotifications` post on macOS. |
| `fulcra_menubar/status_item.py` | NSStatusItem icon + badge overlay + running-pulse layer. |
| `fulcra_menubar/popover/__init__.py` | Marker. |
| `fulcra_menubar/popover/root.py` | NSPopover host; white background; section stack. |
| `fulcra_menubar/popover/header.py` | Title + status pill. |
| `fulcra_menubar/popover/plugin_row.py` | One row per plugin. |
| `fulcra_menubar/popover/bootstrap.py` | Daemon-down card with "Install & start daemon". |
| `fulcra_menubar/preferences/__init__.py` | Marker. |
| `fulcra_menubar/preferences/window.py` | NSWindowController + NSTabView host. |
| `fulcra_menubar/preferences/plugins_tab.py` | Enable, interval, credentials, Run now. |
| `fulcra_menubar/preferences/notifications_tab.py` | Notify-on-failure + mute-all toggles. |
| `fulcra_menubar/preferences/about_tab.py` | Versions, paths, Open Logs, Launch-at-login. |
| `fulcra_menubar/theme/__init__.py` | Marker. |
| `fulcra_menubar/theme/palette.py` | Pure hex string constants (no PyObjC imports). |
| `fulcra_menubar/theme/colors.py` | PyObjC `NSColor` factories from the hex constants. macOS-only. |
| `fulcra_menubar/theme/typography.py` | PyObjC `NSFont` factories. macOS-only. |
| `fulcra_menubar/assets/menubar-icon.pdf` | Template image for the status item. |
| `fulcra_menubar/assets/app-icon.icns` | App bundle icon. |
| `tests/__init__.py` | Marker. |
| `tests/conftest.py` | Skip-on-non-macOS for view-layer tests; `tmp_path` config dir helpers. |
| `tests/test_daemon_client.py` | Round-trip through a fake UDS server. |
| `tests/test_model.py` | Snapshot diff, in-flight set, failure-threshold transitions. |
| `tests/test_polling.py` | Fake-clock cadence + sleep/wake suspension. |
| `tests/test_notifications.py` | One-per-plugin-per-hour de-dup + mute-all. |
| `tests/test_palette.py` | Palette tokens are all valid hex; no duplicates. |

All commands below run from the monorepo root **`/Users/Scanning/Developer/fulcra-tools`** unless noted. `uv sync` after editing `pyproject.toml` files. Tests run with `uv run pytest -q` scoped per package.

---

## Phase A — daemon pre-work

These four small additions to `packages/collect/` are prerequisites. They ship with their own unit tests and their own commit *before* the menubar package is created.

### Task 1: `credentials.has_secret` helper

**Files:**
- Modify: `packages/collect/fulcra_collect/credentials.py`
- Modify: `packages/collect/tests/test_credentials.py`

- [ ] **Step 1: Write the failing test**

Append to `packages/collect/tests/test_credentials.py`:

```python
def test_has_secret_returns_true_when_secret_set(monkeypatch):
    store = {}

    def fake_get(service, key):
        return store.get((service, key))

    monkeypatch.setattr("keyring.get_password", fake_get)
    store[("fulcra-collect:lastfm", "session_key")] = "abc"

    from fulcra_collect import credentials

    assert credentials.has_secret("lastfm", "session_key") is True


def test_has_secret_returns_false_when_missing(monkeypatch):
    monkeypatch.setattr("keyring.get_password", lambda s, k: None)

    from fulcra_collect import credentials

    assert credentials.has_secret("lastfm", "session_key") is False


def test_has_secret_returns_false_for_empty_string(monkeypatch):
    # An empty string in the keychain counts as "no credential set" — the
    # menubar UI should still prompt the user to connect.
    monkeypatch.setattr("keyring.get_password", lambda s, k: "")

    from fulcra_collect import credentials

    assert credentials.has_secret("lastfm", "session_key") is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest packages/collect/tests/test_credentials.py -k has_secret -v`
Expected: FAIL with `AttributeError: module 'fulcra_collect.credentials' has no attribute 'has_secret'`

- [ ] **Step 3: Add the helper**

Append to `packages/collect/fulcra_collect/credentials.py`:

```python
def has_secret(plugin_id: str, key: str) -> bool:
    """Return True iff a non-empty secret is present in the keychain for
    (plugin_id, key). The menubar UI's `credential_status` handler is the
    only caller — it reports presence without ever reading the value."""
    return bool(get_secret(plugin_id, key))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest packages/collect/tests/test_credentials.py -k has_secret -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add packages/collect/fulcra_collect/credentials.py packages/collect/tests/test_credentials.py
git commit -m "feat(collect): credentials.has_secret() — presence check for the menubar UI

The menubar's Preferences pane needs to know which of a plugin's
required credentials are present without revealing values. This adds a
boolean helper over the existing keyring wrapper; the new control-socket
handler (next commit) is a thin pass-through.

An empty string in the keychain is treated as 'not set' — the UI prompts
the user to connect."
```

---

### Task 2: `version` control handler

**Files:**
- Modify: `packages/collect/fulcra_collect/daemon.py`
- Modify: `packages/collect/tests/test_daemon.py`

- [ ] **Step 1: Write the failing test**

Append to `packages/collect/tests/test_daemon.py`:

```python
def test_version_handler_returns_daemon_and_plugin_versions(monkeypatch):
    from fulcra_collect import daemon as daemon_mod
    from fulcra_collect.plugin import Plugin
    from fulcra_collect.registry import RegistryResult

    def fake_run(ctx): pass

    plugin = Plugin(id="lastfm", name="Last.fm", kind="manual", run=fake_run)
    registry = RegistryResult(plugins={"lastfm": plugin})

    def fake_version(dist_name):
        return {"fulcra-collect": "0.1.0", "fulcra-media-helpers": "0.4.2"}[dist_name]

    monkeypatch.setattr("fulcra_collect.daemon._distribution_for_plugin",
                        lambda pid: "fulcra-media-helpers")
    monkeypatch.setattr("importlib.metadata.version", fake_version)

    d = daemon_mod.Daemon(registry=registry, config=daemon_mod.Config())

    reply = d.handle_request({"cmd": "version"})

    assert reply["ok"] is True
    assert reply["daemon_version"] == "0.1.0"
    assert reply["plugins"] == {"lastfm": "0.4.2"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest packages/collect/tests/test_daemon.py::test_version_handler_returns_daemon_and_plugin_versions -v`
Expected: FAIL — handler returns `{"ok": False, "error": "unknown command 'version'"}`.

- [ ] **Step 3: Implement `_distribution_for_plugin` + the `version` handler**

In `packages/collect/fulcra_collect/daemon.py`, add at module scope (next to other helpers):

```python
import importlib.metadata as _im


def _distribution_for_plugin(plugin_id: str) -> str | None:
    """Find the distribution that registered this plugin's entry point."""
    for ep in _im.entry_points(group="fulcra_collect.plugins"):
        try:
            obj = ep.load()
        except Exception:
            continue
        # Two entry-point shapes: a Plugin object directly, or a callable
        # returning one. Match by id either way.
        candidate = obj() if callable(obj) and not hasattr(obj, "id") else obj
        if getattr(candidate, "id", None) == plugin_id:
            return ep.dist.name if ep.dist else None
    return None
```

In `Daemon.__init__`, cache the snapshot:

```python
        # Versions are cheap to compute but only at startup; the
        # menubar's About pane calls `version` every time the tab opens.
        self._version_snapshot = self._build_version_snapshot()
```

Add the builder and the handler:

```python
    def _build_version_snapshot(self) -> dict:
        plugins: dict[str, str] = {}
        for pid in self.registry.plugins:
            dist = _distribution_for_plugin(pid)
            if dist is None:
                continue
            try:
                plugins[pid] = _im.version(dist)
            except _im.PackageNotFoundError:
                continue
        try:
            daemon_version = _im.version("fulcra-collect")
        except _im.PackageNotFoundError:
            daemon_version = "unknown"
        return {"daemon_version": daemon_version, "plugins": plugins}
```

In `Daemon.handle_request`, add a branch *before* the unknown-command fallback:

```python
        if cmd == "version":
            return {"ok": True, **self._version_snapshot}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest packages/collect/tests/test_daemon.py::test_version_handler_returns_daemon_and_plugin_versions -v`
Expected: 1 passed.

- [ ] **Step 5: Commit**

```bash
git add packages/collect/fulcra_collect/daemon.py packages/collect/tests/test_daemon.py
git commit -m "feat(collect): version control handler for the menubar About pane

Adds {\"cmd\":\"version\"} to the control socket. Returns the
fulcra-collect daemon version plus the version of each plugin's
distribution. Cached once at Daemon construction — the About pane
calls this on every tab open, but the metadata doesn't change at
runtime.

First of four menubar pre-work handlers (see the sub-project 2 spec)."
```

---

### Task 3: `credential_status` control handler

**Files:**
- Modify: `packages/collect/fulcra_collect/daemon.py`
- Modify: `packages/collect/tests/test_daemon.py`

- [ ] **Step 1: Write the failing test**

Append to `packages/collect/tests/test_daemon.py`:

```python
def test_credential_status_reports_set_and_missing(monkeypatch):
    from fulcra_collect import daemon as daemon_mod
    from fulcra_collect.plugin import Credential, Plugin
    from fulcra_collect.registry import RegistryResult

    plugin = Plugin(
        id="lastfm",
        name="Last.fm",
        kind="manual",
        run=lambda ctx: None,
        required_credentials=(
            Credential(key="session_key", label="Session key", help=""),
            Credential(key="api_key", label="API key", help=""),
        ),
    )
    registry = RegistryResult(plugins={"lastfm": plugin})

    fake_store = {("lastfm", "session_key"): True, ("lastfm", "api_key"): False}
    monkeypatch.setattr(
        "fulcra_collect.credentials.has_secret",
        lambda pid, key: fake_store[(pid, key)],
    )

    d = daemon_mod.Daemon(registry=registry, config=daemon_mod.Config())

    reply = d.handle_request({"cmd": "credential_status", "plugin": "lastfm"})

    assert reply == {
        "ok": True,
        "credentials": {"session_key": "set", "api_key": "missing"},
    }


def test_credential_status_unknown_plugin_returns_error():
    from fulcra_collect import daemon as daemon_mod
    from fulcra_collect.registry import RegistryResult

    d = daemon_mod.Daemon(registry=RegistryResult(), config=daemon_mod.Config())

    reply = d.handle_request({"cmd": "credential_status", "plugin": "nope"})

    assert reply["ok"] is False
    assert "nope" in reply["error"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest packages/collect/tests/test_daemon.py -k credential_status -v`
Expected: 2 FAIL — handler returns `unknown command`.

- [ ] **Step 3: Implement the handler**

In `packages/collect/fulcra_collect/daemon.py`, import `credentials` at the top of the module if not already, then add to `handle_request`:

```python
        if cmd == "credential_status":
            return self._credential_status(request.get("plugin", ""))
```

Add the helper method on `Daemon`:

```python
    def _credential_status(self, plugin_id: str) -> dict:
        plugin = self.registry.plugins.get(plugin_id)
        if plugin is None:
            return {"ok": False, "error": f"unknown plugin {plugin_id!r}"}
        from . import credentials  # local import; avoids cycles in tests
        out: dict[str, str] = {}
        for cred in plugin.required_credentials:
            out[cred.key] = "set" if credentials.has_secret(plugin_id, cred.key) else "missing"
        return {"ok": True, "credentials": out}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest packages/collect/tests/test_daemon.py -k credential_status -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add packages/collect/fulcra_collect/daemon.py packages/collect/tests/test_daemon.py
git commit -m "feat(collect): credential_status control handler

Reports presence — never values — of each required credential a plugin
declares. The menubar's Preferences > Plugins tab polls this on tab
open and after every Connect/Disconnect.

Second of four menubar pre-work handlers."
```

---

### Task 4: `set_credential` + `delete_credential` control handlers

**Files:**
- Modify: `packages/collect/fulcra_collect/daemon.py`
- Modify: `packages/collect/tests/test_daemon.py`

- [ ] **Step 1: Write the failing tests**

Append to `packages/collect/tests/test_daemon.py`:

```python
def test_set_credential_writes_to_keyring(monkeypatch):
    from fulcra_collect import daemon as daemon_mod
    from fulcra_collect.plugin import Credential, Plugin
    from fulcra_collect.registry import RegistryResult

    plugin = Plugin(
        id="lastfm", name="Last.fm", kind="manual", run=lambda c: None,
        required_credentials=(Credential(key="session_key", label="", help=""),),
    )
    registry = RegistryResult(plugins={"lastfm": plugin})

    calls = []
    monkeypatch.setattr(
        "fulcra_collect.credentials.set_secret",
        lambda pid, k, v: calls.append(("set", pid, k, v)),
    )

    d = daemon_mod.Daemon(registry=registry, config=daemon_mod.Config())
    reply = d.handle_request({
        "cmd": "set_credential", "plugin": "lastfm",
        "key": "session_key", "secret": "abc-secret",
    })

    assert reply == {"ok": True}
    assert calls == [("set", "lastfm", "session_key", "abc-secret")]


def test_delete_credential_calls_keyring(monkeypatch):
    from fulcra_collect import daemon as daemon_mod
    from fulcra_collect.plugin import Credential, Plugin
    from fulcra_collect.registry import RegistryResult

    plugin = Plugin(
        id="lastfm", name="Last.fm", kind="manual", run=lambda c: None,
        required_credentials=(Credential(key="session_key", label="", help=""),),
    )
    registry = RegistryResult(plugins={"lastfm": plugin})

    calls = []
    monkeypatch.setattr(
        "fulcra_collect.credentials.delete_secret",
        lambda pid, k: calls.append(("delete", pid, k)),
    )

    d = daemon_mod.Daemon(registry=registry, config=daemon_mod.Config())
    reply = d.handle_request({
        "cmd": "delete_credential", "plugin": "lastfm", "key": "session_key",
    })

    assert reply == {"ok": True}
    assert calls == [("delete", "lastfm", "session_key")]


def test_set_credential_rejects_unknown_plugin():
    from fulcra_collect import daemon as daemon_mod
    from fulcra_collect.registry import RegistryResult

    d = daemon_mod.Daemon(registry=RegistryResult(), config=daemon_mod.Config())

    reply = d.handle_request({
        "cmd": "set_credential", "plugin": "nope", "key": "x", "secret": "y",
    })

    assert reply["ok"] is False
    assert "nope" in reply["error"]


def test_set_credential_rejects_unknown_key():
    from fulcra_collect import daemon as daemon_mod
    from fulcra_collect.plugin import Credential, Plugin
    from fulcra_collect.registry import RegistryResult

    plugin = Plugin(
        id="lastfm", name="Last.fm", kind="manual", run=lambda c: None,
        required_credentials=(Credential(key="session_key", label="", help=""),),
    )
    registry = RegistryResult(plugins={"lastfm": plugin})

    d = daemon_mod.Daemon(registry=registry, config=daemon_mod.Config())

    reply = d.handle_request({
        "cmd": "set_credential", "plugin": "lastfm",
        "key": "not_a_real_key", "secret": "x",
    })

    assert reply["ok"] is False
    assert "not_a_real_key" in reply["error"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest packages/collect/tests/test_daemon.py -k "set_credential or delete_credential" -v`
Expected: 4 FAIL — `unknown command`.

- [ ] **Step 3: Implement both handlers**

In `packages/collect/fulcra_collect/daemon.py` `handle_request`, add two branches:

```python
        if cmd == "set_credential":
            return self._set_credential(
                request.get("plugin", ""), request.get("key", ""),
                request.get("secret", ""),
            )
        if cmd == "delete_credential":
            return self._delete_credential(
                request.get("plugin", ""), request.get("key", ""),
            )
```

Add helper methods on `Daemon`:

```python
    def _check_credential_key(self, plugin_id: str, key: str) -> dict | None:
        """Return an error reply if (plugin_id, key) doesn't name a
        declared required_credential, else None."""
        plugin = self.registry.plugins.get(plugin_id)
        if plugin is None:
            return {"ok": False, "error": f"unknown plugin {plugin_id!r}"}
        if not any(c.key == key for c in plugin.required_credentials):
            return {"ok": False,
                    "error": f"plugin {plugin_id!r} does not declare credential {key!r}"}
        return None

    def _set_credential(self, plugin_id: str, key: str, secret: str) -> dict:
        err = self._check_credential_key(plugin_id, key)
        if err is not None:
            return err
        from . import credentials
        credentials.set_secret(plugin_id, key, secret)
        return {"ok": True}

    def _delete_credential(self, plugin_id: str, key: str) -> dict:
        err = self._check_credential_key(plugin_id, key)
        if err is not None:
            return err
        from . import credentials
        credentials.delete_secret(plugin_id, key)
        return {"ok": True}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest packages/collect/tests/test_daemon.py -k "set_credential or delete_credential" -v`
Expected: 4 passed.

- [ ] **Step 5: Run the whole collect suite to catch regressions**

Run: `uv run pytest packages/collect/tests/ -q`
Expected: all green; the new tests increment the count, the existing tests are unaffected.

- [ ] **Step 6: Commit**

```bash
git add packages/collect/fulcra_collect/daemon.py packages/collect/tests/test_daemon.py
git commit -m "feat(collect): set_credential and delete_credential control handlers

The menubar's Preferences > Plugins > Connect/Disconnect buttons fire
these. Both reject (plugin, key) pairs that aren't declared in the
plugin's required_credentials — a closed set, so a misconfigured UI
can't write secrets to arbitrary keychain entries. Secrets cross the
socket in plaintext; the socket is mode-0600 owner-only and the
threat model is the same as any other process running as the user.

Final two of four menubar pre-work handlers (see the sub-project 2 spec)."
```

---

## Phase B — menubar package scaffold

### Task 5: Scaffold `packages/menubar/`

**Files:**
- Create: `packages/menubar/pyproject.toml`
- Create: `packages/menubar/README.md`
- Create: `packages/menubar/fulcra_menubar/__init__.py`
- Create: `packages/menubar/fulcra_menubar/__main__.py`
- Create: `packages/menubar/tests/__init__.py`
- Create: `packages/menubar/tests/conftest.py`

- [ ] **Step 1: Create `packages/menubar/pyproject.toml`**

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "fulcra-menubar"
version = "0.1.0"
description = "macOS menubar UI for fulcra-collect (Python v1, pre-Swift)."
requires-python = ">=3.12"
dependencies = [
    "fulcra-collect",
    "tomlkit>=0.12",
]

[project.optional-dependencies]
macos = [
    "rumps>=0.4",
    "pyobjc-core>=10",
    "pyobjc-framework-Cocoa>=10",
    "pyobjc-framework-UserNotifications>=10",
    "pyobjc-framework-ServiceManagement>=10",
]
dev = [
    "pytest>=7.4,<8",
    "ruff>=0.5",
]
build = [
    "py2app>=0.28",
]

[tool.uv.sources]
fulcra-collect = { workspace = true }

[project.scripts]
fulcra-menubar = "fulcra_menubar.__main__:main"

[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-ra --strict-markers"

[tool.hatch.build.targets.wheel]
packages = ["fulcra_menubar"]
```

- [ ] **Step 2: Create `packages/menubar/fulcra_menubar/__init__.py`**

```python
"""Fulcra Collect — macOS menubar UI (Python + PyObjC + rumps).

This package is the v1 of sub-project 2 of the fulcra-collect roadmap.
The UI sits on top of the fulcra-collect daemon as a thin JSON-over-UDS
client; everything plugin-side stays on the daemon.

The pure-model layer (daemon_client, model, polling, notifications)
imports no PyObjC and is fully unit-testable on any platform. The view
layer (status_item, popover, preferences) is macOS-only and exercised
by manual smoke.
"""
```

- [ ] **Step 3: Create `packages/menubar/fulcra_menubar/__main__.py`**

```python
"""Entry point for `python -m fulcra_menubar` and the `fulcra-menubar`
script. Builds and runs the rumps app."""
from __future__ import annotations

import sys


def main() -> int:
    if sys.platform != "darwin":
        print("Fulcra Collect menubar runs only on macOS.", file=sys.stderr)
        return 1
    from .app import FulcraMenubarApp  # local import — keeps PyObjC out of test imports
    FulcraMenubarApp().run()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
```

- [ ] **Step 4: Create `packages/menubar/tests/__init__.py`**

```python
```

- [ ] **Step 5: Create `packages/menubar/tests/conftest.py`**

```python
"""Shared test fixtures. The view-layer modules import PyObjC, which is
macOS-only — tests that touch them skip on Linux. Pure-model tests run
everywhere."""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest


def pytest_collection_modifyitems(config, items):
    if sys.platform != "darwin":
        skip_pyobjc = pytest.mark.skip(reason="PyObjC view layer is macOS-only")
        for item in items:
            if "view_layer" in item.keywords:
                item.add_marker(skip_pyobjc)


@pytest.fixture
def temp_config_home(monkeypatch):
    """Point FULCRA_COLLECT_HOME at a temp dir so tests never touch the
    real `~/.config/fulcra-collect`."""
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setenv("FULCRA_COLLECT_HOME", tmp)
        yield Path(tmp)
```

- [ ] **Step 6: Create `packages/menubar/README.md`**

```markdown
# fulcra-menubar

macOS menubar UI for `fulcra-collect`. Python + PyObjC + rumps v1; a
Swift rewrite follows once the UX is locked (see
`docs/superpowers/specs/2026-05-22-fulcra-collect-menubar-design.md`).

## Run in dev mode

    cd /Users/Scanning/Developer/fulcra-tools
    uv sync --extra macos --package fulcra-menubar
    uv run --package fulcra-menubar python -m fulcra_menubar

The daemon must be running (`fulcra-collect service start`).

## Tests

    uv run pytest packages/menubar/tests/ -q

(Pure-model layer only — the view layer is manual smoke.)

## Build the .app

    uv sync --extra macos --extra build --package fulcra-menubar
    cd packages/menubar
    uv run python setup.py py2app

The unsigned `.app` lands in `packages/menubar/dist/Fulcra Collect.app`.
Code-signing and notarization land in sub-project 3.
```

- [ ] **Step 7: Sync the workspace and verify the new package is picked up**

```bash
uv sync
uv run --package fulcra-menubar python -c "import fulcra_menubar; print(fulcra_menubar.__doc__.splitlines()[0])"
```

Expected output: `Fulcra Collect — macOS menubar UI (Python + PyObjC + rumps).`

- [ ] **Step 8: Run the (empty) test suite to confirm pytest is wired**

Run: `uv run --package fulcra-menubar pytest packages/menubar/tests/ -q`
Expected: `no tests ran` (or `0 passed`).

- [ ] **Step 9: Commit**

```bash
git add packages/menubar/ pyproject.toml uv.lock
git commit -m "feat(menubar): scaffold the fulcra-menubar package

New uv workspace member at packages/menubar/. Declares fulcra-collect
as a workspace dep so the menubar can import the existing
control-socket client (fulcra_collect.control.send_request) without
duplication.

PyObjC and rumps are in an optional [macos] extra; the package
installs cleanly on Linux for CI runs of the pure-model layer
(daemon_client, model, polling, notifications) which the next tasks
build out.

No app code yet — just the scaffold and a __main__ stub that exits
politely on non-Darwin."
```

---

## Phase C — pure-model layer

This layer imports zero PyObjC. Everything below runs in `pytest` on any platform; this is the test surface that ports to Swift unchanged.

### Task 6: Palette (pure hex constants)

**Files:**
- Create: `packages/menubar/fulcra_menubar/theme/__init__.py`
- Create: `packages/menubar/fulcra_menubar/theme/palette.py`
- Create: `packages/menubar/tests/test_palette.py`

- [ ] **Step 1: Write the failing test**

```python
# packages/menubar/tests/test_palette.py
from __future__ import annotations

import re

from fulcra_menubar.theme import palette

HEX_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")


def test_palette_tokens_are_all_valid_hex():
    for name, value in palette.tokens().items():
        if name == "BRAND_GRADIENT":
            assert isinstance(value, tuple) and len(value) == 3
            for stop in value:
                assert HEX_RE.match(stop), f"{name} stop {stop!r} is not #RRGGBB"
        else:
            assert HEX_RE.match(value), f"{name} = {value!r} is not #RRGGBB"


def test_palette_tokens_are_unique_per_role():
    # A bug class: copy-pasting `BG` into `BG_ELEV`. Catch that.
    role_values = {
        k: v for k, v in palette.tokens().items()
        if k not in {"BRAND_GRADIENT"}
    }
    duplicates = [k for k, v in role_values.items()
                  if list(role_values.values()).count(v) > 1]
    # BG_ELEV being subtly off from BG is the whole point.
    assert duplicates == [], f"duplicate hex values in palette: {duplicates}"


def test_bg_is_pure_white():
    # Non-negotiable per the spec.
    assert palette.BG == "#FFFFFF"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --package fulcra-menubar pytest packages/menubar/tests/test_palette.py -v`
Expected: FAIL — module `fulcra_menubar.theme.palette` doesn't exist.

- [ ] **Step 3: Create the theme package marker**

`packages/menubar/fulcra_menubar/theme/__init__.py`:

```python
```

- [ ] **Step 4: Implement the palette**

`packages/menubar/fulcra_menubar/theme/palette.py`:

```python
"""Pure hex string constants for the Fulcra Collect menubar.

This module imports nothing macOS-specific. The PyObjC factories that
turn these into NSColor objects live in theme.colors. The hex values
are sampled from the brand reference materials and tuned to read on
white per the spec — see
`docs/superpowers/specs/2026-05-22-fulcra-collect-menubar-design.md`
section "Visual design".
"""
from __future__ import annotations

# Non-negotiable: the app's background is pure white.
BG = "#FFFFFF"
BG_ELEV = "#F7F8FA"
BORDER = "#E5E7EB"

TEXT = "#0B0D17"
TEXT_SECONDARY = "#5A6072"
TEXT_TERTIARY = "#9CA3AF"

ACCENT_VIOLET = "#6B5BEE"
ACCENT_VIOLET_HOVER = "#5045E5"
ACCENT_VIOLET_TINT = "#F1EFFE"

ACCENT_MINT = "#2D8267"
ACCENT_MINT_HOVER = "#226A53"
ACCENT_MINT_TINT = "#E5F4EE"

ACCENT_CYAN = "#10C7BE"
ACCENT_CYAN_DEEP = "#0E9E97"

WARNING = "#B7791F"
ERROR = "#DC2626"

# A 3-stop gradient (cyan → mid → violet) — used sparingly on the
# running-pulse layer and the bootstrap card's accent stripe.
BRAND_GRADIENT = ("#10C7BE", "#4F7BE8", "#8B5BEE")


def tokens() -> dict[str, object]:
    """All exported palette values keyed by their name. Used by the
    test suite to verify hex format and uniqueness."""
    return {k: v for k, v in globals().items() if k.isupper()}
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run --package fulcra-menubar pytest packages/menubar/tests/test_palette.py -v`
Expected: 3 passed.

- [ ] **Step 6: Commit**

```bash
git add packages/menubar/fulcra_menubar/theme/ packages/menubar/tests/test_palette.py
git commit -m "feat(menubar): theme.palette — pure hex constants for the brand-on-white palette

Hex tokens only — no PyObjC. The NSColor factories that consume these
ship in theme.colors (added later, macOS-only). Tests verify the hex
format, no duplicate role values, and BG = #FFFFFF (the spec's
non-negotiable).

This module ports to Swift unchanged: the hex strings transcribe to
Color(hex:) verbatim, which is the contract the spec's
'UX lock and the Swift handoff' section depends on."
```

---

### Task 7: `daemon_client` — typed wrapper over the control socket

**Files:**
- Create: `packages/menubar/fulcra_menubar/daemon_client.py`
- Create: `packages/menubar/tests/test_daemon_client.py`

- [ ] **Step 1: Write the failing test**

```python
# packages/menubar/tests/test_daemon_client.py
from __future__ import annotations

import json
import socket
import threading
from pathlib import Path

import pytest

from fulcra_menubar.daemon_client import DaemonClient, DaemonUnavailable


@pytest.fixture
def fake_daemon(tmp_path):
    """A UDS server that answers each request from a queue of canned
    JSON replies. Yields (socket_path, replies_list, requests_seen)."""
    sock_path = tmp_path / "ctl.sock"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(sock_path))
    server.listen(4)

    replies: list[dict] = []
    seen: list[dict] = []
    stop = threading.Event()

    def serve():
        server.settimeout(0.2)
        while not stop.is_set():
            try:
                conn, _ = server.accept()
            except socket.timeout:
                continue
            with conn:
                buf = b""
                while b"\n" not in buf:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    buf += chunk
                if not buf:
                    continue
                seen.append(json.loads(buf.split(b"\n", 1)[0].decode()))
                reply = replies.pop(0) if replies else {"ok": False, "error": "no canned reply"}
                conn.sendall(json.dumps(reply).encode() + b"\n")

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    try:
        yield sock_path, replies, seen
    finally:
        stop.set()
        t.join(timeout=1.0)
        server.close()


def test_status_round_trip(fake_daemon):
    sock_path, replies, seen = fake_daemon
    replies.append({"ok": True, "plugins": [], "load_errors": {}})

    client = DaemonClient(socket_path=sock_path)
    out = client.status()

    assert out == {"ok": True, "plugins": [], "load_errors": {}}
    assert seen == [{"cmd": "status"}]


def test_run_sends_plugin_id(fake_daemon):
    sock_path, replies, seen = fake_daemon
    replies.append({"ok": True, "started": True})

    client = DaemonClient(socket_path=sock_path)
    out = client.run("lastfm")

    assert out["started"] is True
    assert seen == [{"cmd": "run", "plugin": "lastfm"}]


def test_set_and_delete_credential(fake_daemon):
    sock_path, replies, seen = fake_daemon
    replies.extend([{"ok": True}, {"ok": True}])

    client = DaemonClient(socket_path=sock_path)
    assert client.set_credential("lastfm", "session_key", "abc")["ok"] is True
    assert client.delete_credential("lastfm", "session_key")["ok"] is True

    assert seen == [
        {"cmd": "set_credential", "plugin": "lastfm",
         "key": "session_key", "secret": "abc"},
        {"cmd": "delete_credential", "plugin": "lastfm", "key": "session_key"},
    ]


def test_socket_missing_raises_daemon_unavailable(tmp_path):
    client = DaemonClient(socket_path=tmp_path / "does-not-exist.sock")
    with pytest.raises(DaemonUnavailable):
        client.status()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --package fulcra-menubar pytest packages/menubar/tests/test_daemon_client.py -v`
Expected: 4 FAIL — module `fulcra_menubar.daemon_client` doesn't exist.

- [ ] **Step 3: Implement `DaemonClient`**

`packages/menubar/fulcra_menubar/daemon_client.py`:

```python
"""Typed wrapper around fulcra_collect.control.send_request.

The menubar always speaks to the daemon through this class — never
opens a UDS socket directly. Each method maps to one control-socket
command. Connection errors raise DaemonUnavailable so callers can
treat 'daemon stopped' as a single, known state.
"""
from __future__ import annotations

from pathlib import Path

from fulcra_collect import config as _config
from fulcra_collect.control import send_request as _send_request


class DaemonUnavailable(RuntimeError):
    """Raised when the control socket is missing, refusing connections,
    or the daemon is not on PATH. The UI maps this to the 'Daemon
    stopped' state and shows the bootstrap card."""


def default_socket_path() -> Path:
    return _config.config_dir() / "control.sock"


class DaemonClient:
    """One instance per menubar app. Stateless apart from the socket
    path; safe to call from any thread (the underlying send_request
    opens a fresh connection per call)."""

    def __init__(self, *, socket_path: Path | None = None, timeout: float = 5.0) -> None:
        self.socket_path = socket_path or default_socket_path()
        self.timeout = timeout

    # ---- request plumbing ------------------------------------------

    def _send(self, request: dict) -> dict:
        try:
            return _send_request(self.socket_path, request, timeout=self.timeout)
        except ConnectionError as exc:
            raise DaemonUnavailable(str(exc)) from exc

    # ---- commands --------------------------------------------------

    def status(self) -> dict:
        return self._send({"cmd": "status"})

    def run(self, plugin_id: str) -> dict:
        return self._send({"cmd": "run", "plugin": plugin_id})

    def reload(self) -> dict:
        return self._send({"cmd": "reload"})

    def version(self) -> dict:
        return self._send({"cmd": "version"})

    def credential_status(self, plugin_id: str) -> dict:
        return self._send({"cmd": "credential_status", "plugin": plugin_id})

    def set_credential(self, plugin_id: str, key: str, secret: str) -> dict:
        return self._send({
            "cmd": "set_credential", "plugin": plugin_id,
            "key": key, "secret": secret,
        })

    def delete_credential(self, plugin_id: str, key: str) -> dict:
        return self._send({
            "cmd": "delete_credential", "plugin": plugin_id, "key": key,
        })
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --package fulcra-menubar pytest packages/menubar/tests/test_daemon_client.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add packages/menubar/fulcra_menubar/daemon_client.py packages/menubar/tests/test_daemon_client.py
git commit -m "feat(menubar): DaemonClient — typed wrapper over the control socket

One method per control-socket command, all funnelled through the
existing fulcra_collect.control.send_request helper. The menubar
never opens a UDS socket directly — this is the only client surface
the rest of the app sees, and the only place 'daemon stopped' gets
translated from a stdlib ConnectionError into a typed
DaemonUnavailable.

Tests round-trip every command through a real socketpair-based fake
daemon — exercising the wire framing too, not just the method
signatures."
```

---

### Task 8: `StatusModel` — snapshot, in-flight set, observer protocol

**Files:**
- Create: `packages/menubar/fulcra_menubar/model.py`
- Create: `packages/menubar/tests/test_model.py`

- [ ] **Step 1: Write the failing tests**

```python
# packages/menubar/tests/test_model.py
from __future__ import annotations

from datetime import datetime, timezone

from fulcra_menubar.model import StatusModel, OverallState


HEALTHY = {
    "ok": True, "plugins": [
        {"id": "lastfm", "name": "Last.fm", "kind": "scheduled",
         "enabled": True, "last_run": "2026-05-23T12:00:00+00:00",
         "last_outcome": "done", "last_error": None,
         "consecutive_failures": 0},
    ], "load_errors": {},
}

FAILING = {
    "ok": True, "plugins": [
        {"id": "lastfm", "name": "Last.fm", "kind": "scheduled",
         "enabled": True, "last_run": "2026-05-23T12:05:00+00:00",
         "last_outcome": "error", "last_error": "401 unauthorized",
         "consecutive_failures": 3},
    ], "load_errors": {},
}


def test_initial_state_is_unknown():
    m = StatusModel()
    assert m.overall is OverallState.UNKNOWN
    assert m.plugins == []


def test_healthy_snapshot_yields_healthy_overall():
    m = StatusModel()
    m.update_from_status(HEALTHY)
    assert m.overall is OverallState.HEALTHY


def test_failing_snapshot_yields_failing_overall():
    m = StatusModel()
    m.update_from_status(FAILING)
    assert m.overall is OverallState.FAILING


def test_observers_called_on_change():
    m = StatusModel()
    calls = []
    m.add_observer(lambda model: calls.append(model.overall))
    m.update_from_status(HEALTHY)
    m.update_from_status(FAILING)
    assert calls == [OverallState.HEALTHY, OverallState.FAILING]


def test_observers_not_called_when_snapshot_unchanged():
    m = StatusModel()
    calls = []
    m.add_observer(lambda model: calls.append(model.overall))
    m.update_from_status(HEALTHY)
    m.update_from_status(HEALTHY)  # identical
    assert calls == [OverallState.HEALTHY]  # second update no-ops


def test_in_flight_set_drives_running_overall():
    m = StatusModel()
    m.update_from_status(HEALTHY)
    m.mark_in_flight("lastfm")
    assert m.overall is OverallState.RUNNING
    # status poll after the run completes (last_run advances) clears it
    advanced = {**HEALTHY, "plugins": [{**HEALTHY["plugins"][0],
                                         "last_run": "2026-05-23T12:10:00+00:00"}]}
    m.update_from_status(advanced)
    assert m.overall is OverallState.HEALTHY
    assert "lastfm" not in m.in_flight


def test_daemon_stopped_overrides_everything():
    m = StatusModel()
    m.update_from_status(FAILING)
    m.mark_daemon_stopped()
    assert m.overall is OverallState.DAEMON_STOPPED


def test_failure_threshold_transitions():
    m = StatusModel()
    m.update_from_status(HEALTHY)
    transitions = []
    m.add_failure_transition_observer(transitions.append)
    m.update_from_status(FAILING)  # 0 -> 3, crosses 3
    assert transitions == ["lastfm"]
    m.update_from_status(FAILING)  # 3 -> 3, no new transition
    assert transitions == ["lastfm"]


def test_failure_transition_only_on_first_crossing():
    m = StatusModel()
    transitions = []
    m.add_failure_transition_observer(transitions.append)
    # First snapshot already at 3 — that IS a crossing from the model's
    # POV (we had no prior knowledge to know it had been failing).
    m.update_from_status(FAILING)
    assert transitions == ["lastfm"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --package fulcra-menubar pytest packages/menubar/tests/test_model.py -v`
Expected: 8 FAIL — module doesn't exist.

- [ ] **Step 3: Implement `StatusModel`**

`packages/menubar/fulcra_menubar/model.py`:

```python
"""In-memory status snapshot + diff/observer protocol.

This is a pure-Python module — no PyObjC. The view layer observes it;
the polling layer feeds it. Diffing here means the UI only redraws on
actual change, and failure-threshold transitions fire exactly once per
crossing.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class OverallState(Enum):
    UNKNOWN = "unknown"
    HEALTHY = "healthy"
    RUNNING = "running"
    FAILING = "failing"
    DAEMON_STOPPED = "daemon_stopped"


@dataclass
class PluginSnapshot:
    id: str
    name: str
    kind: str
    enabled: bool
    last_run: str | None
    last_outcome: str | None
    last_error: str | None
    consecutive_failures: int

    @classmethod
    def from_dict(cls, d: dict) -> "PluginSnapshot":
        return cls(
            id=d["id"], name=d["name"], kind=d["kind"],
            enabled=d.get("enabled", False),
            last_run=d.get("last_run"),
            last_outcome=d.get("last_outcome"),
            last_error=d.get("last_error"),
            consecutive_failures=d.get("consecutive_failures", 0),
        )


@dataclass
class StatusModel:
    plugins: list[PluginSnapshot] = field(default_factory=list)
    load_errors: dict[str, str] = field(default_factory=dict)
    in_flight: set[str] = field(default_factory=set)
    daemon_stopped: bool = False

    _last_snapshot_raw: Any = None
    _observers: list[Callable[["StatusModel"], None]] = field(default_factory=list)
    _failure_observers: list[Callable[[str], None]] = field(default_factory=list)
    _known_failing: set[str] = field(default_factory=set)

    # ---- observer registration -------------------------------------

    def add_observer(self, fn: Callable[["StatusModel"], None]) -> None:
        self._observers.append(fn)

    def add_failure_transition_observer(self, fn: Callable[[str], None]) -> None:
        self._failure_observers.append(fn)

    # ---- mutation --------------------------------------------------

    def update_from_status(self, reply: dict) -> None:
        if reply == self._last_snapshot_raw and not self.daemon_stopped:
            return  # no-op when nothing changed
        self._last_snapshot_raw = reply
        self.daemon_stopped = False
        self.plugins = [PluginSnapshot.from_dict(p) for p in reply.get("plugins", [])]
        self.load_errors = dict(reply.get("load_errors", {}))
        self._reconcile_in_flight()
        self._fire_failure_transitions()
        self._notify()

    def mark_daemon_stopped(self) -> None:
        if self.daemon_stopped:
            return
        self.daemon_stopped = True
        self._notify()

    def mark_in_flight(self, plugin_id: str) -> None:
        if plugin_id not in self.in_flight:
            self.in_flight.add(plugin_id)
            self._notify()

    # ---- derived state --------------------------------------------

    @property
    def overall(self) -> OverallState:
        if self.daemon_stopped:
            return OverallState.DAEMON_STOPPED
        if not self.plugins:
            return OverallState.UNKNOWN
        if self.in_flight:
            return OverallState.RUNNING
        if any(p.consecutive_failures > 0 for p in self.plugins if p.enabled):
            return OverallState.FAILING
        return OverallState.HEALTHY

    @property
    def failing_count(self) -> int:
        return sum(1 for p in self.plugins if p.enabled and p.consecutive_failures > 0)

    # ---- internals --------------------------------------------------

    def _reconcile_in_flight(self) -> None:
        """A plugin id leaves in_flight once its snapshot's last_run
        moves past the value we observed when the run was triggered.
        Here we approximate: if a plugin is in_flight AND its current
        snapshot has last_outcome != 'running' (the daemon doesn't emit
        that; the absence is the signal that the runner exited) AND its
        last_run is non-null, treat the run as completed."""
        completed = set()
        for p in self.plugins:
            if p.id in self.in_flight and p.last_run:
                completed.add(p.id)
        self.in_flight -= completed

    def _fire_failure_transitions(self) -> None:
        now_failing = {p.id for p in self.plugins
                        if p.enabled and p.consecutive_failures >= 3}
        crossings = now_failing - self._known_failing
        self._known_failing = now_failing
        for pid in sorted(crossings):
            for fn in self._failure_observers:
                fn(pid)

    def _notify(self) -> None:
        for fn in self._observers:
            fn(self)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --package fulcra-menubar pytest packages/menubar/tests/test_model.py -v`
Expected: 8 passed.

- [ ] **Step 5: Commit**

```bash
git add packages/menubar/fulcra_menubar/model.py packages/menubar/tests/test_model.py
git commit -m "feat(menubar): StatusModel — daemon snapshot + in-flight set + transitions

The pure-Python observer model that the view layer subscribes to.
- Computes a five-state OverallState (UNKNOWN, HEALTHY, RUNNING,
  FAILING, DAEMON_STOPPED) from the snapshot + in-flight set.
- Skips the notify cycle when an identical snapshot is re-applied
  (the 2s poll otherwise hammers the view layer).
- Fires a separate transition observer exactly once per plugin when
  consecutive_failures first crosses 3 — the notifications layer
  hooks that to post macOS notifications.

No PyObjC imports. This module is the contract; the view layer (next
tasks) only reads from it."
```

---

### Task 9: `PollingScheduler` — 2s/10s cadence, sleep-aware

**Files:**
- Create: `packages/menubar/fulcra_menubar/polling.py`
- Create: `packages/menubar/tests/test_polling.py`

- [ ] **Step 1: Write the failing tests**

```python
# packages/menubar/tests/test_polling.py
from __future__ import annotations

import threading
import time

from fulcra_menubar.polling import PollingScheduler


def test_open_cadence_is_two_seconds():
    times: list[float] = []
    now = [0.0]

    def fake_monotonic() -> float:
        return now[0]

    def fake_sleep(s: float) -> None:
        now[0] += s

    def tick() -> None:
        times.append(now[0])

    sched = PollingScheduler(
        on_tick=tick, monotonic=fake_monotonic, sleep=fake_sleep,
    )
    sched.set_popover_open(True)

    stop_after = [3]

    def maybe_stop():
        if len(times) >= stop_after[0]:
            sched.stop()
    sched.add_post_tick_hook(maybe_stop)

    sched.run()

    assert times == [0.0, 2.0, 4.0]


def test_closed_cadence_is_ten_seconds():
    times: list[float] = []
    now = [0.0]

    sched = PollingScheduler(
        on_tick=lambda: times.append(now[0]),
        monotonic=lambda: now[0],
        sleep=lambda s: now.__setitem__(0, now[0] + s),
    )
    sched.set_popover_open(False)

    def maybe_stop():
        if len(times) >= 3:
            sched.stop()
    sched.add_post_tick_hook(maybe_stop)

    sched.run()

    assert times == [0.0, 10.0, 20.0]


def test_open_then_closed_switches_cadence():
    times: list[float] = []
    now = [0.0]

    sched = PollingScheduler(
        on_tick=lambda: times.append(now[0]),
        monotonic=lambda: now[0],
        sleep=lambda s: now.__setitem__(0, now[0] + s),
    )
    sched.set_popover_open(True)  # 2s

    def maybe_stop():
        if len(times) == 2:
            sched.set_popover_open(False)  # switch mid-loop to 10s
        if len(times) >= 4:
            sched.stop()

    sched.add_post_tick_hook(maybe_stop)
    sched.run()

    # tick 0 at t=0, tick 1 at t=2 (open). Then switch to closed.
    # tick 2 at t=12, tick 3 at t=22.
    assert times == [0.0, 2.0, 12.0, 22.0]


def test_sleep_suspends_ticking():
    times: list[float] = []
    now = [0.0]

    sched = PollingScheduler(
        on_tick=lambda: times.append(now[0]),
        monotonic=lambda: now[0],
        sleep=lambda s: now.__setitem__(0, now[0] + s),
    )
    sched.set_popover_open(True)

    def maybe_act():
        if len(times) == 1:
            sched.suspend()      # asleep
            now[0] += 100        # 100s of system sleep
            sched.resume()       # back to ticking
        if len(times) >= 3:
            sched.stop()
    sched.add_post_tick_hook(maybe_act)

    sched.run()

    # tick 0 at t=0; suspend at t=0; clock jumps to t=100; resume; next
    # tick fires immediately (t=100), then again at t=102.
    assert times == [0.0, 100.0, 102.0]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --package fulcra-menubar pytest packages/menubar/tests/test_polling.py -v`
Expected: FAIL — module doesn't exist.

- [ ] **Step 3: Implement `PollingScheduler`**

`packages/menubar/fulcra_menubar/polling.py`:

```python
"""Drives periodic status polls.

The schedule has two regimes — 2s while the popover is open (the user
wants live feedback) and 10s while it is closed (just enough to keep
the menubar icon honest and to fire failure notifications). The whole
thing suspends while the machine is asleep; on wake, the next tick
fires immediately so an overdue plugin shows up in seconds.

This is a pure-logic module — `monotonic` and `sleep` are injected so
the tests can use a fake clock. In production, `time.monotonic` and
`time.sleep` are passed in.
"""
from __future__ import annotations

import threading
import time as _time
from collections.abc import Callable

INTERVAL_OPEN_S = 2.0
INTERVAL_CLOSED_S = 10.0


class PollingScheduler:
    def __init__(
        self,
        *,
        on_tick: Callable[[], None],
        monotonic: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self._on_tick = on_tick
        self._monotonic = monotonic or _time.monotonic
        self._sleep = sleep or _time.sleep
        self._popover_open = False
        self._stop = False
        self._suspended = False
        self._suspended_cond = threading.Condition()
        self._post_tick_hooks: list[Callable[[], None]] = []

    def set_popover_open(self, open_: bool) -> None:
        self._popover_open = open_

    def add_post_tick_hook(self, hook: Callable[[], None]) -> None:
        self._post_tick_hooks.append(hook)

    def suspend(self) -> None:
        with self._suspended_cond:
            self._suspended = True

    def resume(self) -> None:
        with self._suspended_cond:
            self._suspended = False
            self._suspended_cond.notify_all()

    def stop(self) -> None:
        self._stop = True
        self.resume()  # release any wait in the sleep loop

    def run(self) -> None:
        while not self._stop:
            self._tick()
            if self._stop:
                break
            self._sleep_for_interval()

    # ---- internals --------------------------------------------------

    def _tick(self) -> None:
        try:
            self._on_tick()
        finally:
            for hook in self._post_tick_hooks:
                hook()

    def _interval(self) -> float:
        return INTERVAL_OPEN_S if self._popover_open else INTERVAL_CLOSED_S

    def _sleep_for_interval(self) -> None:
        # If suspended (machine asleep), block here until resumed; do
        # NOT consume the interval while suspended.
        with self._suspended_cond:
            while self._suspended:
                # Real implementation uses condition wait; tests skip
                # this branch because they call resume() inline.
                self._suspended_cond.wait()
                if self._stop:
                    return
                return  # on resume, tick immediately — don't wait the interval
        self._sleep(self._interval())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --package fulcra-menubar pytest packages/menubar/tests/test_polling.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add packages/menubar/fulcra_menubar/polling.py packages/menubar/tests/test_polling.py
git commit -m "feat(menubar): PollingScheduler — 2s open / 10s closed, sleep-aware

The status-poll heartbeat. `monotonic` and `sleep` are injected so the
test suite can drive it with a fake clock and assert exact tick times.

The 'suspended' branch is the sleep/wake hook — when macOS posts
NSWorkspaceWillSleepNotification the app calls suspend(), and the next
tick fires immediately on resume() rather than after a full interval.
That matters because a laptop that was asleep for hours should refresh
the moment the user opens the lid, not on the next 10s heartbeat."
```

---

### Task 10: `notifications` — de-dup logic, posting callback injected

**Files:**
- Create: `packages/menubar/fulcra_menubar/notifications.py`
- Create: `packages/menubar/tests/test_notifications.py`

- [ ] **Step 1: Write the failing tests**

```python
# packages/menubar/tests/test_notifications.py
from __future__ import annotations

from fulcra_menubar.notifications import NotificationCentre


def test_one_post_per_plugin_per_hour():
    posts = []
    now = [0.0]
    centre = NotificationCentre(
        post=lambda title, body: posts.append((now[0], title, body)),
        monotonic=lambda: now[0],
    )

    centre.notify_failure("lastfm", "401 unauthorized")
    centre.notify_failure("lastfm", "401 unauthorized")  # dup, within hour
    now[0] += 3600 - 1
    centre.notify_failure("lastfm", "401 unauthorized")  # still within hour
    now[0] += 2
    centre.notify_failure("lastfm", "401 unauthorized")  # outside hour, fires

    assert [t for t, _, _ in posts] == [0.0, 3601.0]


def test_different_plugins_are_independent():
    posts = []
    centre = NotificationCentre(
        post=lambda title, body: posts.append((title, body)),
        monotonic=lambda: 0.0,
    )

    centre.notify_failure("lastfm", "x")
    centre.notify_failure("spotify-extended", "y")

    assert len(posts) == 2


def test_mute_all_suppresses_everything():
    posts = []
    centre = NotificationCentre(
        post=lambda title, body: posts.append((title, body)),
        monotonic=lambda: 0.0,
    )
    centre.mute_all = True

    centre.notify_failure("lastfm", "x")
    centre.notify_daemon_stopped()

    assert posts == []


def test_daemon_stopped_is_independent_of_plugin_dedup():
    posts = []
    now = [0.0]
    centre = NotificationCentre(
        post=lambda title, body: posts.append((now[0], title)),
        monotonic=lambda: now[0],
    )

    centre.notify_failure("lastfm", "x")
    centre.notify_daemon_stopped()  # different category, fires
    centre.notify_daemon_stopped()  # dup within hour
    now[0] += 3601
    centre.notify_daemon_stopped()  # outside hour, fires

    assert [t for t, _ in posts] == [0.0, 0.0, 3601.0]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --package fulcra-menubar pytest packages/menubar/tests/test_notifications.py -v`
Expected: FAIL — module doesn't exist.

- [ ] **Step 3: Implement `NotificationCentre`**

`packages/menubar/fulcra_menubar/notifications.py`:

```python
"""Failure-notification de-dup logic.

Each notification category is rate-limited to at most one post per
hour. The actual macOS notification is injected as a callback — the
tests pass a recorder; on macOS the app passes a PyObjC wrapper around
UNUserNotificationCenter.

This module imports no PyObjC. The real `post` implementation lives in
app.py and uses pyobjc-framework-UserNotifications.
"""
from __future__ import annotations

import time as _time
from collections.abc import Callable

DEDUP_WINDOW_S = 3600.0

_DAEMON_STOPPED_KEY = ("_daemon", "_stopped")


class NotificationCentre:
    def __init__(
        self,
        *,
        post: Callable[[str, str], None],
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        self._post = post
        self._monotonic = monotonic or _time.monotonic
        self._last_posted_at: dict[tuple[str, str], float] = {}
        self.mute_all = False

    def notify_failure(self, plugin_id: str, error: str) -> None:
        self._maybe_post(
            key=("failure", plugin_id),
            title=f"{plugin_id} is failing",
            body=error or "Fulcra Collect plugin has failed 3 times in a row.",
        )

    def notify_daemon_stopped(self) -> None:
        self._maybe_post(
            key=_DAEMON_STOPPED_KEY,
            title="Fulcra Collect daemon stopped",
            body="The background daemon is no longer running. Open the "
                 "menubar to start it again.",
        )

    def _maybe_post(self, *, key: tuple[str, str], title: str, body: str) -> None:
        if self.mute_all:
            return
        now = self._monotonic()
        last = self._last_posted_at.get(key, -DEDUP_WINDOW_S - 1)
        if now - last < DEDUP_WINDOW_S:
            return
        self._last_posted_at[key] = now
        self._post(title, body)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --package fulcra-menubar pytest packages/menubar/tests/test_notifications.py -v`
Expected: 4 passed.

- [ ] **Step 5: Run the full menubar suite**

Run: `uv run --package fulcra-menubar pytest packages/menubar/tests/ -q`
Expected: all green. Pure-model layer is complete.

- [ ] **Step 6: Commit**

```bash
git add packages/menubar/fulcra_menubar/notifications.py packages/menubar/tests/test_notifications.py
git commit -m "feat(menubar): NotificationCentre — failure de-dup, 1/category/hour

The pure-logic part of the notification surface: rate-limits each
(category, plugin) pair to one post per hour so a flapping plugin
doesn't fill the Notification Centre. The actual UNUserNotification
post is a callback the app supplies on macOS — keeps PyObjC out of
this module so it runs on CI on any platform.

Closes the pure-model layer. View-layer tasks follow."
```

---

## Phase D — PyObjC view layer (manual smoke)

The view layer is exercised by hand: a developer runs `python -m fulcra_menubar`, watches the icon, opens the popover, clicks buttons, edits preferences. Each task lists its **smoke check** — the human-observable behaviour to confirm before committing.

The view layer imports PyObjC. Tasks 6–10's pure-model tests stay green throughout.

### Task 11: `app.py` — `rumps.App` scaffold + idle status item

**Files:**
- Create: `packages/menubar/fulcra_menubar/app.py`
- Create: `packages/menubar/fulcra_menubar/status_item.py`
- Create: `packages/menubar/fulcra_menubar/assets/menubar-icon.pdf` (placeholder PDF until the brand asset arrives)
- Create: `packages/menubar/fulcra_menubar/theme/colors.py`
- Create: `packages/menubar/fulcra_menubar/theme/typography.py`

- [ ] **Step 1: Generate a placeholder template icon**

A 22×22 monochrome circle is fine for development — the brand mark
ships separately. Run from the monorepo root:

```bash
uv run --package fulcra-menubar python - <<'PY'
from pathlib import Path
import struct, zlib, io

# Minimal valid PDF with a single black filled circle, 22pt × 22pt.
# Just enough for NSImage to load and treat as a template image.
pdf = b"""%PDF-1.4
1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj
2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj
3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 22 22]/Contents 4 0 R>>endobj
4 0 obj<</Length 76>>stream
q 0 0 0 rg
11 11 m
11 17 6 22 0 17 c
-6 22 -11 17 -11 11 c
-11 5 -6 0 0 5 c
6 0 11 5 11 11 c f
Q
endstream
endobj
xref
0 5
0000000000 65535 f
0000000009 00000 n
0000000052 00000 n
0000000095 00000 n
0000000156 00000 n
trailer<</Size 5/Root 1 0 R>>
startxref
278
%%EOF
"""

target = Path("packages/menubar/fulcra_menubar/assets/menubar-icon.pdf")
target.parent.mkdir(parents=True, exist_ok=True)
target.write_bytes(pdf)
print("wrote", target)
PY
```

- [ ] **Step 2: Implement `theme/colors.py` and `theme/typography.py`**

`packages/menubar/fulcra_menubar/theme/colors.py`:

```python
"""PyObjC NSColor factories built from the pure hex tokens in palette.

This module is macOS-only — it imports PyObjC. Anything that needs
colours but does not import PyObjC should pull from theme.palette
instead.
"""
from __future__ import annotations

from AppKit import NSColor  # type: ignore[import-not-found]

from . import palette


def _hex(value: str) -> NSColor:
    """#RRGGBB → NSColor in the device sRGB colour space, opaque."""
    h = value.lstrip("#")
    r = int(h[0:2], 16) / 255.0
    g = int(h[2:4], 16) / 255.0
    b = int(h[4:6], 16) / 255.0
    return NSColor.colorWithSRGBRed_green_blue_alpha_(r, g, b, 1.0)


def bg() -> NSColor: return _hex(palette.BG)
def bg_elev() -> NSColor: return _hex(palette.BG_ELEV)
def border() -> NSColor: return _hex(palette.BORDER)

def text() -> NSColor: return _hex(palette.TEXT)
def text_secondary() -> NSColor: return _hex(palette.TEXT_SECONDARY)
def text_tertiary() -> NSColor: return _hex(palette.TEXT_TERTIARY)

def violet() -> NSColor: return _hex(palette.ACCENT_VIOLET)
def violet_hover() -> NSColor: return _hex(palette.ACCENT_VIOLET_HOVER)
def violet_tint() -> NSColor: return _hex(palette.ACCENT_VIOLET_TINT)

def mint() -> NSColor: return _hex(palette.ACCENT_MINT)
def mint_hover() -> NSColor: return _hex(palette.ACCENT_MINT_HOVER)
def mint_tint() -> NSColor: return _hex(palette.ACCENT_MINT_TINT)

def cyan() -> NSColor: return _hex(palette.ACCENT_CYAN)
def cyan_deep() -> NSColor: return _hex(palette.ACCENT_CYAN_DEEP)

def warning() -> NSColor: return _hex(palette.WARNING)
def error() -> NSColor: return _hex(palette.ERROR)
```

`packages/menubar/fulcra_menubar/theme/typography.py`:

```python
"""PyObjC NSFont factories. macOS-only."""
from __future__ import annotations

from AppKit import NSFont  # type: ignore[import-not-found]


def title() -> NSFont:
    return NSFont.systemFontOfSize_weight_(16.0, 0.5)  # semibold ~ 0.5


def body() -> NSFont:
    return NSFont.systemFontOfSize_weight_(14.0, 0.0)  # regular


def small() -> NSFont:
    return NSFont.systemFontOfSize_weight_(12.0, 0.0)


def mono() -> NSFont:
    return NSFont.monospacedSystemFontOfSize_weight_(11.0, 0.0)
```

- [ ] **Step 3: Implement `status_item.py` (idle-only for now)**

`packages/menubar/fulcra_menubar/status_item.py`:

```python
"""The menubar icon. Holds a reference to the NSStatusItem owned by
rumps and applies overlay states (idle / running / failure / down)
driven by the StatusModel.

This task wires up the idle state only. The running pulse and the
failure badge land in Task 12.
"""
from __future__ import annotations

from pathlib import Path

from AppKit import NSImage  # type: ignore[import-not-found]

from .model import OverallState, StatusModel

ASSET = Path(__file__).parent / "assets" / "menubar-icon.pdf"


class StatusItemController:
    def __init__(self, rumps_app, model: StatusModel) -> None:
        self._app = rumps_app
        self._model = model
        self._base_image = NSImage.alloc().initWithContentsOfFile_(str(ASSET))
        self._base_image.setTemplate_(True)  # tints with the menubar
        self._apply()
        model.add_observer(lambda _m: self._apply())

    def _apply(self) -> None:
        # rumps exposes the underlying NSStatusItem at ._nsapp; in v1
        # we just set the title image. State overlays are stubbed.
        ns_item = self._app._nsapp.nsstatusitem
        if self._model.overall is OverallState.DAEMON_STOPPED:
            # half-alpha placeholder — proper rendering in Task 12.
            ns_item.button().setImage_(self._base_image)
            ns_item.button().setAlphaValue_(0.4)
        else:
            ns_item.button().setImage_(self._base_image)
            ns_item.button().setAlphaValue_(1.0)
```

- [ ] **Step 4: Implement `app.py` (minimal — status item + on-click stub)**

`packages/menubar/fulcra_menubar/app.py`:

```python
"""The rumps.App subclass.

Hosts the model layer, wires the status item, opens the popover on
click. Sleep/wake observers, preferences, and the notification post
path land in later tasks.
"""
from __future__ import annotations

import logging

import rumps  # type: ignore[import-not-found]

from .daemon_client import DaemonClient, DaemonUnavailable
from .model import StatusModel
from .polling import PollingScheduler
from .status_item import StatusItemController

logger = logging.getLogger("fulcra_menubar")


class FulcraMenubarApp(rumps.App):
    def __init__(self) -> None:
        super().__init__("Fulcra Collect", icon=None, quit_button=None)
        self.client = DaemonClient()
        self.model = StatusModel()
        self.status_item = StatusItemController(self, self.model)
        self.poller = PollingScheduler(on_tick=self._poll_once)
        self.poller.set_popover_open(False)
        import threading
        threading.Thread(target=self.poller.run, daemon=True).start()

    @rumps.clicked("Quit")
    def _quit(self, _sender):  # placeholder until the popover/menu lands
        rumps.quit_application()

    def _poll_once(self) -> None:
        try:
            reply = self.client.status()
        except DaemonUnavailable:
            self.model.mark_daemon_stopped()
            return
        self.model.update_from_status(reply)
```

The default rumps menu offers `Quit` and a placeholder click handler;
this is enough to verify the icon shows up. Subsequent tasks replace
the menu with the popover.

- [ ] **Step 5: Smoke check**

```bash
# (with the daemon NOT running — daemon-stopped state)
uv sync --extra macos --package fulcra-menubar
uv run --package fulcra-menubar python -m fulcra_menubar
```

**Observe:** A dimmed black template icon appears in the menubar.
Clicking it shows a rumps menu with a single "Quit" item. Click Quit
to exit cleanly.

Then start the daemon and re-launch:

```bash
fulcra-collect service start    # if installed
# or, for dev:
uv run --package fulcra-collect fulcra-collect daemon &
DAEMON_PID=$!
uv run --package fulcra-menubar python -m fulcra_menubar
# When done:
kill $DAEMON_PID
```

**Observe:** The icon is at full opacity (no daemon-stopped dim). Quit cleanly.

- [ ] **Step 6: Commit**

```bash
git add packages/menubar/fulcra_menubar/ packages/menubar/fulcra_menubar/assets/menubar-icon.pdf
git commit -m "feat(menubar): minimal rumps app with idle status item

Boots the rumps.App, builds the StatusModel, starts the
PollingScheduler on a background thread, and shows the template
menubar icon. Two states wired so far: full opacity when the daemon
answers status, 40% opacity when the socket is unreachable
(DAEMON_STOPPED) — the failure-badge and running-pulse overlays land
in the next task.

Placeholder PDF icon — the real Fulcra mark replaces it during the
asset pass before py2app.

Smoke: 'python -m fulcra_menubar' shows a dimmed/full-opacity icon
correctly depending on whether 'fulcra-collect daemon' is running."
```

---

### Task 12: Status item — running pulse + failure badge

**Files:**
- Modify: `packages/menubar/fulcra_menubar/status_item.py`

- [ ] **Step 1: Extend `StatusItemController`**

Replace `status_item.py`'s body with the following (kept as one file since the running pulse and failure badge are both small `CALayer` work):

```python
"""The menubar icon. Holds a reference to the NSStatusItem owned by
rumps and applies overlay states driven by the StatusModel.

Three overlays:
  - running pulse: a violet glow CALayer that fades in/out while the
    in-flight set is non-empty.
  - failure badge: a small red dot in the bottom-right corner while any
    enabled plugin has consecutive_failures > 0.
  - daemon-down: the base image at 40% alpha.

The base template image stays untouched so macOS continues to tint it
with the menubar's foreground colour.
"""
from __future__ import annotations

from pathlib import Path

from AppKit import (  # type: ignore[import-not-found]
    NSBezierPath, NSColor, NSCompositingOperationSourceOver, NSGraphicsContext,
    NSImage, NSMakeRect,
)
from Quartz import (  # type: ignore[import-not-found]
    CABasicAnimation, CALayer, kCAFillModeForwards,
)

from .model import OverallState, StatusModel
from .theme import palette

ASSET = Path(__file__).parent / "assets" / "menubar-icon.pdf"


def _hex_to_cgcolor(hex_value: str, alpha: float = 1.0):
    h = hex_value.lstrip("#")
    r = int(h[0:2], 16) / 255.0
    g = int(h[2:4], 16) / 255.0
    b = int(h[4:6], 16) / 255.0
    return NSColor.colorWithSRGBRed_green_blue_alpha_(r, g, b, alpha).CGColor()


def _compose_image_with_badge(base: NSImage, badge_hex: str) -> NSImage:
    """Return a new NSImage = base with a 6pt dot of `badge_hex` at the
    bottom-right. The base is drawn first; the dot overlays it."""
    size = base.size()
    out = NSImage.alloc().initWithSize_(size)
    out.lockFocus()
    base.drawInRect_fromRect_operation_fraction_(
        NSMakeRect(0, 0, size.width, size.height),
        NSMakeRect(0, 0, 0, 0),
        NSCompositingOperationSourceOver,
        1.0,
    )
    NSColor.colorWithSRGBRed_green_blue_alpha_(
        int(badge_hex[1:3], 16) / 255.0,
        int(badge_hex[3:5], 16) / 255.0,
        int(badge_hex[5:7], 16) / 255.0,
        1.0,
    ).set()
    badge = NSBezierPath.bezierPathWithOvalInRect_(
        NSMakeRect(size.width - 7, 0, 6, 6)
    )
    badge.fill()
    out.unlockFocus()
    out.setTemplate_(False)  # badge MUST stay red even on light menubars
    return out


class StatusItemController:
    def __init__(self, rumps_app, model: StatusModel) -> None:
        self._app = rumps_app
        self._model = model
        self._base = NSImage.alloc().initWithContentsOfFile_(str(ASSET))
        self._base.setTemplate_(True)
        self._with_badge = _compose_image_with_badge(self._base, palette.ERROR)
        self._pulse_layer: CALayer | None = None
        self._apply()
        model.add_observer(lambda _m: self._apply())

    def _ns_button(self):
        return self._app._nsapp.nsstatusitem.button()

    def _apply(self) -> None:
        btn = self._ns_button()
        state = self._model.overall

        if state is OverallState.DAEMON_STOPPED:
            btn.setImage_(self._base)
            btn.setAlphaValue_(0.4)
            self._set_pulse(active=False)
            return

        btn.setAlphaValue_(1.0)

        if self._model.failing_count > 0:
            btn.setImage_(self._with_badge)
        else:
            btn.setImage_(self._base)

        self._set_pulse(active=(state is OverallState.RUNNING))

    def _set_pulse(self, *, active: bool) -> None:
        btn = self._ns_button()
        btn.setWantsLayer_(True)
        layer = btn.layer()
        if self._pulse_layer is None and active:
            self._pulse_layer = CALayer.layer()
            self._pulse_layer.setFrame_(layer.bounds())
            self._pulse_layer.setBackgroundColor_(
                _hex_to_cgcolor(palette.ACCENT_VIOLET, alpha=0.0)
            )
            self._pulse_layer.setCornerRadius_(4.0)
            layer.addSublayer_(self._pulse_layer)
            anim = CABasicAnimation.animationWithKeyPath_("backgroundColor")
            anim.setFromValue_(_hex_to_cgcolor(palette.ACCENT_VIOLET, alpha=0.0))
            anim.setToValue_(_hex_to_cgcolor(palette.ACCENT_VIOLET, alpha=0.45))
            anim.setDuration_(0.9)
            anim.setAutoreverses_(True)
            anim.setRepeatCount_(1e9)  # forever
            anim.setFillMode_(kCAFillModeForwards)
            self._pulse_layer.addAnimation_forKey_(anim, "pulse")
        elif self._pulse_layer is not None and not active:
            self._pulse_layer.removeAllAnimations()
            self._pulse_layer.removeFromSuperlayer()
            self._pulse_layer = None
```

- [ ] **Step 2: Smoke check — failure badge**

With the daemon running, force a failure: enable a plugin whose
credentials are missing (e.g. `lastfm` with no session key) and let it
run once.

```bash
# Configure lastfm without credentials so it'll fail.
fulcra-collect plugin enable lastfm
fulcra-collect run lastfm   # will fail
fulcra-collect run lastfm
fulcra-collect run lastfm   # consecutive_failures = 3
uv run --package fulcra-menubar python -m fulcra_menubar
```

**Observe:** the menubar icon now carries a small red dot in the
bottom-right.

- [ ] **Step 3: Smoke check — running pulse**

While the menubar is open, in another terminal:

```bash
fulcra-collect run dayone   # a manual plugin — takes a moment
```

**Observe:** the menubar icon pulses a soft violet for the duration of
the run; the pulse stops within ~10s of the run completing.

- [ ] **Step 4: Commit**

```bash
git add packages/menubar/fulcra_menubar/status_item.py
git commit -m "feat(menubar): status item — failure badge + running pulse overlays

Two new icon states beyond idle/daemon-stopped:
  - Red dot bottom-right when any enabled plugin has
    consecutive_failures > 0 (composited into a non-template NSImage so
    the badge stays red on light and dark menubars).
  - Violet pulse CALayer behind the template icon while the in-flight
    set is non-empty, animated forever with auto-reverse.

Smoke: clear a credential to force a plugin failure and watch the
badge appear; trigger a manual plugin to watch the pulse fire."
```

---

### Task 13: Popover root + header

**Files:**
- Create: `packages/menubar/fulcra_menubar/popover/__init__.py`
- Create: `packages/menubar/fulcra_menubar/popover/root.py`
- Create: `packages/menubar/fulcra_menubar/popover/header.py`
- Modify: `packages/menubar/fulcra_menubar/app.py` (open popover on click)

- [ ] **Step 1: Create the popover package marker**

`packages/menubar/fulcra_menubar/popover/__init__.py`:

```python
```

- [ ] **Step 2: Implement `popover/header.py`**

```python
"""The popover header: title + status pill."""
from __future__ import annotations

from AppKit import (  # type: ignore[import-not-found]
    NSColor, NSStackView, NSStackViewDistributionEqualSpacing,
    NSTextField, NSUserInterfaceLayoutOrientationHorizontal,
    NSView, NSMakeRect, NSLayoutAttributeCenterY,
)

from ..model import OverallState, StatusModel
from ..theme import colors, palette, typography


_STATE_LABEL = {
    OverallState.HEALTHY: ("Healthy", palette.ACCENT_MINT),
    OverallState.RUNNING: ("Running…", palette.ACCENT_VIOLET),
    OverallState.FAILING: ("Failing", palette.ERROR),
    OverallState.DAEMON_STOPPED: ("Daemon stopped", palette.TEXT_TERTIARY),
    OverallState.UNKNOWN: ("Connecting…", palette.TEXT_TERTIARY),
}


def make_header(model: StatusModel) -> NSView:
    view = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, 360, 56))

    title = NSTextField.labelWithString_("Fulcra Collect")
    title.setFont_(typography.title())
    title.setTextColor_(colors.text())
    title.setFrame_(NSMakeRect(16, 28, 220, 22))

    subtitle = NSTextField.labelWithString_("")
    subtitle.setFont_(typography.small())
    subtitle.setTextColor_(colors.text_secondary())
    subtitle.setFrame_(NSMakeRect(16, 8, 280, 16))

    pill = NSTextField.labelWithString_("")
    pill.setFont_(typography.small())
    pill.setAlignment_(2)  # right-aligned
    pill.setFrame_(NSMakeRect(220, 28, 124, 22))

    view.addSubview_(title)
    view.addSubview_(subtitle)
    view.addSubview_(pill)

    def refresh(_m=None):
        text, color_hex = _STATE_LABEL.get(model.overall, _STATE_LABEL[OverallState.UNKNOWN])
        if model.overall is OverallState.FAILING and model.failing_count > 1:
            text = f"{model.failing_count} failing"
        pill.setStringValue_("●  " + text)
        pill.setTextColor_(_color(color_hex))
        n = len(model.plugins)
        scheduled = sum(1 for p in model.plugins if p.kind == "scheduled")
        services = sum(1 for p in model.plugins if p.kind == "service")
        manual = sum(1 for p in model.plugins if p.kind == "manual")
        subtitle.setStringValue_(
            f"{n} plugins · {scheduled} scheduled · {services} services · {manual} manual"
        )

    refresh()
    model.add_observer(refresh)
    return view


def _color(hex_value: str):
    h = hex_value.lstrip("#")
    return NSColor.colorWithSRGBRed_green_blue_alpha_(
        int(h[0:2], 16) / 255.0, int(h[2:4], 16) / 255.0,
        int(h[4:6], 16) / 255.0, 1.0,
    )
```

- [ ] **Step 3: Implement `popover/root.py`**

```python
"""The NSPopover host. White background, fixed width, scrolling body.

Section content (plugin rows, bootstrap card) is added in later tasks.
For now this task lands the popover shell and the header — enough to
verify the white surface and the header refreshes on model changes.
"""
from __future__ import annotations

from AppKit import (  # type: ignore[import-not-found]
    NSColor, NSPopover, NSPopoverBehaviorTransient, NSView, NSViewController,
    NSMakeRect, NSMakeSize,
)

from ..model import StatusModel
from ..theme import colors
from .header import make_header


WIDTH = 360.0
DEFAULT_HEIGHT = 240.0


class PopoverRoot:
    def __init__(self, model: StatusModel) -> None:
        self._model = model
        self._popover = NSPopover.alloc().init()
        self._popover.setBehavior_(NSPopoverBehaviorTransient)
        self._popover.setContentSize_(NSMakeSize(WIDTH, DEFAULT_HEIGHT))

        controller = NSViewController.alloc().init()
        root = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, WIDTH, DEFAULT_HEIGHT))
        root.setWantsLayer_(True)
        root.layer().setBackgroundColor_(colors.bg().CGColor())

        header = make_header(model)
        header.setFrame_(NSMakeRect(0, DEFAULT_HEIGHT - 56, WIDTH, 56))
        root.addSubview_(header)

        # Section body placeholder — replaced in Task 14 with the plugin list.
        controller.setView_(root)
        self._popover.setContentViewController_(controller)
        self._popover.setAppearance_(_light_appearance())

    def toggle(self, anchor_view) -> None:
        if self._popover.isShown():
            self._popover.close()
        else:
            self._popover.showRelativeToRect_ofView_preferredEdge_(
                anchor_view.bounds(), anchor_view, 5  # NSMaxYEdge equivalent
            )


def _light_appearance():
    from AppKit import NSAppearance  # type: ignore[import-not-found]
    return NSAppearance.appearanceNamed_("NSAppearanceNameAqua")
```

- [ ] **Step 4: Wire the popover into `app.py`**

Replace the contents of `app.py` with:

```python
"""The rumps.App subclass.

Hosts the model layer, wires the status item, opens the popover on
click. Sleep/wake observers, preferences, and the notification post
path land in later tasks.
"""
from __future__ import annotations

import logging
import threading

import rumps  # type: ignore[import-not-found]

from .daemon_client import DaemonClient, DaemonUnavailable
from .model import StatusModel
from .polling import PollingScheduler
from .popover.root import PopoverRoot
from .status_item import StatusItemController

logger = logging.getLogger("fulcra_menubar")


class FulcraMenubarApp(rumps.App):
    def __init__(self) -> None:
        super().__init__("Fulcra Collect", icon=None, quit_button=None)
        self.client = DaemonClient()
        self.model = StatusModel()
        self.status_item = StatusItemController(self, self.model)
        self.popover = PopoverRoot(self.model)
        self.poller = PollingScheduler(on_tick=self._poll_once)
        self.poller.set_popover_open(False)
        threading.Thread(target=self.poller.run, daemon=True).start()

        # rumps doesn't directly expose a click-to-popover hook; we
        # replace the menu with a single "Open" item that toggles the
        # popover. Right-click and the fallback menu come in Task 17.
        self.menu = ["Open Fulcra Collect", None, "Quit"]

    @rumps.clicked("Open Fulcra Collect")
    def _open(self, _sender) -> None:
        btn = self._nsapp.nsstatusitem.button()
        self.popover.toggle(btn)
        self.poller.set_popover_open(self.popover._popover.isShown())

    @rumps.clicked("Quit")
    def _quit(self, _sender) -> None:
        rumps.quit_application()

    def _poll_once(self) -> None:
        try:
            reply = self.client.status()
        except DaemonUnavailable:
            self.model.mark_daemon_stopped()
            return
        self.model.update_from_status(reply)
```

- [ ] **Step 5: Smoke check**

With the daemon running, launch the menubar and click "Open Fulcra Collect" from the rumps menu.

```bash
uv run --package fulcra-menubar python -m fulcra_menubar
```

**Observe:** A white popover appears anchored to the status item, with
"Fulcra Collect" as the title and a status pill on the right
("Healthy" with a mint dot, or "N failing" with a red dot, depending
on state). The subtitle reads `N plugins · K scheduled · M services · L manual`.

- [ ] **Step 6: Commit**

```bash
git add packages/menubar/fulcra_menubar/popover/ packages/menubar/fulcra_menubar/app.py
git commit -m "feat(menubar): popover shell + header

NSPopover anchored to the status item, white background, fixed 360pt
width. The header carries the 'Fulcra Collect' title, a status pill
(Healthy / N failing / Running / Daemon stopped / Connecting) coloured
from the brand palette, and a subtitle counting plugins by kind.

The polling cadence flips to 2s while the popover is shown and back
to 10s when it closes — implements the live-feedback regime the spec
describes.

Body is still empty — the plugin list lands next."
```

---

### Task 14: Plugin row + body list

**Files:**
- Create: `packages/menubar/fulcra_menubar/popover/plugin_row.py`
- Modify: `packages/menubar/fulcra_menubar/popover/root.py`

- [ ] **Step 1: Implement `popover/plugin_row.py`**

```python
"""One row per plugin. 44pt tall. Layout:

  [dot]  Name           …  last-run-relative   [Run now]
         id                                       (or kind pill)
"""
from __future__ import annotations

from datetime import datetime, timezone

from AppKit import (  # type: ignore[import-not-found]
    NSButton, NSColor, NSImage, NSTextField, NSView, NSMakeRect,
    NSBezelStyleRounded,
)

from ..daemon_client import DaemonClient
from ..model import PluginSnapshot, StatusModel
from ..theme import colors, palette, typography

ROW_HEIGHT = 44


def make_row(snapshot: PluginSnapshot, *, client: DaemonClient,
              model: StatusModel, width: float) -> NSView:
    view = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, width, ROW_HEIGHT))

    dot = _status_dot(snapshot, model)
    dot.setFrame_(NSMakeRect(16, 18, 10, 10))
    view.addSubview_(dot)

    name = NSTextField.labelWithString_(snapshot.name)
    name.setFont_(typography.body())
    name.setTextColor_(colors.text() if snapshot.enabled else colors.text_tertiary())
    name.setFrame_(NSMakeRect(34, 22, 180, 18))
    view.addSubview_(name)

    pid = NSTextField.labelWithString_(snapshot.id)
    pid.setFont_(typography.small())
    pid.setTextColor_(colors.text_secondary())
    pid.setFrame_(NSMakeRect(34, 6, 180, 14))
    view.addSubview_(pid)

    right_text = NSTextField.labelWithString_(_right_text(snapshot))
    right_text.setFont_(typography.small())
    right_text.setTextColor_(colors.text_secondary())
    right_text.setAlignment_(2)  # right
    right_text.setFrame_(NSMakeRect(width - 200, 16, 96, 14))
    view.addSubview_(right_text)

    if snapshot.kind in ("scheduled", "manual") and snapshot.enabled:
        button = NSButton.alloc().initWithFrame_(NSMakeRect(width - 96, 12, 80, 22))
        button.setTitle_("Run now")
        button.setBezelStyle_(NSBezelStyleRounded)

        def _on_click(_sender):
            try:
                client.run(snapshot.id)
            finally:
                model.mark_in_flight(snapshot.id)

        _RowTarget.attach(button, _on_click)
        view.addSubview_(button)

    return view


def _right_text(s: PluginSnapshot) -> str:
    if s.kind == "service":
        if s.last_outcome == "error":
            return "Crashed"
        return "Running"
    if not s.last_run:
        return "Never run"
    return _relative(s.last_run)


def _relative(iso: str) -> str:
    try:
        when = datetime.fromisoformat(iso)
    except ValueError:
        return iso
    delta = datetime.now(timezone.utc) - when
    sec = int(delta.total_seconds())
    if sec < 60: return f"{sec}s ago"
    if sec < 3600: return f"{sec // 60}m ago"
    if sec < 86400: return f"{sec // 3600}h ago"
    return f"{sec // 86400}d ago"


def _status_dot(s: PluginSnapshot, _model: StatusModel) -> NSView:
    from AppKit import NSBezierPath  # type: ignore[import-not-found]
    color_hex = (
        palette.TEXT_TERTIARY if not s.enabled
        else palette.ERROR if s.consecutive_failures > 0
        else palette.WARNING if s.last_outcome == "running"
        else palette.ACCENT_MINT
    )
    view = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, 10, 10))
    view.setWantsLayer_(True)
    from Quartz import CALayer  # type: ignore[import-not-found]
    layer = CALayer.layer()
    layer.setBackgroundColor_(_to_cg(color_hex))
    layer.setCornerRadius_(5.0)
    layer.setFrame_(view.bounds())
    view.setLayer_(layer)
    return view


def _to_cg(hex_value: str):
    h = hex_value.lstrip("#")
    return NSColor.colorWithSRGBRed_green_blue_alpha_(
        int(h[0:2], 16) / 255.0, int(h[2:4], 16) / 255.0,
        int(h[4:6], 16) / 255.0, 1.0,
    ).CGColor()


# AppKit needs an NSObject target for button clicks; this proxies a Python
# callable.
class _RowTarget:
    _retain: list = []

    @classmethod
    def attach(cls, button, callable_):
        from Foundation import NSObject  # type: ignore[import-not-found]
        class _T(NSObject):
            def call_(self, sender):
                callable_(sender)
        target = _T.alloc().init()
        button.setTarget_(target)
        button.setAction_("call:")
        cls._retain.append(target)  # keep alive
```

- [ ] **Step 2: Add the plugin list into the popover body**

Replace the body section of `popover/root.py`'s `__init__` with:

```python
        from .plugin_row import make_row, ROW_HEIGHT

        # Section body: a flipped, vertically-stacked list of plugin rows.
        from AppKit import NSScrollView, NSClipView  # type: ignore[import-not-found]

        body_height = DEFAULT_HEIGHT - 56  # below the header
        scroll = NSScrollView.alloc().initWithFrame_(
            NSMakeRect(0, 0, WIDTH, body_height)
        )
        scroll.setHasVerticalScroller_(True)
        scroll.setBorderType_(0)
        scroll.setDrawsBackground_(False)

        content = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, WIDTH, 0))
        content.setWantsLayer_(True)
        content.layer().setBackgroundColor_(colors.bg().CGColor())
        scroll.setDocumentView_(content)
        root.addSubview_(scroll)

        def rebuild_rows(_model=None):
            # Clear existing subviews.
            for sv in list(content.subviews()):
                sv.removeFromSuperview()
            ordered = sorted(self._model.plugins, key=lambda p: (
                {"service": 0, "scheduled": 1, "manual": 2}.get(p.kind, 3), p.name
            ))
            y = 0
            for snapshot in ordered:
                row = make_row(
                    snapshot, client=self._client, model=self._model, width=WIDTH,
                )
                row.setFrame_(NSMakeRect(0, y, WIDTH, ROW_HEIGHT))
                content.addSubview_(row)
                y += ROW_HEIGHT
            content.setFrame_(NSMakeRect(0, 0, WIDTH, max(y, body_height)))

        rebuild_rows()
        model.add_observer(rebuild_rows)
```

Also add a `client` parameter to `PopoverRoot.__init__`:

```python
class PopoverRoot:
    def __init__(self, model: StatusModel, client) -> None:
        self._model = model
        self._client = client
        ...
```

And pass it from `app.py`:

```python
        self.popover = PopoverRoot(self.model, self.client)
```

- [ ] **Step 3: Smoke check**

```bash
uv run --package fulcra-menubar python -m fulcra_menubar
```

**Observe:** Click the rumps "Open Fulcra Collect" item. The popover
shows a list grouped by kind. Each row has its status dot (mint /
amber / red / grey), name + id, a relative timestamp ("2m ago"), and a
"Run now" button for enabled scheduled/manual plugins. Click "Run now"
on a plugin — within a second the row's right-side timestamp updates
and the status item's pulse fires.

- [ ] **Step 4: Commit**

```bash
git add packages/menubar/fulcra_menubar/popover/plugin_row.py packages/menubar/fulcra_menubar/popover/root.py packages/menubar/fulcra_menubar/app.py
git commit -m "feat(menubar): popover body — plugin rows grouped by kind

Each row: status dot, plugin name + id, relative last-run timestamp,
and a 'Run now' button (for enabled scheduled/manual plugins; service
plugins show 'Running'/'Crashed' on the right instead). Rows are
sorted by kind first (services, scheduled, manual) then by name.

The popover body is an NSScrollView so 17+ plugins scroll cleanly.

Smoke: Run-now fires the daemon and the row + status item icon
update on the next 2s status poll."
```

---

### Task 15: Bootstrap card (daemon-not-running)

**Files:**
- Create: `packages/menubar/fulcra_menubar/popover/bootstrap.py`
- Modify: `packages/menubar/fulcra_menubar/popover/root.py`

- [ ] **Step 1: Implement `popover/bootstrap.py`**

```python
"""The 'Daemon not running' card that replaces the plugin list when
the control socket is unreachable. Single CTA: 'Install & start daemon'
runs `fulcra-collect service install && fulcra-collect service start`
in a subprocess on a background thread, captures stdout/stderr, and
shows the output in a small label below the button.
"""
from __future__ import annotations

import shutil
import subprocess
import threading

from AppKit import (  # type: ignore[import-not-found]
    NSBezelStyleRounded, NSButton, NSColor, NSScrollView, NSTextField,
    NSTextView, NSView, NSMakeRect, NSMakeSize,
)
from Foundation import NSObject  # type: ignore[import-not-found]

from ..theme import colors, palette, typography


def make_bootstrap_card(width: float, height: float) -> NSView:
    view = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, width, height))
    view.setWantsLayer_(True)
    view.layer().setBackgroundColor_(colors.bg().CGColor())

    title = NSTextField.labelWithString_("Fulcra Collect is not running.")
    title.setFont_(typography.title())
    title.setTextColor_(colors.text())
    title.setFrame_(NSMakeRect(16, height - 56, width - 32, 22))
    view.addSubview_(title)

    body = NSTextField.labelWithString_(
        "The Fulcra Collect daemon hosts your local importers and is "
        "required for this menubar."
    )
    body.setFont_(typography.body())
    body.setTextColor_(colors.text_secondary())
    body.setFrame_(NSMakeRect(16, height - 110, width - 32, 40))
    view.addSubview_(body)

    button = NSButton.alloc().initWithFrame_(NSMakeRect(
        (width - 200) / 2, height - 160, 200, 28,
    ))
    button.setBezelStyle_(NSBezelStyleRounded)

    if shutil.which("fulcra-collect"):
        button.setTitle_("Install & start daemon")
    else:
        button.setTitle_("Install fulcra-collect first")
        button.setEnabled_(False)
    view.addSubview_(button)

    log = NSTextField.labelWithString_("")
    log.setFont_(typography.mono())
    log.setTextColor_(colors.text_tertiary())
    log.setFrame_(NSMakeRect(16, 16, width - 32, height - 196))
    log.setLineBreakMode_(0)  # word-wrap
    view.addSubview_(log)

    def on_click(_sender):
        log.setStringValue_("Running…")
        def work():
            try:
                p1 = subprocess.run(
                    ["fulcra-collect", "service", "install"],
                    capture_output=True, text=True, timeout=30,
                )
                p2 = subprocess.run(
                    ["fulcra-collect", "service", "start"],
                    capture_output=True, text=True, timeout=30,
                )
                output = (p1.stdout + p1.stderr + p2.stdout + p2.stderr).strip()
            except Exception as exc:
                output = f"{type(exc).__name__}: {exc}"
            # Update label on main thread.
            from AppKit import NSOperationQueue  # type: ignore[import-not-found]
            def main():
                log.setStringValue_(output[:400] or "Daemon started.")
            NSOperationQueue.mainQueue().addOperationWithBlock_(main)

        threading.Thread(target=work, daemon=True).start()

    _ButtonTarget.attach(button, on_click)
    return view


class _ButtonTarget:
    _retain: list = []

    @classmethod
    def attach(cls, button, callable_):
        class _T(NSObject):
            def call_(self, sender):
                callable_(sender)
        target = _T.alloc().init()
        button.setTarget_(target)
        button.setAction_("call:")
        cls._retain.append(target)
```

- [ ] **Step 2: Switch the popover body between bootstrap and plugin list**

Modify `popover/root.py`'s `rebuild_rows` to swap the body view based on
`model.daemon_stopped`:

```python
        from .bootstrap import make_bootstrap_card

        body_container = NSView.alloc().initWithFrame_(
            NSMakeRect(0, 0, WIDTH, body_height)
        )
        root.addSubview_(body_container)

        def render(_model=None):
            for sv in list(body_container.subviews()):
                sv.removeFromSuperview()
            if self._model.daemon_stopped:
                card = make_bootstrap_card(WIDTH, body_height)
                body_container.addSubview_(card)
                return
            scroll = NSScrollView.alloc().initWithFrame_(
                NSMakeRect(0, 0, WIDTH, body_height)
            )
            scroll.setHasVerticalScroller_(True)
            scroll.setBorderType_(0)
            scroll.setDrawsBackground_(False)
            content = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, WIDTH, 0))
            content.setWantsLayer_(True)
            content.layer().setBackgroundColor_(colors.bg().CGColor())
            scroll.setDocumentView_(content)
            ordered = sorted(self._model.plugins, key=lambda p: (
                {"service": 0, "scheduled": 1, "manual": 2}.get(p.kind, 3), p.name
            ))
            y = 0
            for snapshot in ordered:
                row = make_row(
                    snapshot, client=self._client, model=self._model, width=WIDTH,
                )
                row.setFrame_(NSMakeRect(0, y, WIDTH, ROW_HEIGHT))
                content.addSubview_(row)
                y += ROW_HEIGHT
            content.setFrame_(NSMakeRect(0, 0, WIDTH, max(y, body_height)))
            body_container.addSubview_(scroll)

        render()
        model.add_observer(render)
```

(Remove the previous `rebuild_rows` and `scroll` instantiation; this
`render` replaces it.)

- [ ] **Step 3: Smoke check**

```bash
# With the daemon NOT running:
pkill -f "fulcra-collect daemon" 2>/dev/null
uv run --package fulcra-menubar python -m fulcra_menubar
```

**Observe:** Open the popover. The body is the bootstrap card: a
title, an explanation, and an "Install & start daemon" button (enabled
because `fulcra-collect` is on PATH). Click it: the label below shows
the installer's stdout/stderr. Within ~10s the popover redraws to show
the plugin list once the daemon answers.

- [ ] **Step 4: Commit**

```bash
git add packages/menubar/fulcra_menubar/popover/bootstrap.py packages/menubar/fulcra_menubar/popover/root.py
git commit -m "feat(menubar): bootstrap card replaces the plugin list when daemon down

When the control socket is unreachable, the popover body swaps to a
single-screen card: title, explanation, and 'Install & start daemon'
button that shells 'fulcra-collect service install && service start'
on a background thread.

If 'fulcra-collect' isn't on PATH the button is disabled with an
'Install fulcra-collect first' label — the spec's hard line that the
.app does not bundle the daemon.

Smoke: stop the daemon, open the popover, click the CTA, watch the
plugin list appear ~10s later as the next status poll succeeds."
```

---

### Task 16: Preferences window + Plugins tab

**Files:**
- Create: `packages/menubar/fulcra_menubar/preferences/__init__.py`
- Create: `packages/menubar/fulcra_menubar/preferences/window.py`
- Create: `packages/menubar/fulcra_menubar/preferences/plugins_tab.py`
- Modify: `packages/menubar/fulcra_menubar/app.py`

- [ ] **Step 1: Create the preferences package marker**

`packages/menubar/fulcra_menubar/preferences/__init__.py`:

```python
```

- [ ] **Step 2: Implement `preferences/window.py`**

```python
"""The Preferences window — NSWindowController hosting an NSTabView.

Tabs: Plugins, Notifications, About. Each tab is a separate NSView
factory in its own module; this file just wires them up.
"""
from __future__ import annotations

from AppKit import (  # type: ignore[import-not-found]
    NSBackingStoreBuffered, NSTabView, NSTabViewItem, NSTitledWindowMask,
    NSWindow, NSWindowController, NSClosableWindowMask, NSMiniaturizableWindowMask,
    NSMakeRect,
)

from ..daemon_client import DaemonClient
from ..model import StatusModel
from ..notifications import NotificationCentre


WIDTH = 640.0
HEIGHT = 480.0


class PreferencesController(NSWindowController):
    @classmethod
    def create(cls, *, model: StatusModel, client: DaemonClient,
                centre: NotificationCentre) -> "PreferencesController":
        window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, WIDTH, HEIGHT),
            NSTitledWindowMask | NSClosableWindowMask | NSMiniaturizableWindowMask,
            NSBackingStoreBuffered, False,
        )
        window.setTitle_("Fulcra Collect — Preferences")
        window.center()

        tabs = NSTabView.alloc().initWithFrame_(NSMakeRect(0, 0, WIDTH, HEIGHT - 22))

        from .plugins_tab import make_plugins_tab
        from .notifications_tab import make_notifications_tab
        from .about_tab import make_about_tab

        plugins_view = make_plugins_tab(model=model, client=client)
        notifs_view = make_notifications_tab(centre=centre)
        about_view = make_about_tab(client=client)

        for label, view in (
            ("Plugins", plugins_view),
            ("Notifications", notifs_view),
            ("About", about_view),
        ):
            item = NSTabViewItem.alloc().initWithIdentifier_(label)
            item.setLabel_(label)
            item.setView_(view)
            tabs.addTabViewItem_(item)

        window.contentView().addSubview_(tabs)

        controller = cls.alloc().initWithWindow_(window)
        return controller
```

- [ ] **Step 3: Implement `preferences/plugins_tab.py`**

```python
"""The Plugins tab. One expanded row per plugin: enable toggle,
interval input (scheduled only), credentials (Connect / Disconnect),
Run now button.

Edits to enable / interval are persisted via fulcra_collect.config,
followed by a daemon `reload`. Credentials writes go through the
daemon's set_credential / delete_credential — never via the keychain
directly.
"""
from __future__ import annotations

from AppKit import (  # type: ignore[import-not-found]
    NSButton, NSBezelStyleRounded, NSScrollView, NSSecureTextField,
    NSStackView, NSStackViewDistributionFill, NSSwitch, NSTextField,
    NSView, NSMakeRect, NSUserInterfaceLayoutOrientationVertical,
)
from Foundation import NSObject  # type: ignore[import-not-found]

from fulcra_collect import config as _config

from ..daemon_client import DaemonClient
from ..model import PluginSnapshot, StatusModel
from ..theme import colors, palette, typography


def make_plugins_tab(*, model: StatusModel, client: DaemonClient) -> NSView:
    width = 640.0
    height = 440.0
    scroll = NSScrollView.alloc().initWithFrame_(NSMakeRect(0, 0, width, height))
    scroll.setHasVerticalScroller_(True)
    scroll.setBorderType_(0)
    scroll.setDrawsBackground_(False)

    content = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, width, 0))
    content.setWantsLayer_(True)
    content.layer().setBackgroundColor_(colors.bg().CGColor())
    scroll.setDocumentView_(content)

    def rebuild(_model=None):
        for sv in list(content.subviews()):
            sv.removeFromSuperview()
        y = 0
        ordered = sorted(model.plugins, key=lambda p: (p.kind, p.name))
        for snap in ordered:
            row_height = 80 + 24 * len(_creds_for(snap))
            row = _make_plugin_row(snap, width, row_height, client=client, model=model)
            row.setFrame_(NSMakeRect(0, y, width, row_height))
            content.addSubview_(row)
            y += row_height
        content.setFrame_(NSMakeRect(0, 0, width, max(y, height)))

    rebuild()
    model.add_observer(rebuild)
    return scroll


def _creds_for(snap: PluginSnapshot) -> list:
    # The snapshot doesn't include the plugin's required_credentials
    # declaration — only the daemon-side registry knows. The Plugins
    # tab fetches credential_status on tab build to learn the keys.
    return []  # populated below via the deferred fetch


def _make_plugin_row(snap: PluginSnapshot, width: float, height: float,
                     *, client: DaemonClient, model: StatusModel) -> NSView:
    row = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, width, height))

    name = NSTextField.labelWithString_(f"{snap.name}  ({snap.id})")
    name.setFont_(typography.body())
    name.setTextColor_(colors.text())
    name.setFrame_(NSMakeRect(16, height - 28, width - 200, 18))
    row.addSubview_(name)

    enabled_switch = NSSwitch.alloc().initWithFrame_(
        NSMakeRect(width - 80, height - 32, 50, 22)
    )
    enabled_switch.setState_(1 if snap.enabled else 0)

    def on_toggle(sender):
        cfg = _config.load()
        if sender.state():
            cfg.enable(snap.id)
        else:
            cfg.disable(snap.id)
        _config.save(cfg)
        client.reload()
    _Target.attach(enabled_switch, on_toggle)
    row.addSubview_(enabled_switch)

    # Interval input — scheduled only.
    if snap.kind == "scheduled":
        cfg = _config.load()
        seconds = cfg.interval_overrides.get(snap.id, 3600)
        interval_label = NSTextField.labelWithString_("Interval (minutes):")
        interval_label.setFont_(typography.small())
        interval_label.setFrame_(NSMakeRect(16, height - 56, 140, 16))
        row.addSubview_(interval_label)

        interval_field = NSTextField.alloc().initWithFrame_(
            NSMakeRect(160, height - 60, 60, 22)
        )
        interval_field.setStringValue_(str(max(seconds // 60, 1)))

        def on_interval_change(sender):
            try:
                minutes = max(int(sender.stringValue()), 1)
            except ValueError:
                return
            cfg2 = _config.load()
            cfg2.set_interval(snap.id, minutes * 60)
            _config.save(cfg2)
            client.reload()
        _Target.attach(interval_field, on_interval_change, action="textChanged:")
        row.addSubview_(interval_field)

    # Run now (manual + scheduled, only when enabled).
    if snap.enabled and snap.kind in ("manual", "scheduled"):
        run_btn = NSButton.alloc().initWithFrame_(
            NSMakeRect(width - 200, 16, 100, 24)
        )
        run_btn.setTitle_("Run now")
        run_btn.setBezelStyle_(NSBezelStyleRounded)

        def on_run(_s):
            client.run(snap.id)
            model.mark_in_flight(snap.id)
        _Target.attach(run_btn, on_run)
        row.addSubview_(run_btn)

    # Credentials block — fetched live.
    try:
        cred_status = client.credential_status(snap.id)
        credentials = cred_status.get("credentials", {}) if cred_status.get("ok") else {}
    except Exception:
        credentials = {}

    yoff = 16 + 24
    for key, state in credentials.items():
        label = NSTextField.labelWithString_(f"  {key}: ")
        label.setFont_(typography.small())
        label.setTextColor_(colors.text_secondary())
        label.setFrame_(NSMakeRect(16, yoff, 220, 16))
        row.addSubview_(label)

        if state == "set":
            badge = NSTextField.labelWithString_("Connected")
            badge.setFont_(typography.small())
            badge.setTextColor_(colors.mint())
            badge.setFrame_(NSMakeRect(220, yoff, 100, 16))
            row.addSubview_(badge)

            disc = NSButton.alloc().initWithFrame_(NSMakeRect(330, yoff - 4, 100, 24))
            disc.setTitle_("Disconnect")
            disc.setBezelStyle_(NSBezelStyleRounded)
            _Target.attach(disc, lambda _s, key=key: (
                client.delete_credential(snap.id, key),
            ))
            row.addSubview_(disc)
        else:
            field = NSSecureTextField.alloc().initWithFrame_(
                NSMakeRect(220, yoff - 2, 200, 22)
            )
            field.setPlaceholderString_("paste secret")
            row.addSubview_(field)

            conn = NSButton.alloc().initWithFrame_(NSMakeRect(430, yoff - 4, 80, 24))
            conn.setTitle_("Connect")
            conn.setBezelStyle_(NSBezelStyleRounded)
            _Target.attach(conn, lambda _s, key=key, field=field: (
                client.set_credential(snap.id, key, field.stringValue()),
            ))
            row.addSubview_(conn)

        yoff += 24

    return row


class _Target:
    _retain: list = []

    @classmethod
    def attach(cls, control, callable_, action: str = "call:"):
        class _T(NSObject):
            def call_(self, sender):
                callable_(sender)
        target = _T.alloc().init()
        control.setTarget_(target)
        control.setAction_(action)
        cls._retain.append(target)
```

- [ ] **Step 4: Stub `notifications_tab.py` and `about_tab.py`**

(These get filled out in Task 17. Stub them now so the window builds.)

`packages/menubar/fulcra_menubar/preferences/notifications_tab.py`:

```python
"""Notifications tab — filled in Task 17."""
from __future__ import annotations

from AppKit import NSTextField, NSView, NSMakeRect  # type: ignore[import-not-found]

from ..notifications import NotificationCentre
from ..theme import colors, typography


def make_notifications_tab(*, centre: NotificationCentre):
    view = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, 640, 440))
    label = NSTextField.labelWithString_("Notifications — coming in Task 17.")
    label.setFont_(typography.body())
    label.setTextColor_(colors.text_secondary())
    label.setFrame_(NSMakeRect(16, 400, 600, 18))
    view.addSubview_(label)
    return view
```

`packages/menubar/fulcra_menubar/preferences/about_tab.py`:

```python
"""About tab — filled in Task 17."""
from __future__ import annotations

from AppKit import NSTextField, NSView, NSMakeRect  # type: ignore[import-not-found]

from ..daemon_client import DaemonClient
from ..theme import colors, typography


def make_about_tab(*, client: DaemonClient):
    view = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, 640, 440))
    label = NSTextField.labelWithString_("About — coming in Task 17.")
    label.setFont_(typography.body())
    label.setTextColor_(colors.text_secondary())
    label.setFrame_(NSMakeRect(16, 400, 600, 18))
    view.addSubview_(label)
    return view
```

- [ ] **Step 5: Wire Preferences into `app.py`**

Add to `FulcraMenubarApp`:

```python
        # Notification centre — real PyObjC post path lands in Task 18.
        from .notifications import NotificationCentre
        self.notifications = NotificationCentre(
            post=lambda title, body: print(f"[notify] {title}: {body}"),
        )
        # Hook failure-threshold transitions to notifications.
        self.model.add_failure_transition_observer(
            lambda pid: self.notifications.notify_failure(pid, "consecutive failures ≥ 3")
        )

        self._prefs_controller = None

    @rumps.clicked("Preferences…")
    def _open_prefs(self, _sender) -> None:
        from .preferences.window import PreferencesController
        if self._prefs_controller is None:
            self._prefs_controller = PreferencesController.create(
                model=self.model, client=self.client, centre=self.notifications,
            )
        self._prefs_controller.window().makeKeyAndOrderFront_(None)
        from AppKit import NSApp  # type: ignore[import-not-found]
        NSApp.activateIgnoringOtherApps_(True)
```

Update `self.menu`:

```python
        self.menu = ["Open Fulcra Collect", "Preferences…", None, "Quit"]
```

- [ ] **Step 6: Smoke check**

```bash
uv run --package fulcra-menubar python -m fulcra_menubar
```

**Observe:** Click Preferences from the rumps menu. The window opens
with three tabs: Plugins, Notifications, About. The Plugins tab lists
every plugin; toggling the enable switch updates config.toml and the
plugin list refreshes within ~10s. Setting an interval persists.
Connecting a credential calls `set_credential` and the "Disconnect"
state appears. Disconnect calls `delete_credential` and the secure
text field returns.

- [ ] **Step 7: Commit**

```bash
git add packages/menubar/fulcra_menubar/preferences/ packages/menubar/fulcra_menubar/app.py
git commit -m "feat(menubar): preferences window + Plugins tab

NSWindowController hosting an NSTabView with three tabs. Plugins tab
is fully wired: enable toggle (writes config.toml + reload), interval
input (scheduled only, writes interval_overrides + reload), Connect /
Disconnect for each required credential (set_credential /
delete_credential), and a Run now button.

Notifications and About tabs are placeholders that the next task
fills in.

Smoke: enable lastfm, set its interval to 5 minutes, paste a session
key, watch the row redraw to 'Connected', click Disconnect, watch it
flip back."
```

---

### Task 17: Notifications + About tabs

**Files:**
- Modify: `packages/menubar/fulcra_menubar/preferences/notifications_tab.py`
- Modify: `packages/menubar/fulcra_menubar/preferences/about_tab.py`

- [ ] **Step 1: Implement `notifications_tab.py`**

Replace its contents with:

```python
"""Notifications tab — failure-threshold + mute-all toggles."""
from __future__ import annotations

from AppKit import (  # type: ignore[import-not-found]
    NSSwitch, NSTextField, NSView, NSMakeRect,
)
from Foundation import NSObject  # type: ignore[import-not-found]

from ..notifications import NotificationCentre
from ..theme import colors, typography


def make_notifications_tab(*, centre: NotificationCentre):
    view = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, 640, 440))

    title = NSTextField.labelWithString_("Notify me when a plugin fails repeatedly")
    title.setFont_(typography.body())
    title.setTextColor_(colors.text())
    title.setFrame_(NSMakeRect(16, 400, 500, 18))
    view.addSubview_(title)

    note = NSTextField.labelWithString_(
        "After 3 consecutive failures. At most one notification per plugin per hour."
    )
    note.setFont_(typography.small())
    note.setTextColor_(colors.text_secondary())
    note.setFrame_(NSMakeRect(16, 380, 500, 16))
    view.addSubview_(note)

    fail_switch = NSSwitch.alloc().initWithFrame_(NSMakeRect(560, 396, 50, 22))
    fail_switch.setState_(0 if centre.mute_all else 1)
    view.addSubview_(fail_switch)

    mute_title = NSTextField.labelWithString_("Mute all notifications")
    mute_title.setFont_(typography.body())
    mute_title.setTextColor_(colors.text())
    mute_title.setFrame_(NSMakeRect(16, 340, 500, 18))
    view.addSubview_(mute_title)

    mute_switch = NSSwitch.alloc().initWithFrame_(NSMakeRect(560, 336, 50, 22))
    mute_switch.setState_(1 if centre.mute_all else 0)

    def on_fail_change(sender):
        # We map the "notify on failure" toggle to NOT-mute-all, since
        # mute_all is the master kill-switch. If the user turns failure
        # notifications off, we set mute_all True. If on, we leave
        # mute_all untouched (the master toggle separately controls it).
        if sender.state() == 0:
            centre.mute_all = True
            mute_switch.setState_(1)
    _T.attach(fail_switch, on_fail_change)

    def on_mute_change(sender):
        centre.mute_all = bool(sender.state())
        if centre.mute_all:
            fail_switch.setState_(0)
        else:
            fail_switch.setState_(1)
    _T.attach(mute_switch, on_mute_change)
    view.addSubview_(mute_switch)

    return view


class _T:
    _retain: list = []

    @classmethod
    def attach(cls, control, fn):
        class _Target(NSObject):
            def call_(self, sender):
                fn(sender)
        target = _Target.alloc().init()
        control.setTarget_(target)
        control.setAction_("call:")
        cls._retain.append(target)
```

- [ ] **Step 2: Implement `about_tab.py`**

Replace its contents with:

```python
"""About tab — versions, paths, Open Logs, Launch-at-login."""
from __future__ import annotations

import importlib.metadata as _im
import subprocess
from pathlib import Path

from AppKit import (  # type: ignore[import-not-found]
    NSButton, NSBezelStyleRounded, NSSwitch, NSTextField, NSView, NSMakeRect,
)
from Foundation import NSObject  # type: ignore[import-not-found]

from fulcra_collect import config as _config

from ..daemon_client import DaemonClient, DaemonUnavailable
from ..theme import colors, palette, typography


def make_about_tab(*, client: DaemonClient):
    view = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, 640, 440))

    try:
        app_version = _im.version("fulcra-menubar")
    except _im.PackageNotFoundError:
        app_version = "0.1.0"

    try:
        version_reply = client.version()
        daemon_version = version_reply.get("daemon_version", "unknown")
        plugin_versions = version_reply.get("plugins", {})
    except DaemonUnavailable:
        daemon_version = "(daemon stopped)"
        plugin_versions = {}

    def row(label_text: str, value_text: str, y: float):
        l = NSTextField.labelWithString_(label_text)
        l.setFont_(typography.small())
        l.setTextColor_(colors.text_secondary())
        l.setFrame_(NSMakeRect(16, y, 220, 16))
        view.addSubview_(l)
        v = NSTextField.labelWithString_(value_text)
        v.setFont_(typography.small())
        v.setTextColor_(colors.text())
        v.setFrame_(NSMakeRect(240, y, 400, 16))
        view.addSubview_(v)

    row("App version", app_version, 410)
    row("Daemon version", daemon_version, 390)
    row("Config", str(_config.config_dir() / "config.toml"), 360)
    row("State directory", str(_config.config_dir() / "state"), 340)

    plugin_y = 300
    plugin_header = NSTextField.labelWithString_("Plugin versions")
    plugin_header.setFont_(typography.body())
    plugin_header.setTextColor_(colors.text())
    plugin_header.setFrame_(NSMakeRect(16, plugin_y, 400, 18))
    view.addSubview_(plugin_header)
    plugin_y -= 22
    for pid in sorted(plugin_versions):
        row(f"  {pid}", plugin_versions[pid], plugin_y)
        plugin_y -= 18

    # Open logs.
    logs_btn = NSButton.alloc().initWithFrame_(NSMakeRect(16, 60, 200, 28))
    logs_btn.setTitle_("Open Activity Logs")
    logs_btn.setBezelStyle_(NSBezelStyleRounded)

    def on_logs(_s):
        # The daemon's launchd log path; falls back to Console.app open.
        log = Path.home() / "Library" / "Logs" / "com.fulcradynamics.collect.log"
        subprocess.Popen(["open", "-a", "Console", str(log) if log.exists() else "/var/log/system.log"])
    _T.attach(logs_btn, on_logs)
    view.addSubview_(logs_btn)

    # Launch at login toggle.
    launch_label = NSTextField.labelWithString_("Launch at login")
    launch_label.setFont_(typography.body())
    launch_label.setTextColor_(colors.text())
    launch_label.setFrame_(NSMakeRect(16, 24, 400, 18))
    view.addSubview_(launch_label)

    launch_switch = NSSwitch.alloc().initWithFrame_(NSMakeRect(560, 20, 50, 22))
    launch_switch.setState_(1 if _is_login_item() else 0)

    def on_launch_change(sender):
        if sender.state():
            _register_login_item()
        else:
            _unregister_login_item()
    _T.attach(launch_switch, on_launch_change)
    view.addSubview_(launch_switch)

    return view


def _is_login_item() -> bool:
    try:
        from ServiceManagement import SMAppService  # type: ignore[import-not-found]
    except ImportError:
        return False
    svc = SMAppService.mainAppService()
    return svc.status() == 1  # SMAppServiceStatusEnabled


def _register_login_item() -> None:
    try:
        from ServiceManagement import SMAppService  # type: ignore[import-not-found]
    except ImportError:
        return
    svc = SMAppService.mainAppService()
    err = None
    svc.registerAndReturnError_(err)


def _unregister_login_item() -> None:
    try:
        from ServiceManagement import SMAppService  # type: ignore[import-not-found]
    except ImportError:
        return
    svc = SMAppService.mainAppService()
    err = None
    svc.unregisterAndReturnError_(err)


class _T:
    _retain: list = []

    @classmethod
    def attach(cls, control, fn):
        class _Target(NSObject):
            def call_(self, sender):
                fn(sender)
        target = _Target.alloc().init()
        control.setTarget_(target)
        control.setAction_("call:")
        cls._retain.append(target)
```

- [ ] **Step 3: Smoke check**

```bash
uv run --package fulcra-menubar python -m fulcra_menubar
```

**Observe:** Open Preferences > About. The fields are populated (app
version, daemon version, config path, state path, plugin versions
from the new `version` handler). Open Activity Logs opens Console.app.
Toggle "Launch at login" on and off; verify with:

```bash
plutil -p ~/Library/LaunchAgents/com.fulcradynamics.collect.menubar.plist 2>/dev/null || \
    echo "(no login item — modern SMAppService manages it internally)"
```

(Note: `SMAppService.mainAppService()` only works once the app is
packaged. Outside py2app, the launch-at-login toggle is a no-op — the
spec explicitly allows this; document in README.)

Open Preferences > Notifications. Toggle each switch and verify the
underlying NotificationCentre state changes (print statements in
`_open_prefs` or just trust the assignment).

- [ ] **Step 4: Commit**

```bash
git add packages/menubar/fulcra_menubar/preferences/notifications_tab.py packages/menubar/fulcra_menubar/preferences/about_tab.py
git commit -m "feat(menubar): preferences — Notifications and About tabs filled in

Notifications tab: failure-on toggle + mute-all toggle, coupled so
that 'mute all' wins.

About tab: app version, daemon version (via the new {\"cmd\":\"version\"}
handler), config + state paths, per-plugin versions, Open Activity
Logs button (launches Console.app), Launch-at-login toggle (a no-op
unless the app is packaged via py2app — SMAppService.mainAppService
requires a real bundle identifier; document in README).

All three preferences tabs are now live."
```

---

## Phase E — Wire-up

### Task 18: Real `UserNotifications` post path

**Files:**
- Modify: `packages/menubar/fulcra_menubar/app.py`

- [ ] **Step 1: Replace the stub `post` callback with a real PyObjC post**

In `app.py`, replace:

```python
        self.notifications = NotificationCentre(
            post=lambda title, body: print(f"[notify] {title}: {body}"),
        )
```

with:

```python
        from .notifications import NotificationCentre
        self.notifications = NotificationCentre(post=self._post_notification)
        self._request_notification_authorization()
```

Add to the class:

```python
    def _request_notification_authorization(self) -> None:
        try:
            from UserNotifications import (  # type: ignore[import-not-found]
                UNAuthorizationOptionAlert, UNAuthorizationOptionSound,
                UNUserNotificationCenter,
            )
        except ImportError:
            return
        centre = UNUserNotificationCenter.currentNotificationCenter()
        opts = UNAuthorizationOptionAlert | UNAuthorizationOptionSound

        def handler(granted, err):
            if err is not None:
                logger.warning("UN authorization error: %s", err)
        centre.requestAuthorizationWithOptions_completionHandler_(opts, handler)

    def _post_notification(self, title: str, body: str) -> None:
        try:
            from UserNotifications import (  # type: ignore[import-not-found]
                UNMutableNotificationContent, UNNotificationRequest,
                UNUserNotificationCenter,
            )
        except ImportError:
            print(f"[notify] {title}: {body}")
            return
        import uuid
        content = UNMutableNotificationContent.alloc().init()
        content.setTitle_(title)
        content.setBody_(body)
        request = UNNotificationRequest.requestWithIdentifier_content_trigger_(
            str(uuid.uuid4()), content, None,
        )
        UNUserNotificationCenter.currentNotificationCenter() \
            .addNotificationRequest_withCompletionHandler_(request, None)
```

- [ ] **Step 2: Smoke check**

```bash
# Force a plugin to consecutive_failures >= 3 (see Task 12's smoke).
uv run --package fulcra-menubar python -m fulcra_menubar
```

**Observe:** macOS prompts "Fulcra Collect would like to send you
notifications" the first time. After the plugin fails its 3rd time in
a row, a native notification appears: "lastfm is failing — consecutive
failures ≥ 3".

- [ ] **Step 3: Commit**

```bash
git add packages/menubar/fulcra_menubar/app.py
git commit -m "feat(menubar): real UserNotifications post path

Replaces the stub print-callback with PyObjC calls into
UNUserNotificationCenter. Authorization is requested on first launch;
the macOS prompt fires once and then the system remembers the choice.

NotificationCentre's de-dup logic stays the same — failures still cap
at one notification per plugin per hour even though the post is now
'real'."
```

---

### Task 19: Sleep/wake observers + login-item registration

**Files:**
- Modify: `packages/menubar/fulcra_menubar/app.py`

- [ ] **Step 1: Wire NSWorkspace sleep/wake observers to the poller**

In `app.py` `__init__`, after starting the polling thread:

```python
        self._install_sleep_wake_observers()
```

Add to the class:

```python
    def _install_sleep_wake_observers(self) -> None:
        from AppKit import NSWorkspace  # type: ignore[import-not-found]
        from Foundation import NSObject  # type: ignore[import-not-found]
        centre = NSWorkspace.sharedWorkspace().notificationCenter()

        outer = self

        class _Listener(NSObject):
            def onSleep_(self, _n):
                outer.poller.suspend()

            def onWake_(self, _n):
                outer.poller.resume()

        self._sleep_listener = _Listener.alloc().init()
        centre.addObserver_selector_name_object_(
            self._sleep_listener, "onSleep:",
            "NSWorkspaceWillSleepNotification", None,
        )
        centre.addObserver_selector_name_object_(
            self._sleep_listener, "onWake:",
            "NSWorkspaceDidWakeNotification", None,
        )
```

- [ ] **Step 2: Smoke check (manual)**

Run the menubar. Put the machine to sleep (close the lid for a few seconds, or use `pmset sleepnow` if you accept the immediate sleep). On wake, observe:

- The next status poll fires immediately rather than after a 10s heartbeat.
- The status item icon refreshes within ~2s of unlock.

(This is an observation, not a `pytest` assertion. The `PollingScheduler`'s sleep-handling logic is already covered in `test_polling.py::test_sleep_suspends_ticking`.)

- [ ] **Step 3: Commit**

```bash
git add packages/menubar/fulcra_menubar/app.py
git commit -m "feat(menubar): sleep/wake observers suspend and resume polling

Subscribes to NSWorkspaceWillSleepNotification and
NSWorkspaceDidWakeNotification through PyObjC; calls
PollingScheduler.suspend() / resume() respectively. On wake, the
scheduler fires its next tick immediately — so a laptop that's been
asleep for hours shows its real state within seconds of unlock, not
after the next 10s heartbeat.

Sleep/wake logic itself was tested in test_polling.py; this commit
just wires the AppKit notifications to the scheduler."
```

---

## Phase F — Build, smoke, ship

### Task 20: `py2app` build

**Files:**
- Create: `packages/menubar/setup.py`

- [ ] **Step 1: Create `setup.py`**

```python
"""py2app entry point.

    cd packages/menubar
    uv run python setup.py py2app

For development iteration ('alias' build that links to source rather
than copying):

    uv run python setup.py py2app -A
"""
from setuptools import setup

APP = ["fulcra_menubar/__main__.py"]
DATA_FILES = [
    ("fulcra_menubar/assets", [
        "fulcra_menubar/assets/menubar-icon.pdf",
    ]),
]
OPTIONS = {
    "argv_emulation": False,
    "plist": {
        "CFBundleName": "Fulcra Collect",
        "CFBundleDisplayName": "Fulcra Collect",
        "CFBundleIdentifier": "com.fulcradynamics.collect.menubar",
        "CFBundleVersion": "0.1.0",
        "CFBundleShortVersionString": "0.1.0",
        "LSUIElement": True,                # menubar app — no dock icon
        "NSHumanReadableCopyright": "Fulcra Dynamics",
    },
    "packages": [
        "rumps", "fulcra_collect", "fulcra_menubar", "tomlkit", "keyring",
    ],
    "includes": [
        "AppKit", "Foundation", "Quartz", "ServiceManagement",
        "UserNotifications",
    ],
}

setup(
    app=APP,
    data_files=DATA_FILES,
    options={"py2app": OPTIONS},
    setup_requires=["py2app"],
)
```

- [ ] **Step 2: Build an alias `.app` for dev**

```bash
cd /Users/Scanning/Developer/fulcra-tools
uv sync --extra macos --extra build --package fulcra-menubar
cd packages/menubar
uv run python setup.py py2app -A
```

Expected: `dist/Fulcra Collect.app` exists. `ls -la dist/` shows the
bundle.

- [ ] **Step 3: Run the .app and smoke**

```bash
open "dist/Fulcra Collect.app"
```

**Observe:** The menubar icon appears. There's no dock icon (because
`LSUIElement: True`). Click the icon → rumps menu → Open Fulcra
Collect → popover renders. Quit through the menu cleanly.

- [ ] **Step 4: Commit**

```bash
git add packages/menubar/setup.py
git commit -m "feat(menubar): py2app build configuration

Produces 'Fulcra Collect.app' as a menubar-only app (LSUIElement=True
— no dock icon). 'python setup.py py2app -A' produces an alias build
that links to source for fast dev iteration; the full build copies
Python + PyObjC into the .app for distribution.

Code signing and notarization are out of scope for this plan (they
land in sub-project 3). The unsigned .app will trip Gatekeeper on
first launch — acceptable for v1, the team uses 'right-click → Open'."
```

---

### Task 21: README + manual smoke checklist

**Files:**
- Modify: `packages/menubar/README.md`

- [ ] **Step 1: Expand the README**

Replace the README's contents with:

````markdown
# fulcra-menubar

macOS menubar UI for `fulcra-collect`. Python + PyObjC + rumps v1; a
Swift rewrite follows once the UX is locked (see
`docs/superpowers/specs/2026-05-22-fulcra-collect-menubar-design.md`).

## Run in dev mode

    cd /Users/Scanning/Developer/fulcra-tools
    uv sync --extra macos --package fulcra-menubar
    uv run --package fulcra-menubar python -m fulcra_menubar

The daemon must be running (`fulcra-collect service start`). The
menubar icon appears in the top-right of the screen; click for the
rumps menu, then "Open Fulcra Collect" for the popover.

## Tests

    uv run pytest packages/menubar/tests/ -q

The pure-model layer (daemon_client, model, polling, notifications)
runs everywhere — Linux CI included. The view layer (status_item,
popover, preferences) is exercised by manual smoke; see the checklist
below.

## Build the .app

    uv sync --extra macos --extra build --package fulcra-menubar
    cd packages/menubar
    uv run python setup.py py2app -A      # alias build for dev
    uv run python setup.py py2app         # distributable build

The unsigned `.app` lands in `packages/menubar/dist/Fulcra Collect.app`.
The first launch will trip Gatekeeper (right-click → Open to bypass).
Code-signing and notarization land in sub-project 3.

## Manual smoke checklist

Run before merging any view-layer change.

- [ ] Daemon stopped → popover shows bootstrap card with "Install &
      start daemon" enabled.
- [ ] Daemon stopped → menubar icon at 40% opacity.
- [ ] Daemon running, no failures → popover shows plugin list grouped
      by kind; status pill is "Healthy" (mint dot); icon is opaque, no
      badge.
- [ ] Force a plugin to ≥3 consecutive failures → popover row shows red
      dot + error line; status pill flips to "N failing"; menubar icon
      gets a red dot; a macOS notification appears (once per hour).
- [ ] Click "Run now" on a manual plugin → row updates, menubar icon
      pulses violet for ~the duration of the run.
- [ ] Preferences > Plugins → toggle Enable → `~/.config/fulcra-collect/config.toml`
      reflects the change; popover plugin list redraws.
- [ ] Preferences > Plugins → set interval → config.toml's
      `interval_overrides` updates; daemon reloads.
- [ ] Preferences > Plugins → Connect credential → daemon receives
      `set_credential`; `credential_status` flips to "set" on next
      tab redraw; Disconnect reverses it.
- [ ] Preferences > Notifications → Mute all → no notifications fire.
- [ ] Preferences > About → daemon version + plugin versions populate.
- [ ] Lid close / wake → next status poll fires immediately on wake.
- [ ] Quit from the rumps menu → app exits cleanly; daemon keeps
      running.

## Architecture

Two layers:

1. **Pure-model layer** — no PyObjC imports, full unit tests.
   - `daemon_client.py` — typed wrapper over
     `fulcra_collect.control.send_request`.
   - `model.py` — `StatusModel`: snapshot + in-flight + observer
     protocol + failure-transition observer.
   - `polling.py` — `PollingScheduler` (2s open / 10s closed,
     sleep-aware).
   - `notifications.py` — failure-notification de-dup
     (1/category/hour).
   - `theme/palette.py` — hex constants.

2. **View layer** — PyObjC; manual smoke only.
   - `app.py` — `rumps.App` subclass, wires everything.
   - `status_item.py` — menubar icon + badge + running pulse.
   - `popover/*` — the click-to-show popover (header, plugin rows,
     bootstrap card).
   - `preferences/*` — NSWindowController + tabs.
   - `theme/colors.py`, `theme/typography.py` — PyObjC NSColor / NSFont
     factories.

The daemon owns all plugin logic, scheduling, supervision, watermarks,
and credentials. This app is a thin client; it reads daemon state and
issues control-socket commands.

## Path to Swift

Per the spec, this Python build is the UX laboratory. Once the
popover layout, Preferences structure, notification triggers, palette,
bootstrap copy, and icon assets are locked, the Swift port begins as
sub-project 2.5. The Python file boundaries were chosen to map 1:1 to
Swift files — see the spec's "UX lock and the Swift handoff" section.
````

- [ ] **Step 2: Commit**

```bash
git add packages/menubar/README.md
git commit -m "docs(menubar): README with dev workflow, build steps, smoke checklist

Walks a new contributor from 'cloned the monorepo' to 'menubar app on
screen' in three commands. Lists the manual smoke checklist as the
contract the view-layer changes must meet before merging. Repeats the
two-layer architecture (pure-model with tests, view with smoke) and
points at the spec's UX-lock criteria for the Swift handoff."
```

---

### Task 22: Final orphan/obsolete sweep + workspace verification

**Files:** none new — this is a pre-push hygiene task per the user's global rule.

- [ ] **Step 1: Run the whole workspace test suite**

```bash
cd /Users/Scanning/Developer/fulcra-tools
uv run pytest -q
```

Expected: every package green, including the new `fulcra-menubar` tests
and the four new `fulcra-collect` daemon-handler tests. No regressions
in any of the existing 17-plugin discovery.

- [ ] **Step 2: Run `ruff` over the new code**

```bash
uv run ruff check packages/collect/ packages/menubar/
```

Expected: clean.

- [ ] **Step 3: Search the new code for dead leftovers**

```bash
grep -nr "TODO\|FIXME\|XXX\|HACK" packages/menubar/ packages/collect/ \
    --include="*.py" || echo "no markers"
```

Manually inspect each hit; remove markers that point at finished work,
keep ones that document genuine open questions referenced in the spec.

- [ ] **Step 4: Verify the workspace's plugin discovery still finds all 17 plugins**

```bash
uv run --package fulcra-collect python -c "
from fulcra_collect.registry import discover
result = discover()
print(len(result.plugins), 'plugins;', len(result.errors), 'errors')
for pid in sorted(result.plugins): print(' -', pid)
for entry, err in result.errors.items(): print(' !', entry, err)
"
```

Expected: `17 plugins; 0 errors` with the same list as before.

- [ ] **Step 5: Smoke the .app one more time end-to-end**

Build a fresh non-alias `.app` and run through the README's manual
smoke checklist top to bottom.

```bash
cd packages/menubar && rm -rf build/ dist/
uv run python setup.py py2app
open "dist/Fulcra Collect.app"
```

- [ ] **Step 6: Final commit (if anything changed in Steps 1–5)**

If `ruff`, the orphan sweep, or the smoke uncovered anything, commit
the fixes:

```bash
git add -p   # carefully — never -A, per the global rule
git commit -m "chore(menubar): pre-push sweep — orphan/obsolete review

Fixes uncovered during the global pre-push orphan/obsolete sweep:
[describe each finding briefly]

Per the user's CLAUDE.md rule: never push without this pass."
```

If nothing changed, skip this step.

- [ ] **Step 7: Hand the branch back to the user for push approval**

Do **not** run `git push`. Report to the user that all 22 tasks are
green, the manual smoke checklist is signed off, the workspace tests
are green, the orphan sweep is done, and the commits are sitting on
`main` waiting for an explicit "push" go-ahead.

---

## How it works

The menubar process is a `rumps.App` subclass that owns the
**StatusModel** and a **PollingScheduler**. The scheduler runs on a
background thread and calls `DaemonClient.status()` every 2s while the
popover is open and every 10s while it is closed. Each successful poll
publishes a snapshot into the model; each failed poll marks the model
as daemon-stopped. The model fires its observers, which redraw the
status item, the popover (if shown), and the preferences (if open).

Failure-threshold transitions feed the `NotificationCentre`, which
de-dups to one post per (plugin, hour) and forwards to
`UNUserNotificationCenter` on macOS.

User actions in the popover and preferences turn into control-socket
commands through the `DaemonClient` — `run`, `reload`,
`set_credential`, `delete_credential`. Config edits write
`~/.config/fulcra-collect/config.toml` directly (preserving comments
via `tomlkit`) and then send `{"cmd":"reload"}`. Two clients (the CLI
and the menubar) editing the same file is acceptable because writes
are small, the schema is fixed, and the daemon re-reads on reload.

The daemon owns all the real work. This app reads its state and pushes
its buttons.

## Usage

After Task 22 ships:

    # one-time install (until the homebrew bottle of sub-project 3):
    cd /Users/Scanning/Developer/fulcra-tools
    uv sync --extra macos --extra build --package fulcra-menubar
    cd packages/menubar
    uv run python setup.py py2app
    cp -R dist/"Fulcra Collect.app" /Applications/

    # run:
    open /Applications/"Fulcra Collect.app"
    # then enable "Launch at login" from Preferences > About.

## Develop

    # alias build, live-edits in fulcra_menubar/ reflect in dist/:
    cd packages/menubar
    uv run python setup.py py2app -A
    open dist/"Fulcra Collect.app"

    # or skip py2app entirely:
    uv run --package fulcra-menubar python -m fulcra_menubar

## Self-Review

Performed by the plan author after writing, before handing off:

**Spec coverage:** every section of `docs/superpowers/specs/2026-05-22-fulcra-collect-menubar-design.md` maps to one or more tasks:
- Communication / Existing commands → Task 7.
- Communication / New commands → Tasks 1–4.
- Polling / Connecting → Tasks 7, 9, 11.
- Status item (four states) → Tasks 11, 12.
- Popover (header, rows, bootstrap) → Tasks 13, 14, 15.
- Preferences (Plugins/Notifications/About) → Tasks 16, 17.
- Bootstrap flow → Task 15.
- Notifications → Tasks 10, 18.
- Visual design / palette → Task 6 (constants), Tasks 13–17 (consumption).
- Components file layout → Tasks 5–17.
- Data flow → Tasks 7–18 (each step matches one numbered step in the spec).
- Error handling → Tasks 7 (`DaemonUnavailable`), 8 (model), 11 (icon), 18 (auth-denial silent).
- Testing → Tasks 1–10 (model layer) + Task 21 (manual smoke list).
- Deployment / py2app → Task 20.
- Required pre-work in sub-project 1 → Tasks 1–4.
- UX lock + Swift handoff → covered by the file boundaries chosen across Tasks 5–17.

**Placeholder scan:** no `TBD`, `TODO`, or "implement appropriate handling" in the plan body. Every step contains the code or command an engineer needs.

**Type consistency:** `StatusModel.update_from_status` signature is used identically in Task 8 (definition), Task 11 (`app._poll_once`), and the polling tests. `DaemonClient.run/credential_status/set_credential/delete_credential` signatures match the daemon handlers added in Tasks 2–4 and the JSON the menubar sends in Task 7.

**Scope:** one plan; one sub-project. Sub-project 3 (signing/notarization) and any Linux tray work are explicitly excluded both in the spec and at the deployment task.
