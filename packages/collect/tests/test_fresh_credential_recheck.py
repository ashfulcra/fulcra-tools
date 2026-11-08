"""Transient quiet reads recheck without grants, caches or write effects."""
import pytest

from fulcra_collect import credentials


def test_pending_read_rechecks_fresh_without_requesting_access(monkeypatch):
    replies = iter([TimeoutError('synthetic pending'), 'first', 'second'])
    calls = []
    def read(service, key):
        calls.append((service, key))
        value = next(replies)
        if isinstance(value, Exception):
            raise value
        return value
    monkeypatch.setattr(credentials, '_keyring_get', read)
    monkeypatch.setattr(credentials, 'authorize_user_secret', lambda *_: pytest.fail('grant requested'))
    monkeypatch.setattr(credentials, 'set_user_secret', lambda *_: pytest.fail('credential write'))
    assert credentials.get_user_secret_fresh('bearer-token') == 'first'
    assert credentials.get_user_secret_fresh('bearer-token') == 'second'
    assert calls == [('fulcra-collect:user', 'bearer-token')] * 3


def test_persistent_pending_stays_unknown_after_bounded_rechecks(monkeypatch):
    first = TimeoutError('synthetic pending')
    calls = []
    def read(*_):
        calls.append(1)
        raise first
    monkeypatch.setattr(credentials, '_keyring_get', read)
    with pytest.raises(TimeoutError) as error:
        credentials.get_user_secret_fresh('bearer-token')
    assert error.value is first
    assert len(calls) == 3


def test_rechecks_reserve_the_whole_native_wait_inside_total_budget(monkeypatch):
    clock, calls = [0.0], []
    monkeypatch.setattr(credentials.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(credentials.time, 'sleep', lambda delay: clock.__setitem__(0, clock[0] + delay))
    def read(*_):
        calls.append(1)
        clock[0] += credentials._KEYCHAIN_READ_TIMEOUT_S
        raise TimeoutError('synthetic pending')
    monkeypatch.setattr(credentials, '_keyring_get', read)
    with pytest.raises(TimeoutError):
        credentials.get_user_secret_fresh('bearer-token')
    assert len(calls) == 2
    assert clock[0] < 15


def test_recheck_refuses_revision_change_before_another_native_read(monkeypatch):
    revision, calls = [0], []
    monkeypatch.setattr(credentials, 'credential_revision', lambda: revision[0])
    def read(*_):
        calls.append(1)
        revision[0] += 1
        raise TimeoutError('synthetic pending')
    monkeypatch.setattr(credentials, '_keyring_get', read)
    with pytest.raises(credentials.CredentialStateChanged):
        credentials.get_user_secret_fresh('bearer-token')
    assert calls == [1]


@pytest.mark.parametrize('failure', [credentials.KeychainAccessRequired('synthetic denial'),
                                   credentials.CredentialStateChanged('synthetic changed')])
def test_denied_or_changed_access_never_starts_pending_retry(monkeypatch, failure):
    calls = []
    def read(*_):
        calls.append(1)
        raise failure
    monkeypatch.setattr(credentials, '_keyring_get', read)
    with pytest.raises(type(failure)):
        credentials.get_user_secret_fresh('bearer-token')
    assert calls == [1]
