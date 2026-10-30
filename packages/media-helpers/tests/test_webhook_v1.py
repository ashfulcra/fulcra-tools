"""Actual configured runtime/SQLite and ephemeral HTTP; no provider traffic."""
import json
import threading

import pytest
from click.testing import CliRunner

from fulcra_media.cli import cli
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import http.client
import socket
import time

from fulcra_collect import db
from fulcra_media.importers.base import NormalizedEvent
from fulcra_media.state import State
from fulcra_media.webhook_receiver import make_server
from test_webhook_receiver import jellyfin_payload, plex_payload, _multipart_body

from webhook_authority import authority as authority




def test_cli_configured_v1_reaches_bind_without_legacy_state(authority, monkeypatch):
    seen = []
    monkeypatch.setattr('fulcra_media.state.load', lambda *_: pytest.fail('legacy state cache load'))
    def bind(**kwargs):
        seen.append(kwargs['client'].record_destination_ids)
        raise OSError('synthetic occupied port')
    monkeypatch.setattr('fulcra_media.webhook_receiver.make_server', bind)
    res = CliRunner().invoke(cli, ['webhook', '--port', '0', '--json'])
    assert res.exit_code == 2, res.output
    assert json.loads(res.output)['errors'][0]['stage'] == 'bind'
    assert seen == [authority.settings['v1_destination_ids']]
    assert len(set(authority.threads)) == 1
    assert authority.threads[0] != threading.get_ident()



def until(predicate):
    deadline = time.monotonic() + 5
    while not predicate():
        assert time.monotonic() < deadline, 'test barrier did not arrive'
        time.sleep(0.005)


def sample(name='one'):
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return NormalizedEvent(importer='synthetic', service='synthetic', category='watched',
        note='Synthetic', title=name, start_time=start, end_time=start + timedelta(minutes=1),
        deterministic_id='com.fulcra.media.synthetic.v1.' + name, timestamp_confidence='high',
        external_ids={'nested': {'values': [1]}})


@contextmanager
def owner_for(authority):
    from fulcra_media.webhook_owner import MediaOwner
    wake = threading.Event()
    owner = MediaOwner(authority.root.parent / 'legacy', wake)
    owner.start()
    try:
        yield owner
    finally:
        owner.close()
    assert owner.done.is_set()
    assert not owner._thread.is_alive()


@contextmanager
def serving(authority, token='synthetic-bearer'):
    from fulcra_media.webhook_owner import ServeLoop
    with owner_for(authority) as owner:
        server = make_server(host='127.0.0.1', port=0, state=State(), client=owner,
                             bearer_token=token)
        loop = ServeLoop(server, threading.Event())
        loop.start()
        assert loop.entered.wait(5)
        try:
            yield owner, server, loop
        finally:
            owner.stop_admission()
            loop.close()
            server.server_close()


def request(server, payload=None, *, path='/webhook', token='synthetic-bearer', raw=None, ct='application/json'):
    conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
    body = raw if raw is not None else json.dumps(payload).encode() if payload is not None else None
    headers = {'Content-Type': ct, 'Authorization': 'Bearer ' + token}
    try:
        conn.request('POST' if body is not None else 'GET', path, body=body, headers=headers)
        reply = conn.getresponse()
        content = reply.read()
        return reply.status, json.loads(content) if content else None
    finally:
        conn.close()


def pending(authority):
    import sqlite3
    conn = sqlite3.connect(authority.root / 'state.db')
    conn.row_factory = sqlite3.Row
    try:
        return db.get_record_outbox(conn, 'cli-media', 'v1_media_pending')
    finally:
        conn.close()


