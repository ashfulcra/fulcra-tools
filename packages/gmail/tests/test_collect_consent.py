"""Actual Collect caller captures destination and rejects in-flight revocation."""
import base64
from contextlib import nullcontext
import json
import logging
from types import SimpleNamespace

import httpx
import pytest

from fulcra_collect import config
from fulcra_collect.plugin import RunContext
from fulcra_collect.state import PluginState
from fulcra_gmail import collect_plugin

ACCOUNT = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
OTHER = "aaaaaaaa-bbbb-4ccc-8ddd-ffffffffffff"


def token(account=ACCOUNT):
    body = base64.urlsafe_b64encode(json.dumps({"fulcradynamics.com/userid": account, "exp": 4102444800}).encode()).decode().rstrip("=")
    return "synthetic." + body + ".signature"


def context(tmp_path, monkeypatch, **settings):
    monkeypatch.setenv("FULCRA_COLLECT_HOME", str(tmp_path))
    cfg = config.load()
    cfg.enable("gmail")
    cfg.update_plugin_settings("gmail", {"rules": [{"id": "synthetic", "version": 1, "name": "Synthetic",
        "match": "x", "actions": ["file"]}], **settings})
    config.save(cfg)
    cfg = config.load()
    ctx = RunContext(plugin_id="gmail", config=cfg.plugin_settings["gmail"], config_epoch=cfg.plugin_epochs["gmail"],
        credentials={}, state=PluginState("gmail"), log=logging.getLogger("synthetic"), _emit=lambda _: None,
        _plugin_kv_get=lambda k,d:d, _plugin_kv_set=lambda k,v:None)
    auth = [token()]
    monkeypatch.setattr(ctx, "collect_token", lambda: auth[0])
    return ctx, auth


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", lambda *_: pytest.fail("unexpected real network"))


@pytest.mark.parametrize("change", ["account", "settings", "disable", "source"])
def test_actual_poll_receives_fences_and_destination_scoped_state(tmp_path, monkeypatch, change):
    ctx, auth = context(tmp_path, monkeypatch)
    accounts = [SimpleNamespace(account_id="synthetic-source", email="synthetic@example.com", status="active", added_at="synthetic-generation")]
    registry = SimpleNamespace(list_accounts=lambda: accounts, delivery_scope=lambda *a: nullcontext())
    monkeypatch.setattr(collect_plugin, "_registry", lambda: registry)
    monkeypatch.setattr(collect_plugin, "GmailClient", lambda *a, **kw: SimpleNamespace(guard=kw["still_allowed"]))
    captured = []
    def poll(**kw):
        captured.append(kw)
        kw["still_allowed"]()
        if change == "account":
            auth[0] = token(OTHER)
        elif change == "source":
            accounts.clear()
        else:
            changed = config.load()
            if change == "disable":
                changed.enabled.remove("gmail")
            else:
                changed.plugin_settings["gmail"]["unrelated_setting"] = "synthetic-change"
            monkeypatch.setattr(config, "load", lambda: changed)
        kw["still_allowed"]()
        pytest.fail("revoked source continued")
    monkeypatch.setattr(collect_plugin, "poll_account_rule", poll)
    with pytest.raises(RuntimeError):
        collect_plugin.run(ctx)
    assert len(captured) == 1
    assert "gmail-delivery-v1" in str(captured[0]["cursors"].path)
    assert captured[0]["client"].guard is captured[0]["still_allowed"]


def test_preview_flag_stops_before_any_provider_access(tmp_path, monkeypatch):
    ctx, _ = context(tmp_path, monkeypatch, dry_run=True)
    monkeypatch.setattr(collect_plugin, "_registry", lambda: pytest.fail("source registry touched in unsupported preview"))
    with pytest.raises(RuntimeError, match="Preview"):
        collect_plugin.run(ctx)


@pytest.mark.parametrize("change", ["same-account-refresh", "different-account-after-post"])
def test_embedded_companion_http_uses_current_collect_token_and_latches_revocation(tmp_path, monkeypatch, change):
    from fulcra_collect.v1_session import CollectRecordSession
    from fulcra_gmail.collect_relay import CollectCoordTransport, _CollectSDK
    from coord_engine import cli
    ctx, auth = context(tmp_path, monkeypatch)
    requests = []
    def handler(request):
        requests.append(request)
        assert request.headers["Authorization"] == "Bearer " + auth[0]
        if change == "different-account-after-post":
            auth[0] = token(OTHER)
        return httpx.Response(200, json={"upload_id": "synthetic-upload"})
    with CollectRecordSession(ctx, transport=httpx.MockTransport(handler)) as session, CollectCoordTransport(session, "synthetic") as transport:
        sdk = _CollectSDK(transport.files)
        target = "Event/11111111-2222-4333-8444-555555555555"
        sdk.resolve_data_type = lambda *a, **kw: [{"id": target, "api_version": "v1", "recordable": True}]
        sdk.validate_records = lambda **kw: []
        transport._sdk = sdk
        if change == "same-account-refresh":
            auth[0] = token() + "-refreshed-synthetic-signature"
        def engine(argv, transport):
            ok = transport.record_write(target, "v1", "Synthetic", "com.fulcra.synthetic")
            if change == "same-account-refresh":
                assert ok
            else:
                assert not ok
                # A caught best-effort failure must not authorize another call.
                assert not transport.record_write(target, "v1", "Synthetic", "com.fulcra.synthetic")
            return 0
        monkeypatch.setattr(cli, "main", engine)
        rc, _ = transport.run(["tell", "synthetic", "recipient", "Synthetic"])
        assert (rc == 0) == (change == "same-account-refresh")
    assert len(requests) == 1
    assert requests[0].url.path == "/ingest/v1/record/Event/11111111-2222-4333-8444-555555555555"
    data = json.loads(requests[0].content)
    assert data["sources"] == ["com.fulcradynamics.cli", "com.fulcra.synthetic"]


def test_one_source_auth_failure_cannot_abort_other_healthy_account_rules(tmp_path, monkeypatch):
    from fulcra_gmail.pipeline import PollResult
    rules = [{'id': name, 'version': 1, 'name': name, 'match': 'synthetic', 'actions': ['file']}
             for name in ('first', 'second')]
    ctx, _ = context(tmp_path, monkeypatch, rules=rules)
    accounts = [SimpleNamespace(account_id=f'synthetic-{i}', email=f'synthetic-{i}@example.com',
        status='active', added_at='synthetic-generation') for i in range(2)]
    registry = SimpleNamespace(list_accounts=lambda:accounts, delivery_scope=lambda *a:nullcontext())
    monkeypatch.setattr(collect_plugin, '_registry', lambda:registry)
    monkeypatch.setattr(collect_plugin, 'GmailClient', lambda *a, **kw:object())
    calls = []
    def poll(**kw):
        calls.append((kw['account_id'], kw['rule'].id))
        if kw['account_id'] == 'synthetic-0':
            accounts[0].status = 'auth_failed'
            kw['still_allowed']()
        return PollResult(kw['account_id'], kw['rule'].id, 1, 0, 0, 0, False, None)
    monkeypatch.setattr(collect_plugin, 'poll_account_rule', poll)
    collect_plugin.run(ctx)
    assert calls == [('synthetic-0', 'first'), ('synthetic-1', 'first'), ('synthetic-1', 'second')]
