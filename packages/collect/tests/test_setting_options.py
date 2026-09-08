"""Synthetic list discovery and persistence contracts."""
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from fulcra_collect import config, credentials
from fulcra_collect.plugin import Credential, Plugin, Setting
from fulcra_collect.daemon import Daemon, Config
from fulcra_collect.registry import RegistryResult
from fulcra_collect.web import build_app, _ensure_token


def make_client(callback, *, required=True):
    plugin = Plugin(
        id="synthetic-lists", name="Synthetic lists", kind="scheduled",
        collect_mode="live_polled", run=lambda ctx: None,
        default_interval=timedelta(minutes=5),
        required_settings=(Setting("lists", "Lists", "multiselect", required=required),
                           Setting("region", "Region", "text")),
        required_credentials=(Credential("token", "Token", ""),
                              Credential("shared", "Shared", "", user_level=True)),
        setting_options=callback,
    )
    daemon = Daemon(registry=RegistryResult(plugins={plugin.id: plugin}), config=Config())
    client = TestClient(build_app(daemon))
    client.headers["Authorization"] = f"Bearer {_ensure_token()}"
    return client


pytestmark = pytest.mark.usefixtures("_in_memory_keyring")


OPTIONS = "/api/plugin/synthetic-lists/setting_options/lists"
SETTINGS = "/api/plugin/synthetic-lists/settings"


def test_multiselect_default_is_independent_list():
    a, b = Setting("a", "A", "multiselect"), Setting("b", "B", "multiselect")
    assert a.default == b.default == []
    assert a.default is not b.default


def test_options_use_saved_settings_and_scoped_credentials(collect_home, monkeypatch):
    monkeypatch.setattr(credentials, "get_secret", lambda pid, key: "plugin-synthetic" if (pid, key) == ("synthetic-lists", "token") else None)
    monkeypatch.setattr(credentials, "get_user_secret", lambda key: "user-synthetic" if key == "shared" else None)
    seen = []
    def discover(ctx, key):
        seen.append((ctx.config, ctx.credentials, key))
        return [{"value": "list-1", "label": "Synthetic list", "disabled": True}]
    client = make_client(discover)
    assert client.put(SETTINGS, json={"region": "sample"}).status_code == 200
    result = client.get(OPTIONS)
    assert result.status_code == 200
    assert result.json() == {"options": [{"value": "list-1", "label": "Synthetic list", "disabled": True}]}
    assert seen == [({"region": "sample"}, {"token": "plugin-synthetic", "shared": "user-synthetic"}, "lists")]
    assert "synthetic" not in result.text.replace("Synthetic", "")
    assert result.headers["cache-control"] == "no-store"
    assert client.get("/api/plugin/synthetic-lists/contract").json()["required_settings"][0]["default"] == []


def test_options_auth_and_unsupported_keys(collect_home):
    calls = []
    client = make_client(lambda ctx, key: calls.append(key) or [])
    client.headers.pop("Authorization")
    assert client.get(OPTIONS).status_code == 401
    assert calls == []
    client.headers["Authorization"] = f"Bearer {_ensure_token()}"
    for url in (OPTIONS.replace("synthetic-lists", "missing"), OPTIONS.replace("/lists", "/region"), OPTIONS.replace("/lists", "/missing")):
        assert client.get(url).status_code == 404
    assert calls == []


@pytest.mark.parametrize("options", [None, {}, ["x"], [{"value": "a", "label": "A"}, {"value": "a", "label": "B"}], [{"value": 1, "label": "A"}], [{"value": "", "label": "A"}], [{"value": "a", "label": 1}], [{"value": "a", "label": ""}], [{"value": "a", "label": "A", "disabled": "false"}], [{"value": "a", "label": "A", "secret": "do-not-return"}]])
def test_invalid_discovery_is_failure_not_empty(collect_home, options):
    client = make_client(lambda ctx, key: options)
    result = client.get(OPTIONS)
    assert result.status_code == 503
    assert "options" not in result.json()
    assert "do-not-return" not in result.text


def test_discovery_failure_is_sanitized_and_empty_is_success(collect_home):
    def fail(ctx, key):
        raise RuntimeError("private synthetic credential")
    result = make_client(fail).get(OPTIONS)
    assert result.status_code == 503
    assert "private synthetic credential" not in result.text
    assert make_client(lambda ctx, key: []).get(OPTIONS).json() == {"options": []}


@pytest.mark.parametrize("value", ["a", "[]", None, 3, True, {}, [1], [""], ["  "], ["a", "a"]])
def test_multiselect_rejects_malformed_saved_arrays(collect_home, value):
    client = make_client(lambda ctx, key: [])
    assert client.put(SETTINGS, json={"lists": value}).status_code == 400
    assert "lists" not in client.get(SETTINGS).json()


def test_saved_missing_ids_and_empty_can_persist_without_discovery(collect_home):
    def fail(ctx, key):
        pytest.fail("Saving must not call source discovery")
    client = make_client(fail)
    assert client.put(SETTINGS, json={"lists": ["missing-id"]}).status_code == 200
    assert client.get(SETTINGS).json()["lists"] == ["missing-id"]
    assert config.load().plugin_settings["synthetic-lists"]["lists"] == ["missing-id"]
    assert client.put(SETTINGS, json={"lists": []}).status_code == 200
    assert client.post("/api/plugin/synthetic-lists/enable").status_code == 400
    assert "synthetic-lists" not in config.load().enabled