def test_real_http_actual_writer_dedup_plex_jellyfin_and_health(authority):
    with serving(authority) as (owner, server, _):
        status, health = request(server, path='/health')
        assert status == 200 and health['api_version'] == 'v1'
        assert 'definition_id' not in health
        assert health['data_type_id'] == authority.settings['v1_destination_ids']['watched']
        assert request(server, jellyfin_payload()) == (200, {'posted': 1, 'skipped': 0})
        assert request(server, jellyfin_payload()) == (200, {'posted': 0, 'skipped': 1})
        body, ct = _multipart_body(plex_payload())
        assert request(server, raw=body, ct=ct) == (200, {'posted': 1, 'skipped': 0})
        assert request(server, jellyfin_payload('PlaybackStart')) == (204, None)
        assert request(server, jellyfin_payload(), token='wrong')[0] == 401
        assert request(server, raw=b'broken')[0] == 400
        assert request(server, path='/health')[1]['posted'] == 2
        destinations = owner.record_destination_ids
        destinations['watched'] = 'bad'
        assert owner.record_destination_ids['watched'] != 'bad'
    assert len(authority.posts) == 2
    assert len(set(authority.threads)) == 1
    assert authority.threads[0] != threading.get_ident()
    assert pending(authority) == {'groups': []}
    assert any(source.startswith('com.fulcra.media.jellyfin.')
               for row in authority.posts for source in row['sources'])


def test_blocked_writer_keeps_health_auth_parse_responsive_and_drains_before_exit(authority):
    blocked, release = threading.Event(), threading.Event()
    def hold():
        blocked.set()
        assert release.wait(5)
    authority.before_post = hold
    with serving(authority) as (owner, server, _), ThreadPoolExecutor(3) as pool:
        first = pool.submit(request, server, jellyfin_payload())
        assert blocked.wait(5)
        other = jellyfin_payload()
        other['Item']['Id'] = 'second'
        second = pool.submit(request, server, other)
        until(lambda: owner._queue.qsize() == 1)
        assert request(server, path='/health')[0] == 200
        assert request(server, jellyfin_payload(), token='wrong')[0] == 401
        assert request(server, raw=b'bad')[0] == 400
        owner.stop_admission()
        closer = pool.submit(owner.close)
        assert not closer.done()
        with pytest.raises(RuntimeError, match='closed'):
            owner.ingest_batch([sample('late')], State())
        release.set()
        assert first.result(5) == second.result(5) == (200, {'posted': 1, 'skipped': 0})
        closer.result(5)
        assert owner.done.is_set()
    assert len(authority.posts) == 2
    assert len(set(authority.threads)) == 1


def test_events_snapshot_includes_nested_values_before_queued_admission(authority):
    blocked, release = threading.Event(), threading.Event()
    def hold():
        blocked.set()
        assert release.wait(5)
    authority.before_post = hold
    with owner_for(authority) as owner, ThreadPoolExecutor(2) as pool:
        first = pool.submit(owner.ingest_batch, [sample()], State())
        assert blocked.wait(5)
        event = sample('original')
        second = pool.submit(owner.ingest_batch, [event], State())
        until(lambda: owner._queue.qsize() == 1)
        event.title = 'MUTATED'
        event.external_ids['nested']['values'].append(2)
        queued = owner._queue.queue[0].events[0]
        assert queued.title == 'original' and queued.external_ids == {'nested': {'values': [1]}}
        release.set()
        assert first.result(5).verified == second.result(5).verified == 1
    assert authority.posts[1]['title'] == 'original'


@pytest.mark.parametrize('mode', ['lost', 'unknown'])
def test_once_only_delivery_and_later_call_preserves_original_pending(authority, mode):
    authority.mode = mode
    with serving(authority) as (_, server, _):
        assert request(server, jellyfin_payload())[0] == (502 if mode == 'lost' else 502)
        original = pending(authority)
        assert original['groups']
        authority.mode = 'visible' if mode == 'lost' else 'unreadable'
        if mode == 'lost':
            assert request(server, jellyfin_payload()) == (200, {'posted': 0, 'skipped': 1})
            assert len(authority.posts) == 1
            assert request(server, path='/health')[1]['posted'] == 0
        other = jellyfin_payload()
        other['Item']['Id'] = 'second'
        status, result = request(server, other)
        if mode == 'lost':
            assert status == 200 and result == {'posted': 1, 'skipped': 0}
            assert len(authority.posts) == 2
        else:
            assert status == 502 and 'posted' not in result
            assert pending(authority) == original
            assert len(authority.posts) == 1


