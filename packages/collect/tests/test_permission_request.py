"""Native permission prompts require a separate explicit request endpoint."""
from dataclasses import replace

import pytest

from fulcra_collect import credentials
from fulcra_collect.plugin import Credential, Plugin, Setting
from fulcra_collect.daemon import Daemon, Config
from fulcra_collect.registry import RegistryResult
from fulcra_collect.web import build_app, _ensure_token
from fastapi.testclient import TestClient

pytestmark = pytest.mark.usefixtures("_in_memory_keyring")


def make_client(check=None, request=None):
    plugin = Plugin(
        id="synthetic-permission", name="Synthetic permission", kind="manual",
        collect_mode="historical", run=lambda ctx: None,
        required_settings=(Setting("mode", "Mode", "text"),),
        required_credentials=(Credential("local", "Local", ""),
                              Credential("shared", "Shared", "", user_level=True)),
        permission_check=check,
    )
    plugin = replace(plugin, permission_request=request)
    daemon = Daemon(registry=RegistryResult(plugins={plugin.id: plugin}), config=Config())
    client = TestClient(build_app(daemon))
    client.headers["Authorization"] = f"Bearer {_ensure_token()}"
    return client


BASE = "/api/plugin/synthetic-permission"


def test_permission_check_and_contract_never_request_native_access(collect_home):
    calls = []
    client = make_client(lambda ctx: calls.append("check") or {"granted": False},
                         lambda ctx: calls.append("request") or {"granted": True})
    assert client.get(BASE + "/contract").json()["permission_request_available"] is True
    assert client.post(BASE + "/check_permission").json()["granted"] is False
    assert calls == ["check"]
    assert client.post(BASE + "/request_permission").json() == {"granted": True, "hint": None}
    assert calls == ["check", "request"]


def test_request_permission_auth_and_missing_callback(collect_home):
    calls = []
    client = make_client(request=lambda ctx: calls.append("request") or {"granted": True})
    client.headers.pop("Authorization")
    assert client.post(BASE + "/request_permission").status_code == 401
    assert calls == []
    client = make_client()
    assert client.get(BASE + "/contract").json()["permission_request_available"] is False
    assert client.post(BASE + "/request_permission").status_code == 404
    assert client.post("/api/plugin/missing/request_permission").status_code == 404


def test_request_context_has_current_settings_and_correct_secret_scopes(collect_home):
    credentials.set_secret("synthetic-permission", "local", "synthetic-local")
    credentials.set_user_secret("shared", "synthetic-user")
    credentials.set_secret("synthetic-permission", "shared", "wrong-scope")
    seen = []
    def probe(ctx):
        seen.append((ctx.plugin_id, ctx.config, ctx.credentials))
        return {"granted": False, "hint": "Use settings"}
    client = make_client(probe, probe)
    assert client.put(BASE + "/settings", json={"mode": "sample"}).status_code == 200
    for endpoint in ("check_permission", "request_permission"):
        result = client.post(BASE + "/" + endpoint)
        assert result.json() == {"granted": False, "hint": "Use settings"}
        assert "synthetic-local" not in result.text
    assert seen == [("synthetic-permission", {"mode": "sample"}, {"local": "synthetic-local", "shared": "synthetic-user"})] * 2


@pytest.mark.parametrize("result", [None, {}, {"granted": "true"}, {"granted": True, "hint": {"secret": "synthetic"}}])
def test_invalid_request_response_never_grants_access(collect_home, result):
    client = make_client(request=lambda ctx: result)
    response = client.post(BASE + "/request_permission").json()
    assert response["granted"] is False
    assert "synthetic" not in response["hint"]


def test_request_failure_does_not_expose_exception_or_secret(collect_home, caplog):
    def request(ctx):
        raise RuntimeError("synthetic-secret-must-stay-private")
    client = make_client(request=request)
    response = client.post(BASE + "/request_permission")
    assert response.json()["granted"] is False
    assert "synthetic-secret-must-stay-private" not in response.text
    assert "synthetic-secret-must-stay-private" not in caplog.text
