"""Credential storage regressions with no native or saved-auth access."""
import json

import pytest


@pytest.fixture
def backend(monkeypatch, tmp_path):
    monkeypatch.setenv('FULCRA_COLLECT_HOME', str(tmp_path / 'profile'))
    from fulcra_collect import credentials, trakt_source
    values = {key: None for key in trakt_source._FIELDS}
    values.update(client_id='synthetic-client', access_token='synthetic-access',
                  refresh_token='synthetic-refresh')
    items, reads, writes = {}, [], []
    def read(service, key, **kwargs):
        assert service == 'fulcra-collect:trakt'
        reads.append(key)
        return items.get(key)
    def write(key, value):
        writes.append((key, value))
        items[key] = value
    monkeypatch.setattr(credentials, '_keyring_get', read)
    monkeypatch.setattr(trakt_source, '_os_user_root', lambda: tmp_path / 'owner')
    return trakt_source, values, items, reads, writes, write


def test_current_bundle_never_reads_or_prompts_for_legacy_fields(backend):
    source, values, items, reads, writes, write = backend
    items['credential-bundle-v1'] = json.dumps({'version': 1, 'values': values,
                                               'unknown_fields': []})
    assert source.KeychainStore(_quiet_write=write).snapshot() == values
    assert reads == ['credential-bundle-v1']
    assert writes == []


def test_refresh_publication_is_one_atomic_credential_write(backend):
    source, values, items, reads, writes, write = backend
    items['credential-bundle-v1'] = json.dumps({'version': 1, 'values': values,
                                               'unknown_fields': []})
    updated = {**values, 'access_token': 'next-access', 'refresh_token': 'next-refresh',
               'created_at': '1', 'expires_in': '100'}
    store = source.KeychainStore(_quiet_write=write)
    store.publish(updated)
    assert [key for key, _ in writes] == ['credential-bundle-v1']
    assert store.snapshot() == updated


def test_exact_existing_bundle_reconciles_without_another_secret_write(backend):
    source, values, items, reads, writes, write = backend
    items['credential-bundle-v1'] = json.dumps({'version': 1, 'values': values,
                                               'unknown_fields': []})
    source.KeychainStore(_quiet_write=write).publish(values)
    assert writes == []


def test_corrupt_bundle_never_falls_back_to_old_credentials(backend):
    source, values, items, reads, writes, write = backend
    items.update(values)
    items['credential-bundle-v1'] = '{broken'
    with pytest.raises(source.SourceHold):
        source.KeychainStore(_quiet_write=write).snapshot()
    assert reads == ['credential-bundle-v1']
    assert writes == []


def test_legacy_reads_are_readonly_and_first_edit_preserves_original_items(backend):
    source, values, items, reads, writes, write = backend
    items.update(values)
    original = dict(items)
    store = source.KeychainStore(_quiet_write=write)
    assert store.snapshot() == values
    assert writes == []
    store.set('client_secret', 'synthetic-secret')
    assert [key for key, _ in writes] == ['credential-bundle-v1']
    assert {key: items[key] for key in original} == original
    reads.clear()
    assert store.snapshot() == {**values, 'client_secret': 'synthetic-secret'}
    assert reads == ['credential-bundle-v1']


def test_field_authorization_targets_one_bundle_not_six_items(backend, monkeypatch):
    source, values, items, reads, writes, write = backend
    from fulcra_collect import credentials
    authorizations = []
    monkeypatch.setattr(credentials, '_authorize_item',
                        lambda service, key: authorizations.append((service, key)) or True)
    monkeypatch.setattr(credentials, 'authorize_secret',
                        lambda plugin, key: credentials._authorize_item('fulcra-collect:' + plugin, key))
    store = source.KeychainStore(_quiet_write=write)
    for key in source._FIELDS:
        assert store.authorize(key)
    assert set(authorizations) == {('fulcra-collect:trakt', 'credential-bundle-v1')}


def test_unknown_legacy_metadata_is_retained_until_real_refresh(backend):
    source, values, items, reads, writes, write = backend
    items['credential-bundle-v1'] = json.dumps({'version': 1, 'values': values,
        'unknown_fields': ['created_at', 'expires_in']})
    store = source.KeychainStore(_quiet_write=write)
    store.set('client_secret', 'synthetic-secret')
    assert json.loads(items['credential-bundle-v1'])['unknown_fields'] == ['created_at', 'expires_in']
    store.publish({**values, 'created_at': '1', 'expires_in': '100'})
    assert json.loads(items['credential-bundle-v1'])['unknown_fields'] == []


def test_explicit_reconnect_preserves_inaccessible_legacy_items_without_prompting(backend, monkeypatch, tmp_path):
    source, values, items, reads, writes, write = backend
    from fulcra_collect import credentials
    monkeypatch.setenv('FULCRA_COLLECT_HOME', str(tmp_path / 'profile'))
    items.update(values)
    original = dict(items)
    def read(service, key, **kwargs):
        reads.append(key)
        if key in source._FIELDS and key != 'client_id':
            raise credentials.KeychainAccessRequired('synthetic legacy denial')
        return items.get(key)
    monkeypatch.setattr(credentials, '_keyring_get', read)
    monkeypatch.setattr(credentials, '_authorize_item',
                        lambda *a: pytest.fail('reconnect must not request legacy approvals'))
    owner = source.Owner(source.KeychainStore(_quiet_write=write))
    with pytest.raises(credentials.CredentialReadError):
        owner.store.snapshot()
    assert writes == []
    authorization = owner.begin_authorization('synthetic-state', 'synthetic-verifier',
                                             'http://127.0.0.1/callback')
    assert authorization.values['client_id'] == values['client_id']
    assert authorization.values['access_token'] is None
    assert {key: items[key] for key in original} == original
    assert [key for key, _ in writes] == ['credential-bundle-v1']