def test_ordinary_job_failure_continues_and_fatal_settles_all_callers(authority, monkeypatch):
    from fulcra_media.standalone_v1 import StandaloneMediaRun
    from fulcra_media.webhook_owner import OwnerFailed
    original = StandaloneMediaRun.import_events
    entered, release = threading.Event(), threading.Event()
    def dispatch(run, events):
        if events[0].title == 'ordinary':
            raise ValueError('synthetic ordinary failure')
        if events[0].title == 'fatal':
            entered.set()
            assert release.wait(5)
            raise SystemExit('synthetic fatal')
        return original(run, events)
    monkeypatch.setattr(StandaloneMediaRun, 'import_events', dispatch)
    with owner_for(authority) as owner, ThreadPoolExecutor(2) as pool:
        with pytest.raises(ValueError):
            owner.ingest_batch([sample('ordinary')], State())
        assert owner.ingest_batch([sample('success')], State()).verified == 1
        first = pool.submit(owner.ingest_batch, [sample('fatal')], State())
        assert entered.wait(5)
        second = pool.submit(owner.ingest_batch, [sample('queued')], State())
        until(lambda: owner._queue.qsize() == 1)
        release.set()
        for job in (first, second):
            with pytest.raises(OwnerFailed):
                job.result(5)
        assert owner._wake.wait(5)
        assert isinstance(owner.failure, SystemExit)
    assert len(authority.posts) == 1


def test_late_body_parser_rejects_after_owner_cleanup_without_authority(authority):
    with serving(authority) as (owner, server, _):
        body = json.dumps(jellyfin_payload()).encode()
        sock = socket.create_connection(('127.0.0.1', server.server_port), timeout=5)
        sock.sendall((f'POST /webhook HTTP/1.1\r\nHost: localhost\r\n'
                      f'Content-Type: application/json\r\nContent-Length: {len(body)}\r\n'
                      'Authorization: Bearer synthetic-bearer\r\n\r\n').encode() + body[:1])
        owner.close()
        before = list(authority.threads)
        sock.sendall(body[1:])
        response = http.client.HTTPResponse(sock)
        response.begin()
        assert response.status == 502
        assert json.loads(response.read())['error'] == 'ingest_failed'
        sock.close()
        assert authority.threads == before and authority.posts == []


def test_disconnected_accepted_caller_does_not_cancel_delivery(authority):
    entered, release = threading.Event(), threading.Event()
    def hold():
        entered.set()
        assert release.wait(5)
    authority.before_post = hold
    with serving(authority) as (owner, server, _):
        body = json.dumps(jellyfin_payload()).encode()
        sock = socket.create_connection(('127.0.0.1', server.server_port), timeout=5)
        sock.sendall((f'POST /webhook HTTP/1.1\r\nHost: localhost\r\n'
                      f'Content-Type: application/json\r\nContent-Length: {len(body)}\r\n'
                      'Authorization: Bearer synthetic-bearer\r\n\r\n').encode() + body)
        assert entered.wait(5)
        sock.close()
        release.set()
        owner.close()
    assert len(authority.posts) == 1 and pending(authority) == {'groups': []}


def test_startup_recovers_original_pending_before_cli_bind_without_new_counts(authority, monkeypatch):
    authority.mode = 'lost'
    with owner_for(authority) as owner:
        with pytest.raises(Exception):
            owner.ingest_batch([sample()], State())
    assert len(authority.posts) == 1 and pending(authority)['groups']
    authority.mode = 'visible'
    seen = []
    def bind(**kwargs):
        assert pending(authority) == {'groups': []}
        assert len(authority.posts) == 1  # visible original recovered without redispatch
        from fulcra_media.webhook_receiver import ReceiverContext
        health = ReceiverContext(client=kwargs['client'], state=kwargs['state'],
                                 bearer_token=None, host='127.0.0.1').health()
        seen.append(health)
        raise OSError('synthetic occupied')
    monkeypatch.setattr('fulcra_media.webhook_receiver.make_server', bind)
    res = CliRunner().invoke(cli, ['webhook', '--port', '0', '--json'])
    assert res.exit_code == 2 and json.loads(res.output)['errors'][0]['stage'] == 'bind'
    assert seen[0]['posted'] == seen[0]['received'] == seen[0]['skipped'] == 0
    assert len(authority.posts) == 1


