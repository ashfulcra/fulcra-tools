"""The actual versioned daemon and HTTP transports preserve the full scope."""
from datetime import timedelta
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

from test_quick_record_actor import runtime as runtime
from test_quick_record_actor import ACCOUNT, NOW
from test_quick_record_controller import controller as controller
from test_quick_record_controller import DIGEST, TRACK, scope

from fulcra_collect import config
from fulcra_collect.daemon import Daemon
from fulcra_collect.registry import RegistryResult
from fulcra_collect.web import build_app, _ensure_token


def envelope(controller, operation='record_annotation', **fields):
    return {'cmd': 'quick_record_v2', 'operation': operation, **scope(controller), **fields}


@pytest.fixture
def daemon(controller, runtime, monkeypatch):
    import fulcra_collect.quick_record_v1 as authority
    session, _, _ = runtime
    original = authority.QuickRecordSession
    transport = session.client._client()._transport
    def bound_session(ctx, **kwargs):
        current = original(ctx, transport=transport, **kwargs)
        current._exclusion_type = session._exclusion_type
        return current
    monkeypatch.setattr(authority, 'QuickRecordSession', bound_session)
    return Daemon(registry=RegistryResult(), config=config.load())


def test_versioned_daemon_path_executes_real_controller_and_persists_one_action(daemon, controller, runtime):
    _, journal, state = runtime
    identity = str(uuid4())
    state['action_id'] = identity
    body = envelope(controller, action_id=identity, definition_id=TRACK, choice_digest=DIGEST,
        start_time=NOW.isoformat(), end_time=(NOW + timedelta(hours=1)).isoformat(), comment='Synthetic note')
    first = daemon.handle_request(body)
    assert first['ok'] and first['status'] == 'verified' and first['action_id'] == identity, first
    assert journal.inspect(identity, account_id=ACCOUNT).status == 'verified'
    assert daemon.handle_request(body) == first and len(state['posts']) == 1


def test_obsolete_versioned_mutation_is_rejected_before_any_credential_lookup(daemon, monkeypatch):
    import fulcra_collect.quick_record_v1 as authority
    monkeypatch.setattr(authority, 'quick_record_context', lambda: pytest.fail('stale protocol read credentials'))
    reply = daemon.handle_request({'cmd': 'quick_record_v2', 'operation': 'record_annotation'})
    assert not reply['ok'] and reply['code'] == 'refresh_required'


def test_lost_response_returns_original_pending_action_without_source_values(daemon, controller, runtime):
    _, journal, state = runtime
    identity = str(uuid4())
    state.update(action_id=identity, lose_reply=True, delay=True)
    body = envelope(controller, action_id=identity, definition_id=TRACK, choice_digest=DIGEST,
        start_time=NOW.isoformat(), end_time=(NOW + timedelta(hours=1)).isoformat(), comment='Synthetic private note')
    pending = daemon.handle_request(body)
    assert not pending['ok'] and pending['accepted'] and pending['status'] == 'submitted'
    assert pending['action_id'] == identity and pending['code'] == 'pending'
    assert 'Synthetic private note' not in str(pending)
    assert journal.inspect(identity, account_id=ACCOUNT).status == 'submitted'
    assert len(state['posts']) == 1


@pytest.mark.parametrize('receipt', [{}, {'upload_id': 123}])
def test_unreadable_receipt_preserves_pending_metadata_without_claiming_auth_refresh(daemon, controller, runtime, receipt):
    _, journal, state = runtime
    identity = str(uuid4())
    state.update(action_id=identity, target_receipt=receipt, delay=True)
    body = envelope(controller, action_id=identity, definition_id=TRACK, choice_digest=DIGEST,
        start_time=NOW.isoformat(), end_time=(NOW + timedelta(hours=1)).isoformat(), comment='Synthetic note')
    pending = daemon.handle_request(body)
    assert pending['code'] == 'pending' and pending['status'] == 'submitted'
    assert pending['accepted'] and not pending['ok'] and pending['action_id'] == identity
    assert journal.inspect(identity, account_id=ACCOUNT).status == 'submitted' and len(state['posts']) == 1


def test_malformed_timer_id_is_invalid_request_instead_of_auth_refresh(daemon, controller, runtime):
    _, journal, state = runtime
    reply = daemon.handle_request(envelope(controller, 'quick_record_timer_stop', timer_id='bad',
        end_time=(NOW + timedelta(hours=1)).isoformat(), comment=None))
    assert reply['code'] == 'invalid_request' and not reply['ok']
    assert journal.list_actions(account_id=ACCOUNT).actions == () and not state['posts']


