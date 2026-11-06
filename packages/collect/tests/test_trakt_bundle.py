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
    items[source.KeychainStore.bundle_key] = json.dumps({'version': 1, 'values': values, 'unknown_fields': []})
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


@pytest.mark.parametrize('legacy,availability', [(False, 'access_required'), (False, 'pending'),
                                                (True, 'access_required'), (True, 'pending')])
def test_physical_read_origin_survives_hydration_status_and_recovery(backend, monkeypatch, legacy, availability):
    source, values, items, reads, writes, write = backend
    from fulcra_collect import credentials
    from fulcra_collect.config import Config
    from fulcra_collect.daemon import Daemon
    from fulcra_collect.plugin import Plugin, Credential
    from fulcra_collect.registry import RegistryResult
    key = 'created_at' if legacy else source.KeychainStore.bundle_key
    def read(service, field, **kwargs):
        reads.append(field)
        if field == key:
            if availability == 'pending':
                raise TimeoutError('synthetic')
            raise credentials.KeychainAccessRequired('synthetic')
        return items.get(field)
    monkeypatch.setattr(credentials, '_keyring_get', read)
    plugin = Plugin(id='trakt', name='Trakt', kind='manual', collect_mode='historical', run=lambda _: None,
                    required_credentials=[Credential(key=k, label=k, help='Synthetic credential') for k in ['client_id', 'access_token']])
    registry = RegistryResult()
    registry.plugins['trakt'] = plugin
    daemon = Daemon(registry=registry, config=Config())
    with pytest.raises(credentials.CredentialReadError) as caught:
        credentials.get_plugin_credentials(plugin)
    detail = caught.value.detail
    assert detail == {'stage': 'trakt_legacy_migration' if legacy else 'credential_hydration',
                      'scope': 'plugin', 'key': key, 'availability': availability}
    recovery = daemon._validated_credential_error('trakt', detail)
    assert recovery and recovery['recovery'] == 'configure'
    reads.clear()
    status = daemon._credential_status('trakt')
    assert status['credential_error'] == recovery
    assert reads.count(source.KeychainStore.bundle_key) == 1
    assert all(value == availability for value in status['credentials'].values())
    assert status['credential_recovery'] == (
        'trakt_reconnect' if legacy and availability == 'access_required'
        else 'bundle_authorize' if not legacy and availability == 'access_required' else 'quiet')
    assert writes == []


def test_hydration_reads_one_current_bundle_per_operation(backend):
    source, values, items, reads, writes, write = backend
    from fulcra_collect import credentials
    from fulcra_collect.plugin import Plugin, Credential
    items[source.KeychainStore.bundle_key] = json.dumps({'version': 1, 'values': values, 'unknown_fields': []})
    plugin = Plugin(id='trakt', name='Trakt', kind='manual', collect_mode='historical', run=lambda _: None,
                    required_credentials=[Credential(key=k, label=k, help='Synthetic credential') for k in ['client_id', 'access_token']])
    assert credentials.get_plugin_credentials(plugin) == {k: values[k] for k in ['client_id', 'access_token']}
    assert reads == [source.KeychainStore.bundle_key]


def test_stale_authorize_never_requests_grants_for_absent_bundle(backend, monkeypatch):
    source, values, items, reads, writes, write = backend
    from fulcra_collect import credentials
    monkeypatch.setattr(credentials, '_authorize_item', lambda *a: pytest.fail('absent bundle approval'))
    assert source.KeychainStore().authorize('client_id') is False


def test_current_denied_bundle_can_be_authorized_once(backend, monkeypatch):
    source, values, items, reads, writes, write = backend
    from fulcra_collect import credentials
    monkeypatch.setattr(credentials, '_keyring_get', lambda *a, **k: (_ for _ in ()).throw(
        credentials.KeychainAccessRequired('synthetic')))
    calls = []
    monkeypatch.setattr(credentials, '_authorize_item', lambda *a: calls.append(a) or True)
    assert source.KeychainStore().authorize('client_id') is True
    assert calls == [('fulcra-collect:trakt', source.KeychainStore.bundle_key)]