@pytest.mark.parametrize('change', ['account', 'origin', 'config', 'journal', 'profile', 'auth'])
def test_lifetime_authority_change_holds_before_new_post(authority, monkeypatch, change):
    from fulcra_common.data_types import CreationJournal
    from fulcra_media.media_config import FILENAME
    from fulcra_media.v1 import media_spec
    with owner_for(authority) as owner:
        assert owner.ingest_batch([sample()], State()).verified == 1
        before = pending(authority)
        if change == 'account':
            authority.account = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'
        elif change == 'origin':
            authority.client.base_url = 'https://changed.test'
        elif change == 'config':
            settings = json.loads((authority.root / FILENAME).read_text())
            settings['account_id'] = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'
            (authority.root / FILENAME).write_text(json.dumps(settings))
        elif change == 'journal':
            CreationJournal(authority.root / 'v1-type-creations.sqlite').resolved(
                authority.settings['account_id'], media_spec('watched'),
                'Event/bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb')
        elif change == 'profile':
            monkeypatch.setenv('FULCRA_COLLECT_HOME', str(authority.root.parent / 'other-profile'))
        else:
            monkeypatch.setattr(authority.client, 'get_token', lambda: '')
        with pytest.raises((RuntimeError, ValueError)):
            owner.ingest_batch([sample('new')], State())
        assert len(authority.posts) == 1
        assert pending(authority) == before


def test_lifetime_settings_lease_is_bounded_and_released(authority):
    from fulcra_media.media_config import configured_media
    path = authority.root.parent / 'legacy'
    with owner_for(authority):
        with pytest.raises(TimeoutError):
            with configured_media(path, client=authority.client, lease_timeout_s=0):
                pytest.fail('competing authority yielded')
    with configured_media(path, client=authority.client, lease_timeout_s=0):
        pass


def test_close_admission_race_settles_each_accepted_call_once(authority):
    from fulcra_media.webhook_owner import OwnerClosed
    gate = threading.Barrier(9)
    with owner_for(authority) as owner, ThreadPoolExecutor(9) as pool:
        def invoke(i):
            gate.wait(timeout=5)
            try:
                return owner.ingest_batch([sample(str(i))], State())
            except OwnerClosed:
                return None
        calls = [pool.submit(invoke, i) for i in range(8)]
        def stop():
            gate.wait(timeout=5)
            owner.close()
        closer = pool.submit(stop)
        results = [call.result(10) for call in calls]
        closer.result(10)
        assert len(authority.posts) == sum(r is not None for r in results)
        assert all(r is None or r.total == r.posted == r.verified == 1 for r in results)
        assert owner.done.is_set()


def test_snapshot_failure_rejects_before_admission(authority):
    class CannotCopy:
        def __deepcopy__(self, memo):
            raise ValueError('synthetic snapshot failure')
    with owner_for(authority) as owner:
        event = sample()
        event.external_ids['bad'] = CannotCopy()
        before = list(authority.threads)
        with pytest.raises(ValueError, match='snapshot'):
            owner.ingest_batch([event], State())
        assert owner._queue.empty() and authority.posts == []
        assert authority.threads == before


def test_same_principal_cli_token_refresh_requires_fresh_proof(authority, monkeypatch):
    with owner_for(authority) as owner:
        monkeypatch.setenv('FULCRA_ACCESS_TOKEN', authority.token.replace('.signature', '.refreshed-signature'))
        assert owner.ingest_batch([sample()], State()).verified == 1
    assert len(authority.posts) == 1


@pytest.mark.parametrize('phase', ['entry', 'publish', 'cleanup'])
def test_owner_outer_guard_covers_entry_publish_and_cleanup_on_owner_thread(authority, monkeypatch, phase):
    from fulcra_media import media_config
    from fulcra_media.collect_v1 import MediaRecordWriter
    from fulcra_media.webhook_owner import MediaOwner, OwnerFailed
    original = media_config.configured_media
    cleanup_threads = []
    @contextmanager
    def context(path):
        if phase == 'entry':
            raise SystemExit('synthetic entry failure')
        with original(path) as run:
            try:
                yield run
            finally:
                cleanup_threads.append(threading.get_ident())
        if phase == 'cleanup':
            raise SystemExit('synthetic cleanup failure')
    monkeypatch.setattr(media_config, 'configured_media', context)
    if phase == 'publish':
        def fail(_self):
            raise SystemExit('synthetic publish failure')
        monkeypatch.setattr(MediaRecordWriter, 'destination_ids', property(fail))
    owner = MediaOwner(authority.root.parent / 'legacy', threading.Event())
    if phase != 'cleanup':
        with pytest.raises(OwnerFailed):
            owner.start()
    else:
        owner.start()
        result = owner.ingest_batch([sample()], State())
        owner.close()
        assert result.verified == result.posted == 1  # cleanup cannot rewrite an actual receipt
    assert owner.done.is_set() and owner._wake.is_set()
    assert isinstance(owner.failure, SystemExit)
    if phase != 'entry':
        assert cleanup_threads == [owner._thread.ident]
    with pytest.raises(OwnerFailed):
        owner.ingest_batch([sample('late')], State())