@pytest.fixture
def http_client(collect_home, monkeypatch):
    daemon = Daemon(registry=RegistryResult(), config=config.Config())
    calls = []
    monkeypatch.setattr(daemon, 'handle_request', lambda body: calls.append(body) or {'ok': True, 'seen': body})
    token = _ensure_token()
    client = TestClient(build_app(daemon))
    client.headers['Authorization'] = 'Bearer ' + token
    return client, calls


def test_versioned_http_requires_local_authorization(http_client):
    client, calls = http_client
    client.headers.clear()
    assert client.get('/api/quick-record/v2').status_code == 401
    assert client.post('/api/quick-record/v2', json={'operation': 'quick_record_list'}).status_code == 401
    assert calls == []


def test_versioned_http_preserves_scope_and_ids_without_dropping_new_fields(http_client):
    client, calls = http_client
    body = {'operation': 'record_annotation', 'protocol_version': 2, 'account_id': ACCOUNT,
            'config_epoch': 'synthetic-epoch', 'action_id': str(uuid4()), 'definition_id': TRACK,
            'choice_digest': DIGEST, 'start_time': NOW.isoformat(), 'end_time': None,
            'comment': 'Synthetic note'}
    reply = client.post('/api/quick-record/v2', json=body)
    assert reply.status_code == 200
    assert calls == [{'cmd': 'quick_record_v2', **body}]


@pytest.mark.parametrize('change', [{'extra_field': True}, {'protocol_version': True},
                                    {'expected_revision': True}, {'pinned': 'false'}])
def test_versioned_http_rejects_extra_or_coerced_fields(http_client, change):
    client, calls = http_client
    reply = client.post('/api/quick-record/v2', json={'operation': 'quick_record_list', **change})
    assert reply.status_code == 422 and calls == []


@pytest.mark.parametrize('command', ['quick_record_list', 'record_annotation', 'delete_annotation',
    'get_quick_record_favorites', 'set_quick_record_favorites'])
def test_unversioned_quick_record_commands_refuse_before_credentials_or_effects(collect_home, monkeypatch, command):
    import fulcra_collect.credentials as credentials
    monkeypatch.setattr(credentials, 'get_user_secret', lambda *a, **k: pytest.fail('obsolete request read credentials'))
    daemon = Daemon(registry=RegistryResult(), config=config.Config())
    result = daemon.handle_request({'cmd': command, 'source_id': 'synthetic.old',
        'definition_id': TRACK, 'favorites': [TRACK], 'comment': 'Synthetic private note'})
    assert result['ok'] is False and result['code'] == 'refresh_required'
    assert not (collect_home / 'quick_record_favorites.json').exists()
    assert not (collect_home / 'quick-record-actions.sqlite').exists()
    assert 'Synthetic private note' not in str(result)


@pytest.mark.parametrize('method,path,body', [
    ('get', '/api/quick-record/definitions', None), ('get', '/api/quick-record/favorites', None),
    ('put', '/api/quick-record/favorites', {'favorites': [TRACK]}),
    ('post', '/api/annotations', {'definition_id': TRACK}),
    ('delete', '/api/annotations/synthetic.old', None),
])
def test_obsolete_http_surface_requires_browser_auth_then_refuses_old_mutations(collect_home, monkeypatch, method, path, body):
    import fulcra_collect.credentials as credentials
    monkeypatch.setattr(credentials, 'get_user_secret', lambda *a, **k: pytest.fail('obsolete HTTP read credentials'))
    daemon = Daemon(registry=RegistryResult(), config=config.Config())
    client = TestClient(build_app(daemon))
    kwargs = {'json': body} if body is not None else {}
    assert client.request(method, path, **kwargs).status_code == 401
    client.headers['Authorization'] = 'Bearer ' + _ensure_token()
    response = client.request(method, path, **kwargs)
    assert response.status_code == 200 and response.json()['code'] == 'refresh_required'


