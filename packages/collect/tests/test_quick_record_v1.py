"""Interactive authority is distinct from an enabled source plugin."""
import base64
import json

import httpx
import pytest

from fulcra_collect import config
from fulcra_collect.plugin import RunContext
from fulcra_collect.v1_session import CollectRecordSession
from fulcra_common.v1 import build_event_record
from datetime import datetime, timezone

ACCOUNT = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
OTHER = "aaaaaaaa-bbbb-4ccc-8ddd-ffffffffffff"
DESTINATION = "Event/11111111-2222-4333-8444-555555555555"


def token(account):
    claims = base64.urlsafe_b64encode(json.dumps({"fulcradynamics.com/userid": account}).encode()).decode().rstrip("=")
    return "synthetic." + claims + ".signature"


@pytest.fixture
def authority(collect_home, monkeypatch):
    current = [token(ACCOUNT)]
    monkeypatch.setattr(RunContext, "collect_token", lambda _: current[0])
    return current


def test_context_registers_one_stable_epoch_without_enabling_a_plugin(authority):
    from fulcra_collect.quick_record_v1 import quick_record_context
    ctx, account = quick_record_context()
    assert account == ACCOUNT and ctx.plugin_id == "quick-record"
    assert ctx.config_epoch and config.load().plugin_epochs["quick-record"] == ctx.config_epoch
    assert "quick-record" not in config.load().enabled
    again, _ = quick_record_context()
    assert again.config_epoch == ctx.config_epoch


def test_registered_context_can_be_read_without_exclusive_settings_lock(authority, monkeypatch):
    import fulcra_collect.quick_record_v1 as interactive
    first, account = interactive.quick_record_context()
    def no_exclusive_settings_wait():
        pytest.fail('ordinary quick-record reads must not wait for an exclusive settings lease')
    monkeypatch.setattr(interactive, 'settings_update_scope', no_exclusive_settings_wait)
    again, current = interactive.quick_record_context()
    assert again.config_epoch == first.config_epoch and current == account


@pytest.mark.parametrize("missing", [None, "invalid-token"])
def test_no_collect_identity_never_registers_interactive_authority(authority, missing):
    from fulcra_collect.quick_record_v1 import quick_record_context
    authority[0] = missing
    with pytest.raises(ValueError):
        quick_record_context()
    assert "quick-record" not in config.load().plugin_epochs


def test_interactive_session_writes_only_with_captured_account_and_epoch(authority):
    from fulcra_collect.quick_record_v1 import QuickRecordSession, quick_record_context
    ctx, account = quick_record_context()
    row = build_event_record(data_type=DESTINATION, source_id="synthetic.quick", start_time=datetime(2026, 1, 2, tzinfo=timezone.utc))
    calls = []
    def handler(request):
        calls.append(request.method)
        assert request.headers["authorization"] == "Bearer " + authority[0]
        return httpx.Response(200, json={"properties": {key: {} for key in row}}) if request.method == "GET" else httpx.Response(201, json={"upload_id": "synthetic"})
    with QuickRecordSession(ctx, expected_account=account, expected_epoch=ctx.config_epoch,
                            transport=httpx.MockTransport(handler)) as session:
        assert session.ingest(DESTINATION, [row])["upload_id"] == "synthetic"
    assert calls == ["GET", "POST"]
    with CollectRecordSession(ctx, require_enabled=False) as generic_setup:
        with pytest.raises(RuntimeError, match="changed"):
            generic_setup.ingest(DESTINATION, [row])


@pytest.mark.parametrize("change", ["account", "epoch", "settings", "gate", "away_and_back"])
def test_interactive_authority_keeps_all_account_and_consent_fences(authority, monkeypatch, change):
    from fulcra_collect.quick_record_v1 import QuickRecordSession, quick_record_context
    ctx, account = quick_record_context()
    with QuickRecordSession(ctx, expected_account=account, expected_epoch=ctx.config_epoch) as session:
        cfg = config.load()
        if change == "account":
            authority[0] = token(OTHER)
        elif change == "epoch":
            cfg.rotate_plugin_epoch("quick-record")
            config.save(cfg)
        elif change == "settings":
            cfg.plugin_settings["quick-record"] = {"manual": "changed"}
            monkeypatch.setattr(config, "load", lambda: cfg)
        elif change == "gate":
            config._persist_account_transition(config._config_path(), True)
        else:
            with config.fulcra_account_transition():
                pass  # Even the same account's new interactive sign-in revokes the old epoch.
        with pytest.raises(RuntimeError, match="changed"):
            session.ingest(DESTINATION, [])


@pytest.mark.parametrize("field,value", [("expected_account", OTHER), ("expected_account", "bad"),
                                        ("expected_epoch", "changed"), ("expected_epoch", "")])
def test_stale_or_malformed_action_context_cannot_become_authority(authority, field, value):
    from fulcra_collect.quick_record_v1 import QuickRecordSession, quick_record_context
    ctx, account = quick_record_context()
    args = {"expected_account": account, "expected_epoch": ctx.config_epoch}
    args[field] = value
    with pytest.raises((ValueError, RuntimeError)):
        QuickRecordSession(ctx, **args)


def test_interactive_authority_cannot_be_used_for_an_arbitrary_source(authority):
    from fulcra_collect.quick_record_v1 import QuickRecordSession, quick_record_context
    ctx, account = quick_record_context()
    ctx.plugin_id = "a-scheduled-source"
    with pytest.raises(ValueError, match="quick-record"):
        QuickRecordSession(ctx, expected_account=account, expected_epoch=ctx.config_epoch)