def test_fatal_job_keeps_primary_when_cleanup_also_fails(authority, monkeypatch):
    from fulcra_media import media_config
    from fulcra_media.standalone_v1 import StandaloneMediaRun
    from fulcra_media.webhook_owner import OwnerFailed
    original = media_config.configured_media
    @contextmanager
    def context(path):
        try:
            with original(path) as run:
                yield run
        finally:
            raise RuntimeError('synthetic cleanup secondary')
    def fatal(*_):
        raise SystemExit('synthetic primary')
    monkeypatch.setattr(media_config, 'configured_media', context)
    monkeypatch.setattr(StandaloneMediaRun, 'import_events', fatal)
    with owner_for(authority) as owner:
        with pytest.raises(OwnerFailed):
            owner.ingest_batch([sample()], State())
    assert isinstance(owner.failure, SystemExit)
    assert isinstance(owner.cleanup_failure, RuntimeError)


def test_serve_stop_before_thread_entry_does_not_call_shutdown(authority, monkeypatch):
    from fulcra_media.webhook_owner import ServeLoop
    with owner_for(authority) as owner:
        server = make_server(host='127.0.0.1', port=0, state=State(), client=owner,
                             bearer_token=None)
        loop = ServeLoop(server, threading.Event())
        release, at_entry = threading.Event(), threading.Event()
        real_target = loop.thread._target
        def delayed():
            at_entry.set()
            assert release.wait(5)
            real_target()
        loop.thread._target = delayed
        monkeypatch.setattr(server, 'shutdown', lambda: pytest.fail('shutdown before entry'))
        try:
            loop.start()
            assert at_entry.wait(5)
            with ThreadPoolExecutor(1) as pool:
                closer = pool.submit(loop.close)
                until(lambda: loop._stop)
                release.set()
                closer.result(5)
            assert loop.done.is_set() and not loop.entered.is_set()
        finally:
            release.set()
            server.server_close()


def test_sqlite_kv_outbox_and_connection_close_stay_on_owner(authority, monkeypatch):
    import sqlite3
    operations, closed, sessions = [], [], []
    from fulcra_collect.standalone_v1 import StandaloneRecordSession
    real_session_close = StandaloneRecordSession.close
    def close_session(session):
        real_session_close(session)
        assert session._identity_http.is_closed
        if session.client._http is not None:
            assert session.client._http.is_closed
        sessions.append(threading.get_ident())
    monkeypatch.setattr(StandaloneRecordSession, 'close', close_session)
    for name in ('open', 'get_plugin_kv', 'set_plugin_kv', 'get_record_outbox', 'set_record_outbox'):
        real = getattr(db, name)
        def track(*args, real=real, name=name, **kw):
            operations.append((name, threading.get_ident()))
            return real(*args, **kw)
        monkeypatch.setattr(db, name, track)
    original_close = db.close_all
    def close():
        connections = list(getattr(db._tls, 'conns', {}).values())
        original_close()
        for conn in connections:
            with pytest.raises(sqlite3.ProgrammingError, match='closed'):
                conn.execute('SELECT 1')
        closed.append(threading.get_ident())
    monkeypatch.setattr(db, 'close_all', close)
    with owner_for(authority) as owner:
        assert owner.ingest_batch([sample()], State()).verified == 1
        identity = owner._thread.ident
    assert operations and {tid for _, tid in operations} == {identity}
    assert {'open', 'get_record_outbox', 'set_record_outbox'} <= {name for name, _ in operations}
    assert closed == sessions == [identity]


