"""The Gmail fulcra-collect plugin — metadata contract + poll wiring."""
from __future__ import annotations

import logging
import base64
import json
from contextlib import nullcontext
from datetime import timedelta
from types import SimpleNamespace

import pytest


from fulcra_collect.plugin import RunContext
from fulcra_collect.state import PluginState

from fulcra_gmail import collect_plugin
from fulcra_gmail.collect_plugin import PLUGIN
from fulcra_gmail.relay import RelayResult


@pytest.fixture(autouse=True)
def collect_sign_in(tmp_path, monkeypatch):
    from fulcra_collect import credentials
    monkeypatch.setenv("FULCRA_COLLECT_HOME", str(tmp_path))
    claims = base64.urlsafe_b64encode(json.dumps({"fulcradynamics.com/userid": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee", "exp": 4102444800}).encode()).decode().rstrip("=")
    monkeypatch.setattr(credentials, "get_user_secret_fresh", lambda _: "synthetic." + claims + ".signature")


def test_gmail_run_requires_collect_sign_in_before_provider_or_cli_fallback(monkeypatch):
    from fulcra_collect import credentials
    monkeypatch.setattr(credentials, "get_user_secret_fresh", lambda _: None)
    account = SimpleNamespace(account_id="synthetic", email="synthetic@example.com", status="active")
    monkeypatch.setattr(collect_plugin, "_registry", lambda: SimpleNamespace(list_accounts=lambda: [account]))
    monkeypatch.setattr(collect_plugin, "FilesWriter", lambda *_: pytest.fail("writer cannot use default CLI auth"))
    monkeypatch.setattr(collect_plugin, "GmailClient", lambda *_: pytest.fail("source read before Collect sign-in"))
    with pytest.raises(ValueError, match="Reconnect to Fulcra in Collect"):
        PLUGIN.run(_ctx({"rules": [{"id": "synthetic", "version": 1, "name": "Synthetic",
            "match": "subject:synthetic", "actions": ["file"]}]}))


def _ctx(config: dict, emit=None, kv: dict | None = None) -> RunContext:
    from fulcra_collect import config as cfg_mod
    cfg = cfg_mod.load()
    cfg.enable("gmail")
    cfg.update_plugin_settings("gmail", config)
    cfg_mod.save(cfg)
    cfg = cfg_mod.load()
    kv_store = {} if kv is None else kv
    return RunContext(
        plugin_id="gmail", config=config, config_epoch=cfg.plugin_epochs["gmail"], credentials={},
        state=PluginState("gmail"), log=logging.getLogger("t"),
        _emit=emit or (lambda e: None),
        _plugin_kv_get=lambda key, default: kv_store.get(key, default),
        _plugin_kv_set=lambda key, value: kv_store.__setitem__(key, value),
    )


# --- metadata contract -----------------------------------------------------


def test_plugin_metadata_is_scheduled_15min():
    assert PLUGIN.id == "gmail"
    assert PLUGIN.kind == "scheduled"
    assert PLUGIN.collect_mode == "live_polled"
    assert PLUGIN.default_interval == timedelta(minutes=15)
    assert PLUGIN.health_check is not None


def test_plugin_declares_shared_client_credentials():
    keys = {c.key for c in PLUGIN.required_credentials}
    # Keys MUST match what AccountRegistry reads from the keychain.
    from fulcra_gmail.accounts import _CLIENT_ID_KEY, _CLIENT_SECRET_KEY
    assert keys == {_CLIENT_ID_KEY, _CLIENT_SECRET_KEY}


def test_setup_steps_cover_the_flow_and_serialize():
    kinds = [s.kind for s in PLUGIN.setup_steps]
    assert kinds[0] == "intro"
    assert "external_action" in kinds  # cloud-console + add-account
    assert "test_connection" in kinds
    assert kinds[-1] == "done"
    # The exact redirect URI appears in the console click-path step.
    console = next(s for s in PLUGIN.setup_steps
                   if "redirect" in s.body_md.lower())
    assert "http://127.0.0.1:9292/api/oauth/callback" in console.body_md
    assert "gmail.readonly" in console.body_md
    # Every step is a frozen dataclass with the required title.
    for step in PLUGIN.setup_steps:
        assert step.title


def test_run_no_rules_is_noop():
    # No rules → returns cleanly without touching the registry.
    PLUGIN.run(_ctx({}))


# --- poll wiring -----------------------------------------------------------


class RegistryStub:
    def delivery_scope(self, *args):
        return nullcontext()


class FakeAccount:
    def __init__(self, account_id, email, status="active"):
        self.account_id = account_id
        self.email = email
        self.status = status


class FakeLedger:
    def compact_if_needed(self):
        return False


def test_run_polls_each_account_and_skips_auth_failed(tmp_path, monkeypatch):
    events: list[dict] = []

    accounts = [
        FakeAccount("acct-ok", "a@example.com", "active"),
        FakeAccount("acct-bad", "b@example.com", "auth_failed"),
    ]

    class FakeRegistry(RegistryStub):
        def list_accounts(self):
            return accounts

    polled: list[str] = []

    def fake_poll(**kw):
        polled.append(kw["account_id"])
        from fulcra_gmail.pipeline import PollResult
        return PollResult(account_id=kw["account_id"], rule_id=kw["rule"].id,
                          rule_version=kw["rule"].version, candidates=1,
                          effective=1, processed=1, blocked=False, cursor=123)

    monkeypatch.setattr(collect_plugin, "_registry", lambda transport=None: FakeRegistry())
    monkeypatch.setattr(collect_plugin, "FilesWriter", lambda token: object())
    monkeypatch.setattr(collect_plugin, "GmailClient", lambda *a, **k: object())
    monkeypatch.setattr(collect_plugin, "Ledger", lambda *a, **k: FakeLedger())
    monkeypatch.setattr(collect_plugin, "CursorStore", lambda *a, **k: object())
    monkeypatch.setattr(collect_plugin, "CoordEngineRelayEmitter", lambda team, **kw: object())
    monkeypatch.setattr(collect_plugin, "poll_account_rule", fake_poll)

    config = {
        "relay_team": "team-x",
        "rules": [{
            "id": "r1", "version": 1, "name": "R1", "match": "subject:x",
            "actions": ["file", "relay"], "relay_to": "agent:claude",
        }],
    }
    PLUGIN.run(_ctx(config, emit=events.append))

    # Only the active account was polled; the auth_failed one was skipped.
    assert polled == ["acct-ok"]
    statuses = [e for e in events if e.get("status") == "auth_failed"]
    assert statuses and statuses[0]["account"] == "acct-bad"


def test_run_shares_one_effect_budget_across_all_accounts_and_rules(monkeypatch):
    accounts = [
        FakeAccount("acct-1", "a@example.com"),
        FakeAccount("acct-2", "b@example.com"),
    ]

    class FakeRegistry(RegistryStub):
        def list_accounts(self):
            return accounts

    calls: list[tuple[str, str, int]] = []

    def fake_poll(**kw):
        budget = kw["effect_budget"]
        used = 0
        while used < 3 and budget.consume():
            used += 1
        calls.append((kw["account_id"], kw["rule"].id, used))
        from fulcra_gmail.pipeline import PollResult
        return PollResult(
            account_id=kw["account_id"], rule_id=kw["rule"].id,
            rule_version=kw["rule"].version, candidates=used,
            effective=used, processed=used, blocked=budget.exhausted,
            cursor=1,
        )

    monkeypatch.setattr(collect_plugin, "_registry", lambda transport=None: FakeRegistry())
    monkeypatch.setattr(collect_plugin, "FilesWriter", lambda token: object())
    monkeypatch.setattr(collect_plugin, "GmailClient", lambda *a, **k: object())
    monkeypatch.setattr(collect_plugin, "Ledger", lambda *a, **k: FakeLedger())
    monkeypatch.setattr(collect_plugin, "CursorStore", lambda *a, **k: object())
    monkeypatch.setattr(collect_plugin, "poll_account_rule", fake_poll)

    config = {"rules": [
        {"id": "r1", "version": 1, "name": "R1", "match": "x", "actions": ["file"]},
        {"id": "r2", "version": 1, "name": "R2", "match": "y", "actions": ["file"]},
    ]}
    PLUGIN.run(_ctx(config))

    assert calls == [("acct-1", "r1", 3), ("acct-1", "r2", 2)]


def test_run_rotates_first_pair_so_a_permanent_backlog_cannot_starve_accounts(
    monkeypatch,
):
    accounts = [
        FakeAccount("acct-1", "a@example.com"),
        FakeAccount("acct-2", "b@example.com"),
    ]

    class FakeRegistry(RegistryStub):
        def list_accounts(self):
            return accounts

    calls: list[str] = []

    def fake_poll(**kw):
        budget = kw["effect_budget"]
        used = 0
        while budget.consume():
            used += 1
        calls.append(kw["account_id"])
        from fulcra_gmail.pipeline import PollResult
        return PollResult(
            account_id=kw["account_id"], rule_id=kw["rule"].id,
            rule_version=kw["rule"].version, candidates=used,
            effective=used, processed=0, blocked=True, cursor=None,
            budget_exhausted=True,
        )

    monkeypatch.setattr(collect_plugin, "_registry", lambda transport=None: FakeRegistry())
    monkeypatch.setattr(collect_plugin, "FilesWriter", lambda token: object())
    monkeypatch.setattr(collect_plugin, "GmailClient", lambda *a, **k: object())
    monkeypatch.setattr(collect_plugin, "Ledger", lambda *a, **k: FakeLedger())
    monkeypatch.setattr(collect_plugin, "CursorStore", lambda *a, **k: object())
    monkeypatch.setattr(collect_plugin, "poll_account_rule", fake_poll)

    config = {"rules": [{
        "id": "r1", "version": 1, "name": "R1", "match": "x",
        "actions": ["file"],
    }]}
    kv: dict = {}

    PLUGIN.run(_ctx(config, kv=kv))
    PLUGIN.run(_ctx(config, kv=kv))

    assert calls == ["acct-1", "acct-2"]


def test_health_check_no_accounts(monkeypatch):
    class FakeRegistry(RegistryStub):
        def list_accounts(self):
            return []

    monkeypatch.setattr(collect_plugin, "_registry", lambda transport=None: FakeRegistry())
    result = PLUGIN.health_check(_ctx({}))
    assert not result.ok
    assert "No Gmail accounts" in result.summary


def test_add_account_bridge_delegates(monkeypatch):
    calls = {}

    class FakeRegistry(RegistryStub):
        def begin_add_account(self, redirect_uri):
            calls["begin"] = redirect_uri
            return "session"

        def complete_add_account(self, state, code):
            calls["complete"] = (state, code)
            return RelayResult(ok=True)  # any object

    monkeypatch.setattr(collect_plugin, "_registry", lambda transport=None: FakeRegistry())
    assert collect_plugin.begin_add_account() == "session"
    assert calls["begin"] == collect_plugin.REDIRECT_URI
    collect_plugin.complete_add_account("nonce", "code")
    assert calls["complete"] == ("nonce", "code")


def test_run_raises_when_all_rule_polls_fail(monkeypatch):
    """Fail-soft is per-rule; when EVERY poll fails the run must FAIL, not
    report 'no new data' (2026-07-16: expired Fulcra token 401'd every upload
    and 21 matched emails dropped silently behind a green run)."""
    import pytest

    class FakeRegistry(RegistryStub):
        def list_accounts(self):
            return [FakeAccount("acct-ok", "a@example.com", "active")]

    def exploding_poll(**kw):
        raise RuntimeError("upload 401: Jwt is expired")

    monkeypatch.setattr(collect_plugin, "_registry", lambda transport=None: FakeRegistry())
    monkeypatch.setattr(collect_plugin, "FilesWriter", lambda token: object())
    monkeypatch.setattr(collect_plugin, "GmailClient", lambda *a, **k: object())
    monkeypatch.setattr(collect_plugin, "Ledger", lambda *a, **k: FakeLedger())
    monkeypatch.setattr(collect_plugin, "CursorStore", lambda *a, **k: object())
    monkeypatch.setattr(collect_plugin, "poll_account_rule", exploding_poll)

    config = {"rules": [{
        "id": "r1", "version": 1, "name": "R1", "match": "subject:x",
        "actions": ["file"],
    }]}
    with pytest.raises(RuntimeError, match=r"all 1/1 rule poll\(s\) failed"):
        PLUGIN.run(_ctx(config, emit=lambda e: None))


def test_run_partial_poll_failure_stays_soft(monkeypatch):
    """One rule failing while another succeeds keeps the fail-soft behavior."""
    class FakeRegistry(RegistryStub):
        def list_accounts(self):
            return [FakeAccount("acct-ok", "a@example.com", "active")]

    calls = []

    def flaky_poll(**kw):
        calls.append(kw["rule"].id)
        if kw["rule"].id == "bad":
            raise RuntimeError("boom")
        from fulcra_gmail.pipeline import PollResult
        return PollResult(account_id=kw["account_id"], rule_id=kw["rule"].id,
                          rule_version=kw["rule"].version, candidates=1,
                          effective=1, processed=1, blocked=False, cursor=1)

    monkeypatch.setattr(collect_plugin, "_registry", lambda transport=None: FakeRegistry())
    monkeypatch.setattr(collect_plugin, "FilesWriter", lambda token: object())
    monkeypatch.setattr(collect_plugin, "GmailClient", lambda *a, **k: object())
    monkeypatch.setattr(collect_plugin, "Ledger", lambda *a, **k: FakeLedger())
    monkeypatch.setattr(collect_plugin, "CursorStore", lambda *a, **k: object())
    monkeypatch.setattr(collect_plugin, "poll_account_rule", flaky_poll)

    config = {"rules": [
        {"id": "bad", "version": 1, "name": "B", "match": "x", "actions": ["file"]},
        {"id": "good", "version": 1, "name": "G", "match": "y", "actions": ["file"]},
    ]}
    PLUGIN.run(_ctx(config, emit=lambda e: None))  # must NOT raise
    assert calls == ["bad", "good"]
