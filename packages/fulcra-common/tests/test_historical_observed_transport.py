"""Inactive boundary tests. All admission/gate/transport authority is synthetic."""
import base64
import importlib
import importlib.util
import json
import socket
import subprocess
import sys
from pathlib import Path
from threading import Event, RLock, Thread
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from fulcra_common.client import BaseFulcraClient
from fulcra_common.record_observations import _schema_context
def _fixture_module(name, filename):
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


_observations = _fixture_module('_fulcra_b1p_observation_fixtures', 'test_record_observations.py')
ACCOUNT, DESTINATION, IDENTIFIER, SCHEMA = (_observations.ACCOUNT, _observations.DESTINATION,
                                          _observations.IDENTIFIER, _observations.SCHEMA)
Environment = _observations.Environment

NOW = datetime(2026, 10, 25, 22, tzinfo=timezone.utc)


def boundary():
    name = 'fulcra_common._historical_observed_transport'
    assert importlib.util.find_spec(name) is not None, 'inactive prepared-dispatch boundary is missing'
    return importlib.import_module(name)


@pytest.fixture(autouse=True)
def deny_io(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError('unstubbed external I/O')
    for owner, name in [(socket, 'create_connection'), (socket, 'getaddrinfo'),
                        (socket.socket, 'connect'), (urllib.request, 'urlopen'),
                        (subprocess, 'run'), (httpx.HTTPTransport, 'handle_request'),
                        (BaseFulcraClient, 'get_token')]:
        monkeypatch.setattr(owner, name, denied)
    for name in ('record_observations', 'data_types', 'v1', '_historical_observed_transport', '_historical_owned_transcript'):
        full = 'fulcra_common.' + name
        if importlib.util.find_spec(full) is not None:
            monkeypatch.setattr(importlib.import_module(full), '_now', lambda: '2026-10-25T22:00:00.000000Z')


@dataclass(frozen=True, repr=False)
class SyntheticAdmission:
    bearer: str
    account_id: str
    revision: int = 1
    expiry: datetime = NOW + timedelta(hours=1)
    source: str = 'synthetic-consent'


class SyntheticGate:
    def __init__(self, admission):
        self.current = admission
        self.pending = False
        self.held = False
        self.order = []
        self.lock = RLock()

    @contextmanager
    def hold_current(self, admission, *, context, mutating):
        with self.lock:
            if self.pending or self.current != admission or admission.expiry <= NOW + timedelta(seconds=120):
                raise ValueError('synthetic admission changed')
            if type(admission.revision) is not int or admission.revision < 0:
                raise ValueError('synthetic revision invalid')
            self.order.append('enter')
            self.held = True
            try:
                yield
            finally:
                self.held = False
                self.order.append('exit')


def synthetic_client(monkeypatch, handler, *, request_limit=50, event_limit=128, store=None):
    """The ONLY construction bypass: no runtime fixture factory or issuer."""
    module = boundary()
    env = Environment(monkeypatch, handler, request_limit=request_limit, event_limit=event_limit, store=store)
    # Environment's fake SDK is useful elsewhere; exercise the actual SDK here.
    from fulcra_api.core import FulcraAPI
    from fulcra_api.credentials import FulcraCredentials
    from fulcra_common import data_types
    monkeypatch.setattr(data_types, 'FulcraAPI', FulcraAPI)
    monkeypatch.setattr(data_types, 'FulcraCredentials', FulcraCredentials)
    payload = base64.urlsafe_b64encode(json.dumps({'fulcradynamics.com/userid': ACCOUNT}).encode()).decode().rstrip('=')
    admission = SyntheticAdmission('e30.' + payload + '.synthetic', ACCOUNT)
    gate = SyntheticGate(admission)
    adapter = object.__new__(module._ClosedObservedHTTP)
    client = object.__new__(module._ClosedObservedClient)
    def transport(request):
        assert gate.held, 'dispatch escaped synthetic gate'
        env.calls.append(request)
        env.order.append(request.method)
        return handler(request)
    adapter._http = httpx.Client(base_url='https://synthetic.invalid', transport=httpx.MockTransport(transport),
                                 trust_env=False, follow_redirects=False, timeout=30)
    adapter._transport = adapter._http._transport
    adapter._admission, adapter._gate, adapter._capture = admission, gate, None
    adapter._owner = client
    client._http = adapter
    client._admission, client._gate = admission, gate
    env.client.client = client
    env.admission, env.gate, env.adapter = admission, gate, adapter
    return env


def test_production_construction_refuses_before_any_preparation():
    module = boundary()
    for cls in (module._ClosedObservedClient, module._ClosedObservedHTTP):
        with pytest.raises(ValueError, match='inactive'):
            cls()


def test_actual_sdk_decode_and_exact_checked_request_once(monkeypatch):
    from fulcra_api.core import FulcraAPI
    calls = []
    original = FulcraAPI.get_fulcra_userid
    def decode(api):
        calls.append(api.fulcra_credentials.refresh_token)
        return original(api)
    monkeypatch.setattr(FulcraAPI, 'get_fulcra_userid', decode)
    env = synthetic_client(monkeypatch, lambda request: httpx.Response(200, json={'id': IDENTIFIER}))
    observation = env.read()
    assert observation.records == ({'id': IDENTIFIER},)
    assert calls == [None]
    assert env.budget._remaining == 48
    assert len(env.calls) == 1
    assert env.calls[0].headers['authorization'] == 'Bearer ' + env.admission.bearer
    assert env.gate.order == ['enter', 'exit']
    assert env.order[-2:] == ['GET', 'sink:exact-read']
    assert env.order[env.order.index('GET') - 1] == 'guard:record-get'


@pytest.mark.parametrize('change', ['hooks', 'cookies', 'auth', 'redirects', 'mounts', 'transport', 'environment'])
def test_unsupported_client_mutation_refuses_before_send(monkeypatch, change):
    env = synthetic_client(monkeypatch, lambda _: pytest.fail('unsupported client sent HTTP'))
    http = env.adapter._http
    if change == 'hooks':
        http.event_hooks['request'].append(lambda request: request.headers.update(Authorization='changed'))
    elif change == 'cookies':
        http.cookies.set('private', 'synthetic')
    elif change == 'auth':
        http.auth = ('synthetic', 'synthetic')
    elif change == 'redirects':
        http.follow_redirects = True
    elif change == 'mounts':
        http._mounts[httpx._utils.URLPattern('all://')] = httpx.MockTransport(lambda _: pytest.fail('mount'))
    elif change == 'transport':
        http._transport = httpx.MockTransport(lambda _: pytest.fail('transport'))
    else:
        http._trust_env = True
    with pytest.raises(ValueError):
        env.read()
    assert not env.calls


@pytest.mark.parametrize('change', ['bearer', 'revision', 'source', 'expiry', 'pending'])
def test_snapshot_change_during_preparation_has_no_get(monkeypatch, change):
    env = synthetic_client(monkeypatch, lambda _: pytest.fail('changed admission reached GET'))
    original = env.guard
    def guard(*, kind, context):
        original(kind=kind, context=context)
        if kind == 'account-proof':
            if change == 'pending':
                env.gate.pending = True
            else:
                values = {'bearer': 'synthetic-other', 'revision': 2, 'source': 'revoked', 'expiry': NOW}
                env.gate.current = replace(env.admission, **{change: values[change]})
    env.guard = guard
    with pytest.raises(ValueError):
        env.read()
    assert not env.calls


@pytest.mark.parametrize('limit,expected', [(0, 0), (1, 0), (2, 1)])
def test_nested_budget_charge_is_not_duplicated_or_refunded(monkeypatch, limit, expected):
    env = synthetic_client(monkeypatch, lambda _: httpx.Response(200, json={'id': IDENTIFIER}), request_limit=limit)
    if expected:
        env.read()
    else:
        with pytest.raises(ValueError):
            env.read()
    assert len(env.calls) == expected
    assert env.budget._remaining == 0


@pytest.mark.parametrize('status', [401, 303])
def test_error_status_never_retries_redirects_or_refreshes(monkeypatch, status):
    env = synthetic_client(monkeypatch, lambda _: httpx.Response(status, headers={'location': 'https://other.invalid'}))
    with pytest.raises((ValueError, httpx.HTTPStatusError)):
        env.read()
    assert len(env.calls) == 1
    assert env.budget._remaining == 48


def test_final_guard_cannot_swap_prepared_bearer(monkeypatch):
    env = synthetic_client(monkeypatch, lambda _: pytest.fail('mutated sealed request sent'))
    module = boundary()
    prepared = env.adapter.prepare('GET', '/data/v1/records', params={'q': 'synthetic', 'fulcra_userid': ACCOUNT},
                                   headers={'Authorization': 'Bearer ' + env.admission.bearer}, content=b'')
    def guard(**kwargs):
        prepared._request.headers['Authorization'] = 'Bearer swapped'
    with pytest.raises(ValueError):
        module._dispatch_observed(prepared=prepared, kind='record-get', context=env.context,
            request_guard=guard, request_budget=env.budget, admission=env.admission, gate=env.gate)
    assert not env.calls
    assert env.budget._remaining == 49
    assert not env.gate.held


def test_expiry_in_received_sink_retains_without_new_authority(monkeypatch):
    env = synthetic_client(monkeypatch, lambda _: httpx.Response(200, json={'id': IDENTIFIER}))
    env.after_sink = lambda observation: setattr(env.gate, 'pending', True)
    observation = env.read()
    assert env.retained.lookup(observation.ref).observation == observation
    with pytest.raises(ValueError):
        env.read()
    assert len(env.calls) == 1


def test_post_prepares_before_issued_then_final_gate_can_refuse(monkeypatch):
    env = synthetic_client(monkeypatch, lambda request: httpx.Response(200, json=SCHEMA))
    issued = []
    def before_submit():
        issued.append('issued')
        env.gate.pending = True
    with pytest.raises(ValueError):
        env.client.ingest_observed(DESTINATION, [{'id': IDENTIFIER, 'note': 'synthetic'}],
            expected_schema=SCHEMA, before_submit=before_submit,
            **env.kwargs(replace(env.context, purpose='upload-receipt')))
    assert issued == ['issued']
    assert [r.method for r in env.calls] == ['GET']
    assert len(env.retained._load()['slots']) == 2
    assert env.retained._load()['slots'][-1]['state'] == 'started'


def test_schema_uses_same_actual_response_and_one_get(monkeypatch):
    from fulcra_common.data_types import observe_served_schema
    env = synthetic_client(monkeypatch, lambda _: httpx.Response(200, content=b'{ "type" : "object", "properties" : {} }'))
    observation = observe_served_schema(client=env.client.client, data_type=DESTINATION,
        **env.kwargs(_schema_context(env.context)))
    assert observation.schema == {'type': 'object', 'properties': {}}
    assert len(env.calls) == 1
    assert env.budget._remaining == 48


def test_final_guard_failure_keeps_charge_and_started_slot(monkeypatch):
    env = synthetic_client(monkeypatch, lambda _: pytest.fail('guard refusal sent HTTP'))
    original = env.guard
    gets = []
    def guard(*, kind, context):
        original(kind=kind, context=context)
        if kind == 'record-get':
            gets.append(kind)
            if len(gets) == 2:
                raise ValueError('synthetic final guard refused')
    env.guard = guard
    with pytest.raises(ValueError):
        env.read()
    assert env.budget._remaining == 48 and not env.calls
    assert env.retained._load()['slots'][0]['state'] == 'started'


def test_timeout_has_no_retry_and_hides_secret_bearing_exception(monkeypatch):
    def timeout(request):
        raise httpx.ReadTimeout('synthetic private bearer must not escape', request=request)
    env = synthetic_client(monkeypatch, timeout)
    with pytest.raises(ValueError) as error:
        env.read()
    assert 'private bearer' not in str(error.value)
    assert len(env.calls) == 1 and env.budget._remaining == 48
    assert not env.gate.held


def test_received_receipt_survives_expiry_without_second_post(monkeypatch):
    env = synthetic_client(monkeypatch, lambda request: httpx.Response(200, json=SCHEMA) if request.method == 'GET'
                           else httpx.Response(201, json={'upload_id': 'synthetic'}))
    def after_sink(observation):
        if observation.document['kind'] == 'upload-receipt':
            env.active = False
    env.after_sink = after_sink
    receipt = env.client.ingest_observed(DESTINATION, [{'id': IDENTIFIER, 'note': 'synthetic'}],
        expected_schema=SCHEMA, before_submit=lambda: None,
        **env.kwargs(replace(env.context, purpose='upload-receipt')))
    assert env.retained.lookup(receipt.ref).observation == receipt
    assert [request.method for request in env.calls] == ['GET', 'POST']
    assert env.budget._remaining == 46


@pytest.mark.parametrize('timeout', [0, -1, 31, float('nan'), float('inf')])
def test_closed_schema_timeout_override_refuses_before_http(monkeypatch, timeout):
    from fulcra_common.data_types import observe_served_schema
    env = synthetic_client(monkeypatch, lambda _: pytest.fail('unbounded timeout reached HTTP'))
    with pytest.raises(ValueError):
        observe_served_schema(client=env.client.client, data_type=DESTINATION,
            timeout=timeout, **env.kwargs(_schema_context(env.context)))
    assert not env.calls


def test_dispatch_holds_synthetic_gate_against_participating_writer(monkeypatch):
    attempting, changed = Event(), Event()
    workers = []
    def handler(request):
        def writer():
            attempting.set()
            with env.gate.lock:
                env.gate.current = replace(env.admission, revision=2)
                changed.set()
        worker = Thread(target=writer)
        workers.append(worker)
        worker.start()
        assert attempting.wait(1)
        assert not changed.is_set(), 'participating writer escaped held dispatch gate'
        return httpx.Response(200, json={'id': IDENTIFIER})
    env = synthetic_client(monkeypatch, handler)
    env.read()
    for worker in workers:
        worker.join(1)
        assert not worker.is_alive()
    assert changed.is_set()
    with pytest.raises(ValueError):
        env.read()
    assert len(env.calls) == 1