def test_loop_bookkeeping_fatal_settles_queued_call_without_rewriting_completed_result(authority, monkeypatch):
    from fulcra_media.webhook_owner import OwnerFailed
    entered, release = threading.Event(), threading.Event()
    with owner_for(authority) as owner, ThreadPoolExecutor(1) as pool:
        real_get = owner._queue.get
        armed = [True]
        def get(*args, **kw):
            if armed[0] and kw.get('block') is False:
                return real_get(*args, **kw)
            if armed[0]:
                armed[0] = False
                entered.set()
                assert release.wait(5)
                raise SystemExit('synthetic queue bookkeeping failure')
            return real_get(*args, **kw)
        # Owner is currently inside the original blocking get. This first
        # actual job completes before the next get reaches our failing seam.
        monkeypatch.setattr(owner._queue, 'get', get)
        result = owner.ingest_batch([sample()], State())
        assert entered.wait(5)
        later = pool.submit(owner.ingest_batch, [sample('later')], State())
        until(lambda: owner._queue.qsize() == 1)
        release.set()
        with pytest.raises(OwnerFailed):
            later.result(5)
        assert result.posted == result.verified == 1
    assert len(authority.posts) == 1


def test_http_auth_body_cap_routes_and_connection_close_need_no_writer(authority):
    from fulcra_media.webhook_receiver import MAX_BODY_BYTES
    with serving(authority) as (_, server, _):
        before = list(authority.threads)
        for route, expected in (('/missing', 404), ('/webhook', 405), ('/health', 200)):
            conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=3)
            conn.request('GET', route)
            response = conn.getresponse()
            assert response.status == expected
            assert response.getheader('Connection') == 'close'
            if expected == 405:
                assert response.getheader('Allow') == 'POST'
            response.read()
            conn.close()
        for token, expected in (('wrong', 401), ('synthetic-bearer', 413)):
            conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=3)
            conn.putrequest('POST', '/webhook')
            conn.putheader('Authorization', 'Bearer ' + token)
            conn.putheader('Content-Length', str(MAX_BODY_BYTES + 1))
            conn.putheader('Content-Type', 'application/json')
            conn.endheaders()  # deliberately send no body: each gate must precede reads
            response = conn.getresponse()
            assert response.status == expected
            assert response.getheader('Connection') == 'close'
            response.read()
            conn.close()
        assert request(server, jellyfin_payload(), path='/missing')[0] == 404
        assert authority.threads == before and authority.posts == []
        assert request(server, jellyfin_payload(), path='/webhook?token=synthetic-bearer', token='') == (
            200, {'posted': 1, 'skipped': 0})


def test_owner_close_before_start_never_opens_authority(authority, monkeypatch):
    from fulcra_media import media_config
    from fulcra_media.webhook_owner import MediaOwner, OwnerClosed
    opened = []
    original = media_config.configured_media
    @contextmanager
    def context(path):
        opened.append(threading.get_ident())
        with original(path) as run:
            yield run
    monkeypatch.setattr(media_config, 'configured_media', context)
    owner = MediaOwner(authority.root.parent / 'legacy', threading.Event())
    owner.close()
    try:
        with pytest.raises(OwnerClosed):
            owner.start()
        assert opened == []
    finally:
        owner.close()


def test_close_during_owner_startup_cannot_reopen_or_report_success(authority, monkeypatch):
    from fulcra_media import media_config
    from fulcra_media.webhook_owner import MediaOwner, OwnerClosed
    entered, release, stopped = threading.Event(), threading.Event(), threading.Event()
    original = media_config.configured_media
    @contextmanager
    def context(path):
        entered.set()
        assert release.wait(5)
        with original(path) as run:
            yield run
    monkeypatch.setattr(media_config, 'configured_media', context)
    owner = MediaOwner(authority.root.parent / 'legacy', threading.Event())
    real_stop = owner.stop_admission
    def stop():
        real_stop()
        stopped.set()
    monkeypatch.setattr(owner, 'stop_admission', stop)
    with ThreadPoolExecutor(2) as pool:
        starting = pool.submit(owner.start)
        assert entered.wait(5)
        closing = pool.submit(owner.close)
        try:
            assert stopped.wait(5)
            assert not closing.done()
            release.set()
            with pytest.raises(OwnerClosed):
                starting.result(5)
            closing.result(5)
            assert owner.done.is_set() and not owner._open
        finally:
            # RED cleanup: the old owner reopens after first stop; a second stop
            # after publication releases it, rather than leaking a hung thread.
            release.set()
            assert owner._ready.wait(5)
            owner.stop_admission()
            owner.close()
            closing.result(5)


