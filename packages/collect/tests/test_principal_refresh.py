"""Silent refresh may renew Collect's principal, never adopt a CLI account."""
import subprocess

import pytest

from fulcra_collect import config, credentials
from collect_test_helpers import synthetic_account_token as token

ACCOUNT = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
OTHER = "aaaaaaaa-bbbb-4ccc-8ddd-ffffffffffff"




@pytest.fixture
def refresh(collect_home, _in_memory_keyring, monkeypatch):
    monkeypatch.setattr(credentials, "_refresh_failed", False)
    before = token(exp=1, marker="expired")
    credentials.set_user_secret("bearer-token", before)
    monkeypatch.setattr(credentials, "_find_fulcra_cli", lambda: "/synthetic/fulcra")
    calls = []
    def configure(candidate, during=lambda: None):
        def invoke(args, **kwargs):
            calls.append(args)
            during()
            return subprocess.CompletedProcess(args, 0, stdout=candidate, stderr="")
        monkeypatch.setattr(credentials.subprocess, "run", invoke)
    configure(token())
    return before, configure, calls


def test_silent_refresh_renews_only_the_captured_principal(refresh):
    before, configure, calls = refresh
    cfg = config.load()
    cfg.enable("synthetic")
    config.save(cfg)
    epochs = config.load().plugin_epochs
    assert credentials.refresh_fulcra_access_token(expected_token=before) == token()
    assert credentials.get_user_secret_fresh("bearer-token") == token()
    assert config.load().plugin_epochs == epochs


@pytest.mark.parametrize("candidate", [token(OTHER), token(None), token(exp=1), token(exp=True),
    token(exp=float("inf")), token(exp=None), "not-a-jwt"])
def test_silent_refresh_rejects_unknown_wrong_or_expired_cli_identity(refresh, candidate):
    before, configure, calls = refresh
    configure(candidate)
    assert credentials.refresh_fulcra_access_token() is None
    assert credentials.get_user_secret_fresh("bearer-token") == before
    assert credentials.is_refresh_failed()


@pytest.mark.parametrize("stored", [None, "opaque-unknown", token(None)])
def test_silent_refresh_cannot_establish_a_new_sign_in(refresh, stored):
    before, configure, calls = refresh
    if stored is None:
        credentials.delete_user_secret("bearer-token")
    else:
        credentials.set_user_secret("bearer-token", stored)
    assert credentials.refresh_fulcra_access_token() is None
    assert not calls
    assert credentials.get_user_secret_fresh("bearer-token") == stored


@pytest.mark.parametrize("other_account", [False, True])
def test_inflight_refresh_cannot_replace_a_new_interactive_sign_in(refresh, other_account):
    before, configure, calls = refresh
    interactive = token(OTHER if other_account else ACCOUNT, marker="interactive")
    def sign_in():
        with config.fulcra_account_transition():
            credentials.set_user_secret("bearer-token", interactive)
    configure(token(marker="old-refresh"), sign_in)
    returned = credentials.refresh_fulcra_access_token(expected_token=before)
    assert returned == (None if other_account else interactive)
    assert credentials.get_user_secret_fresh("bearer-token") == interactive


def test_refresh_refuses_a_durable_account_transition_gate(refresh):
    before, configure, calls = refresh
    config._persist_account_transition(config._config_path(), True)
    assert credentials.refresh_fulcra_access_token() is None
    assert not calls
    assert credentials.get_user_secret_fresh("bearer-token") == before


def test_refresh_cannot_retarget_an_old_request_after_account_switch(refresh):
    before, configure, calls = refresh
    selected = token(OTHER, marker="new-selected")
    credentials.set_user_secret("bearer-token", selected)
    assert credentials.refresh_fulcra_access_token(expected_token=before) is None
    assert not calls
    assert credentials.get_user_secret_fresh("bearer-token") == selected


def test_refresh_commit_can_run_inside_a_settings_commit(refresh):
    from fulcra_collect.config_leases import settings_update_scope
    before, configure, calls = refresh
    with settings_update_scope():
        assert credentials.refresh_fulcra_access_token(expected_token=before) == token()


def test_401_retry_remains_bound_to_its_original_request_account(refresh, monkeypatch):
    import httpx
    from fulcra_collect import web
    before, configure, calls = refresh
    selected = token(OTHER, marker="new-selected")
    credentials.set_user_secret("bearer-token", selected)
    configure(token(OTHER, marker="new-refresh"))
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(401)
    client_class = httpx.Client
    monkeypatch.setattr(web.httpx, "Client", lambda **kwargs: client_class(
        **kwargs, transport=httpx.MockTransport(handler)))
    with web._RetryingClient(before) as client:
        assert client.get("/synthetic-test").status_code == 401
    assert len(requests) == 1
    assert not calls
    assert credentials.get_user_secret_fresh("bearer-token") == selected
