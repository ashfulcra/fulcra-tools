"""The Gmail fulcra-collect plugin — metadata contract + poll wiring."""
from __future__ import annotations

import logging
from datetime import timedelta


from fulcra_collect.plugin import RunContext
from fulcra_collect.state import PluginState

from fulcra_gmail import collect_plugin
from fulcra_gmail.collect_plugin import PLUGIN
from fulcra_gmail.relay import RelayResult


def _ctx(config: dict, emit=None) -> RunContext:
    return RunContext(
        plugin_id="gmail", config=config, credentials={},
        state=PluginState("gmail"), log=logging.getLogger("t"),
        _emit=emit or (lambda e: None),
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


class FakeAccount:
    def __init__(self, account_id, email, status="active"):
        self.account_id = account_id
        self.email = email
        self.status = status


def test_run_polls_each_account_and_skips_auth_failed(tmp_path, monkeypatch):
    events: list[dict] = []

    accounts = [
        FakeAccount("acct-ok", "a@example.com", "active"),
        FakeAccount("acct-bad", "b@example.com", "auth_failed"),
    ]

    class FakeRegistry:
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
    monkeypatch.setattr(collect_plugin, "build_files_writer", lambda token: object())
    monkeypatch.setattr(collect_plugin, "GmailClient", lambda *a, **k: object())
    monkeypatch.setattr(collect_plugin, "Ledger", lambda *a, **k: object())
    monkeypatch.setattr(collect_plugin, "CursorStore", lambda *a, **k: object())
    monkeypatch.setattr(collect_plugin, "CoordEngineRelayEmitter", lambda team: object())
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


def test_health_check_no_accounts(monkeypatch):
    class FakeRegistry:
        def list_accounts(self):
            return []

    monkeypatch.setattr(collect_plugin, "_registry", lambda transport=None: FakeRegistry())
    result = PLUGIN.health_check(_ctx({}))
    assert not result.ok
    assert "No Gmail accounts" in result.summary


def test_add_account_bridge_delegates(monkeypatch):
    calls = {}

    class FakeRegistry:
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

    class FakeRegistry:
        def list_accounts(self):
            return [FakeAccount("acct-ok", "a@example.com", "active")]

    def exploding_poll(**kw):
        raise RuntimeError("upload 401: Jwt is expired")

    monkeypatch.setattr(collect_plugin, "_registry", lambda transport=None: FakeRegistry())
    monkeypatch.setattr(collect_plugin, "build_files_writer", lambda token: object())
    monkeypatch.setattr(collect_plugin, "GmailClient", lambda *a, **k: object())
    monkeypatch.setattr(collect_plugin, "Ledger", lambda *a, **k: object())
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
    class FakeRegistry:
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
    monkeypatch.setattr(collect_plugin, "build_files_writer", lambda token: object())
    monkeypatch.setattr(collect_plugin, "GmailClient", lambda *a, **k: object())
    monkeypatch.setattr(collect_plugin, "Ledger", lambda *a, **k: object())
    monkeypatch.setattr(collect_plugin, "CursorStore", lambda *a, **k: object())
    monkeypatch.setattr(collect_plugin, "poll_account_rule", flaky_poll)

    config = {"rules": [
        {"id": "bad", "version": 1, "name": "B", "match": "x", "actions": ["file"]},
        {"id": "good", "version": 1, "name": "G", "match": "y", "actions": ["file"]},
    ]}
    PLUGIN.run(_ctx(config, emit=lambda e: None))  # must NOT raise
    assert calls == ["bad", "good"]
