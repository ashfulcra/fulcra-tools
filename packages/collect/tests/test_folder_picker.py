from pathlib import Path

from fastapi.testclient import TestClient

from fulcra_collect import config as config_mod
from fulcra_collect.daemon import Config, Daemon
from fulcra_collect.plugin import Plugin, Setting, SetupStep
from fulcra_collect.registry import RegistryResult
from fulcra_collect.routes import plugins as plugin_routes
from fulcra_collect.web import _ensure_token, build_app


def _plugin() -> Plugin:
    return Plugin(
        id="synthetic-folder",
        name="Synthetic folder",
        kind="manual",
        collect_mode="historical",
        run=lambda ctx: None,
        requires_network=False,
        required_settings=(
            Setting(key="source_path", label="Folder", kind="path"),
            Setting(key="label", label="Label", kind="text", required=False),
        ),
        setup_steps=(
            SetupStep(kind="folder_picker", title="Choose", settings_keys=("source_path",)),
        ),
    )


def _client(plugin: Plugin, *, authenticated: bool = True) -> TestClient:
    daemon = Daemon(registry=RegistryResult(plugins={plugin.id: plugin}), config=Config())
    client = TestClient(build_app(daemon))
    if authenticated:
        client.headers["Authorization"] = f"Bearer {_ensure_token()}"
    return client


def test_folder_picker_requires_authentication(collect_home):
    response = _client(_plugin(), authenticated=False).post(
        "/api/plugin/synthetic-folder/choose-folder?key=source_path"
    )
    assert response.status_code == 401


def test_folder_picker_saves_declared_path_but_returns_only_display_name(
    collect_home, tmp_path: Path, monkeypatch
):
    selected = tmp_path / "Synthetic Project"
    selected.mkdir()
    monkeypatch.setattr(plugin_routes, "_choose_macos_folder", lambda: selected)

    response = _client(_plugin()).post(
        "/api/plugin/synthetic-folder/choose-folder?key=source_path"
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True, "name": "Synthetic Project"}
    saved = config_mod.load().plugin_settings["synthetic-folder"]["source_path"]
    assert saved == str(selected.resolve())
    assert str(selected) not in response.text


def test_folder_picker_rejects_unknown_or_non_path_settings(collect_home, tmp_path, monkeypatch):
    selected = tmp_path / "Synthetic"
    selected.mkdir()
    monkeypatch.setattr(plugin_routes, "_choose_macos_folder", lambda: selected)
    client = _client(_plugin())
    assert client.post(
        "/api/plugin/synthetic-folder/choose-folder?key=missing"
    ).status_code == 400
    assert client.post(
        "/api/plugin/synthetic-folder/choose-folder?key=label"
    ).status_code == 400


def test_native_folder_chooser_uses_fixed_script_and_refuses_root(tmp_path, monkeypatch):
    selected = tmp_path / "Synthetic"
    selected.mkdir()
    calls = []

    class Result:
        returncode = 0
        stdout = str(selected) + "\n"
        stderr = ""

    monkeypatch.setattr(plugin_routes.subprocess, "run", lambda argv, **kwargs: calls.append((argv, kwargs)) or Result())
    assert plugin_routes._choose_macos_folder() == selected.resolve()
    assert calls[0][0][0] == "osascript"
    assert calls[0][0][1] == "-e"
    assert len(calls[0][0]) == 3
    assert calls[0][1]["timeout"] == 120

    Result.stdout = "/\n"
    try:
        plugin_routes._choose_macos_folder()
    except ValueError as exc:
        assert "filesystem root" in str(exc)
    else:
        raise AssertionError("root selection should fail closed")
