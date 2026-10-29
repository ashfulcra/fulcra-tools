"""An obsolete blocking-login request grants no account-change authority."""
import subprocess

from fulcra_collect import config, credentials
from test_fulcra_auth_url import _build_daemon, _client


def test_classic_login_refuses_before_cli_or_existing_token_adoption(
        collect_home, _in_memory_keyring, monkeypatch):
    credentials.set_user_secret('bearer-token', 'synthetic-current-collect-token')
    cfg = config.load()
    cfg.enable('synthetic-source')
    config.save(cfg)
    before = dict(config.load().plugin_epochs)
    client = _client(_build_daemon(collect_home))
    def forbidden(*args, **kwargs):
        raise AssertionError('obsolete reconnect must not invoke a CLI or mutate credentials')
    monkeypatch.setattr(credentials, '_find_fulcra_cli', forbidden)
    monkeypatch.setattr(subprocess, 'run', forbidden)
    monkeypatch.setattr(credentials, 'set_user_secret', forbidden)
    result = client.post('/api/fulcra/auth/cli_login')
    assert result.status_code == 409
    assert 'browser' in result.json()['detail'].lower()
    assert credentials.get_user_secret('bearer-token') == 'synthetic-current-collect-token'
    assert config.load().plugin_epochs == before and not config.load().account_transition