def test_close_serializes_with_thread_start_before_returning(authority, monkeypatch):
    from fulcra_media.webhook_owner import MediaOwner, OwnerClosed
    owner = MediaOwner(authority.root.parent / 'legacy', threading.Event())
    at_start, release, close_called = threading.Event(), threading.Event(), threading.Event()
    real_start = owner._thread.start
    def delayed_start():
        at_start.set()
        assert release.wait(5)
        real_start()
    monkeypatch.setattr(owner._thread, 'start', delayed_start)
    with ThreadPoolExecutor(2) as pool:
        starting = pool.submit(owner.start)
        assert at_start.wait(5)
        def close():
            close_called.set()
            owner.close()
        closing = pool.submit(close)
        try:
            assert close_called.wait(5)
            # A bounded observation window proves close cannot escape while an
            # already-requested thread start is still behind its test barrier.
            from concurrent.futures import TimeoutError
            with pytest.raises(TimeoutError):
                closing.result(0.05)
            release.set()
            try:
                starting.result(5)
            except OwnerClosed:
                pass
            closing.result(5)
            assert owner.done.is_set() and not owner._thread.is_alive()
        finally:
            release.set()
            if owner._thread.ident is not None:
                assert owner._ready.wait(5)
            else:
                # Let the submitted start finish entering Thread.start first.
                try:
                    starting.result(5)
                except OwnerClosed:
                    pass
            owner.stop_admission()
            owner.close()
            closing.result(5)


@pytest.mark.parametrize('phase', ['before-launch', 'after-loop-entry', 'before-loop-entry'])
def test_exceptional_serve_start_closes_every_launched_thread(authority, monkeypatch, phase):
    from fulcra_media.webhook_owner import ServeLoop
    from concurrent.futures import TimeoutError
    with owner_for(authority) as owner:
        server = make_server(host='127.0.0.1', port=0, state=State(), client=owner,
                             bearer_token=None)
        loop = ServeLoop(server, threading.Event())
        real_start, real_target = loop.thread.start, loop.thread._target
        reached, release = threading.Event(), threading.Event()
        if phase == 'before-loop-entry':
            def delayed():
                reached.set()
                assert release.wait(5)
                real_target()
            loop.thread._target = delayed
        def exceptional_start():
            if phase != 'before-launch':
                real_start()
                assert (loop.entered if phase == 'after-loop-entry' else reached).wait(5)
            raise KeyboardInterrupt('synthetic exceptional Thread.start')
        monkeypatch.setattr(loop.thread, 'start', exceptional_start)
        if phase != 'after-loop-entry':
            monkeypatch.setattr(server, 'shutdown', lambda: pytest.fail('shutdown before real loop entry'))
        try:
            with pytest.raises(KeyboardInterrupt):
                loop.start()
            if phase == 'before-loop-entry':
                with ThreadPoolExecutor(1) as pool:
                    closing = pool.submit(loop.close)
                    try:
                        until(lambda: loop._stop)
                        with pytest.raises(TimeoutError):
                            closing.result(0.05)
                    finally:
                        release.set()
                        closing.result(5)
            else:
                loop.close()
            assert not loop.thread.is_alive()
            if phase == 'before-launch':
                assert loop.thread.ident is None and not loop.entered.is_set()
            else:
                assert loop.done.is_set()
        finally:
            # Explicit RED rescue: old close skips a launched thread. Release
            # the pre-entry barrier or stop the proven real loop before join.
            release.set()
            if loop.entered.is_set() and not loop.done.is_set():
                server.shutdown()
            if loop.thread.ident is not None:
                loop.thread.join(5)
                assert not loop.thread.is_alive()
            server.server_close()