@pytest.fixture
def native_runtime(daemon, collect_home):
    import threading
    from fulcra_collect.control import ControlServer
    from fulcra_collect.record_actions import ActionJournal
    from fulcra_menubar.daemon_client import DaemonClient
    from fulcra_menubar.quick_record_client import QuickRecordClient
    from fulcra_menubar.quick_record_requests import ClientRequestJournal
    server = ControlServer(collect_home / 'native-test.sock', daemon.handle_request)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    server.wait_ready(2)
    transport = DaemonClient(socket_path=collect_home / 'native-test.sock')
    journal = ClientRequestJournal(ActionJournal(collect_home / 'native-client-test.sqlite'))
    yield QuickRecordClient(transport, journal), transport
    server.shutdown()
    thread.join(2)


def test_native_durable_client_crosses_actual_socket_backend_and_lost_reply_restart(native_runtime, runtime):
    from fulcra_collect.record_actions import ActionJournal
    from fulcra_menubar.quick_record_requests import ClientRequestJournal
    native, transport = native_runtime
    _, _, cloud = runtime
    view = native.bootstrap()
    pinned = native.change_pin(TRACK, pinned=True, view=view)
    assert pinned['favorites'] == [TRACK] and pinned['revision'] == 1
    view = native.bootstrap()
    identity = str(uuid4())
    cloud['action_id'] = identity
    body = {'operation': 'record_annotation', **scope_from(view), 'action_id': identity,
        'definition_id': TRACK, 'choice_digest': DIGEST, 'start_time': NOW.isoformat(),
        'end_time': (NOW + timedelta(hours=1)).isoformat(), 'comment': 'Synthetic private native note'}
    native.capture(request_id=identity, body=body, current_scope=view)
    real = transport.quick_record_v2
    def lost_response(operation, **fields):
        real(operation, **fields)
        raise TimeoutError('synthetic lost socket reply after backend completion')
    transport.quick_record_v2 = lost_response
    with pytest.raises(TimeoutError):
        native.retry(identity, current_scope=view)
    assert native.journal.get(identity, account_id=ACCOUNT).status == 'pending'
    transport.quick_record_v2 = real
    restarted = type(native)(transport, ClientRequestJournal(ActionJournal(native.journal.journal.path)))
    assert restarted.retry(identity, current_scope=view)['status'] == 'verified'
    assert restarted.journal.get(identity, account_id=ACCOUNT).body == body
    assert len(cloud['posts']) == 1
    assert native.bootstrap()['recent'][0]['action_id'] == identity


def scope_from(view):
    return {key: view[key] for key in ('protocol_version', 'account_id', 'config_epoch')}


def test_native_timer_stop_and_undo_preserve_note_window_and_native_delete_proof(native_runtime, runtime, monkeypatch):
    from uuid import UUID
    import fulcra_collect.quick_record_state as timer_state
    native, _ = native_runtime
    _, _, cloud = runtime
    view = native.bootstrap()
    timer_id, stop_id, action_id = str(uuid4()), str(uuid4()), str(uuid4())
    monkeypatch.setattr(timer_state, 'uuid4', lambda: UUID(action_id))
    start = {'operation': 'quick_record_timer_start', **scope_from(view), 'timer_id': timer_id,
        'definition_id': TRACK, 'choice_digest': DIGEST, 'start_time': NOW.isoformat()}
    assert native.submit(request_id=timer_id, body=start, current_scope=view)['timer']['status'] == 'running'
    cloud['action_id'] = action_id
    end = (NOW + timedelta(minutes=90)).isoformat()
    stop = {'operation': 'quick_record_timer_stop', **scope_from(view), 'timer_id': timer_id,
            'end_time': end, 'comment': 'Synthetic retained stop note'}
    result = native.submit(request_id=stop_id, body=stop, current_scope=view)
    assert result['status'] == result['timer']['status'] == 'verified'
    assert result['action_id'] == action_id and result['timer']['end_time'] == end
    assert cloud['posts'][0][1][0]['note'] == stop['comment']
    assert native.journal.timer_stop(timer_id, account_id=ACCOUNT).body == stop
    undo_id = str(uuid4())
    undone = native.submit(request_id=undo_id, current_scope=view, body={**scope_from(view),
        'operation': 'delete_annotation', 'action_id': undo_id, 'record_action_id': action_id})
    assert undone['status'] == 'verified' and undone['operation'] == 'delete'
    refreshed = native.bootstrap()
    assert refreshed['recent'][0]['undone'] and refreshed['timers'] == []
    assert [destination.split('/')[0] for destination, _ in cloud['posts']] == ['Event', 'Event', 'DeletedRecord']
