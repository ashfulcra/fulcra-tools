"""Offline standalone poll CLI activation evidence."""
import json
import os

import pytest
from click.testing import CliRunner

from test_poll_cursors import configured as configured  # noqa: F401


ROUTES = [
    ('lastfm', []), ('deezer', []),
    ('generic-rss', ['https://feed.invalid/rss', '--service', 'synthetic', '--category', 'watched']),
    ('letterboxd', ['--username', 'synthetic']), ('goodreads', ['--user-id', '123']),
]


@pytest.mark.parametrize('name,args', ROUTES)
def test_args_before_authority(name, args, monkeypatch):
    from fulcra_media.cli import cli
    from fulcra_media import media_config
    monkeypatch.setattr(media_config, 'configured_media', lambda *a, **kw: pytest.fail('authority'))
    result = CliRunner().invoke(cli, ['import', name, *args, '--since', 'SECRET', '--json'])
    assert result.exit_code == 2
    data = json.loads(result.output)
    assert data['errors'] == [{'stage': 'args', 'message': 'poll arguments unavailable'}]
    assert data['cursor'] is None
    assert 'SECRET' not in result.output


@pytest.mark.parametrize('raw', [b'{"username":"u","api_key":"k","n":1e309}', b'x' * 65537,
    b'{"username":"u","username":"u","api_key":"k"}', b'{"username":"u","api_key":""}',
    b'{"username":"u","api_key":"\\ud800"}'])
def test_initial_credentials_fail_closed(tmp_path, raw):
    from fulcra_media.poll_cursors import read_source_credentials
    path = tmp_path / 'creds'
    path.write_bytes(raw)
    with pytest.raises(ValueError, match='^source credentials unavailable$'):
        read_source_credentials('lastfm', credential_path=path)


def test_initial_credentials_regular_bounded_and_detached(tmp_path):
    from fulcra_media.poll_cursors import read_source_credentials, credential_check
    path = tmp_path / 'creds'
    path.write_text('{"username":"u","api_key":"k","unused":1}')
    captured = read_source_credentials('lastfm', credential_path=path)
    assert captured == {'username': 'u', 'api_key': 'k'}
    check = credential_check(path, captured)
    assert check()
    path.write_text('{"username":"u","api_key":"k","n":1e309}')
    assert not check()
    path.unlink()
    os.mkfifo(path)
    with pytest.raises(ValueError):
        read_source_credentials('lastfm', credential_path=path)
    path.unlink()
    path.symlink_to(tmp_path / 'elsewhere')
    with pytest.raises(ValueError):
        read_source_credentials('lastfm', credential_path=path)


@pytest.fixture(params=ROUTES, ids=[r[0] for r in ROUTES])
def route(request, configured, monkeypatch):
    from pathlib import Path
    from types import SimpleNamespace
    import httpx
    from fulcra_media import poll_cli
    from fulcra_media.importers import lastfm, deezer, generic_rss, letterboxd, goodreads

    name, args = request.param
    args = list(args)
    root = Path(__file__).parent / 'fixtures'
    state = {'mode': 'ok', 'requests': [], 'events': [], 'scopes': [], 'deezer_responses': []}
    for module, values in [(lastfm, {'username': 'synthetic', 'api_key': 'SECRET'}),
                           (deezer, {'access_token': 'SECRET'})]:
        path = configured.tmp / (module.__name__.rsplit('.', 1)[-1] + '.json')
        path.write_text(json.dumps(values))
        monkeypatch.setattr(module, 'CREDS_PATH', path)
    def source(req):
        state['requests'].append(req)
        if state['mode'] == 'fail':
            raise RuntimeError('SECRET https://private.invalid/?unknown=SECRET')
        if state['mode'] == 'redirect':
            return httpx.Response(302, headers={'location': 'https://private.invalid/SECRET'})
        if name in ('lastfm', 'deezer'):
            if name == 'deezer':
                position = int(req.url.params['index'])
                assert position in (0, 4)
                assert req.url.params['index'] == str(position)
                assert req.url.params['limit'] == ('50' if position == 0 else '4')
                second = position == 4
            else:
                second = req.url.params.get('page') == '2'
            if second and state['mode'] == 'page-two-fail':
                if name == 'deezer':
                    state['deezer_responses'].append((int(req.url.params['index']), 503))
                return httpx.Response(503, text='SECRET')
            payload = json.loads((root / f'{name}_history_page{2 if second else 1}.json').read_text()) if name == 'deezer' else json.loads((root / f'lastfm_recent_tracks_page{2 if second else 1}.json').read_text())
            if name == 'deezer' and state['mode'] == 'mixed' and not second:
                payload['data'][0]['timestamp'] = 1
            if state['mode'] == 'empty':
                if name == 'lastfm':
                    payload['recenttracks']['track'] = []
                    payload['recenttracks']['@attr']['totalPages'] = '1'
                else:
                    payload = {'data': []}
            if name == 'deezer':
                state['deezer_responses'].append((int(req.url.params['index']), 200))
            return httpx.Response(200, stream=httpx.ByteStream(json.dumps(payload).encode()))
        file = {'generic-rss': 'generic_atom_sample.xml', 'letterboxd': 'letterboxd_sample.xml',
                'goodreads': 'goodreads_sample.xml'}[name]
        content = (root / file).read_text()
        if name == 'generic-rss' and state['mode'] != 'malformed':
            start = content.rfind('  <entry>')
            content = content[:start] + '</feed>'
        if state['mode'] == 'empty':
            content = '<?xml version="1.0"?><rss version="2.0"><channel><title>Empty</title></channel></rss>'
        content = state.get('feed_override', content)
        return httpx.Response(200, stream=httpx.ByteStream(content.encode()), headers={'content-type': 'application/xml'})
    transport = httpx.MockTransport(source)
    module, method = {'lastfm': (lastfm, 'fetch_recent_tracks'), 'deezer': (deezer, 'fetch_history'),
                      'generic-rss': (generic_rss, 'normalize_feed'), 'letterboxd': (letterboxd, 'fetch_diary'),
                      'goodreads': (goodreads, 'fetch_diary')}[name]
    actual = getattr(module, method)
    def forwarded(*a, **kw):
        kw['transport'] = transport
        if name in ('lastfm', 'deezer'):
            kw['sleep_between_pages'] = 0
        return actual(*a, **kw)
    monkeypatch.setattr(module, method, forwarded)
    monkeypatch.setattr(poll_cli, 'configured_media', lambda *a, **kw: configured.open(**kw))
    capture = poll_cli.capture_scope
    def captured_scope(*a, **kw):
        scope = capture(*a, **kw)
        state['scopes'].append(scope)
        return scope
    monkeypatch.setattr(poll_cli, 'capture_scope', captured_scope)
    scan = poll_cli._scan
    def captured(*a, **kw):
        events = scan(*a, **kw)
        state['events'] = events
        return events
    monkeypatch.setattr(poll_cli, '_scan', captured)
    def invoke(*extra):
        from fulcra_media.cli import cli
        result = CliRunner().invoke(cli, ['import', name, *args, *extra, '--json'])
        assert len(result.output.splitlines()) == 1, (result.output, result.exception)
        return result, json.loads(result.output)
    return SimpleNamespace(name=name, args=args, state=state, invoke=invoke, configured=configured,
                           creds=module.CREDS_PATH if name in ('lastfm', 'deezer') else None)


def test_actual_success_replay_preview_and_wire_parity(route):
    from fulcra_media.v1 import build_media_record
    result, data = route.invoke()
    assert result.exit_code == 0, data
    assert data['cursor'] == {'state': 'advanced', 'reason': 'complete'}
    assert data['posted'] == data['verified'] > 0
    assert data['importer'] == route.name
    assert data['new_watermark'] == max(e.start_time for e in route.state['events']).isoformat()
    assert 'SECRET' not in result.output
    if route.name == 'deezer':
        expected = _deezer_expected_events()
        assert ([int(req.url.params['index']) for req in route.state['requests']],
                route.state['events']) == ([0, 4], expected)
        assert route.state['deezer_responses'] == [(0, 200), (4, 200)]
        assert len(expected) == data['total'] == data['posted'] == data['verified'] == 7
    import hashlib
    scope = route.state['scopes'][-1]
    with route.configured.open() as run:
        persisted = run.ctx.kv_get(scope.key)
    assert persisted['scope'] == scope._value()
    assert persisted['watermark'] == data['new_watermark']
    assert persisted['scope']['importer'] == route.name
    assert persisted['scope']['category'] == route.state['events'][0].category
    assert persisted['scope']['service_digest'] == hashlib.sha256(route.state['events'][0].service.encode()).hexdigest()
    assert persisted['scope']['source_binding_digest'] == scope._binding.digest
    posts = route.configured.posts
    for post in posts:
        event = next(e for e in route.state['events'] if build_media_record(e, route.configured.settings['v1_destination_ids'][e.category])['id'] == post['id'])
        expected = build_media_record(event, route.configured.settings['v1_destination_ids'][event.category], tags=post['tags'])
        assert post == expected
    count = len(posts)
    result, replay = route.invoke()
    assert result.exit_code == 0, replay
    assert replay['posted'] == replay['verified'] == 0
    assert replay['skipped_existing'] > 0
    assert replay['cursor'] == {'state': 'unchanged', 'reason': 'no-increase'}
    assert replay['since_watermark'] is not None
    result, preview = route.invoke('--check-only')
    assert result.exit_code == 0, preview
    assert preview['cursor'] == {'state': 'withheld', 'reason': 'preview'}
    assert preview['verified'] == 0
    assert len(posts) == count
    requests = route.state['requests']
    if route.name == 'lastfm':
        assert requests[0].url.params['user'] == 'synthetic'
        assert requests[0].url.params['api_key'] == 'SECRET'
        assert any('from' in req.url.params for req in requests)
    elif route.name == 'deezer':
        assert requests[0].url.params['access_token'] == 'SECRET'
        assert any('index' in req.url.params for req in requests)
    elif route.name == 'letterboxd':
        assert requests[0].url.path == '/synthetic/rss/'
    elif route.name == 'goodreads':
        assert '/123' in requests[0].url.path


def test_actual_cap_even_natural_end_withholds(route):
    flag = '--max-pages' if route.name in ('lastfm', 'deezer') else '--max-entries'
    result, data = route.invoke(flag, '999')
    assert result.exit_code == 0, data
    assert data['posted'] == data['verified'] > 0
    assert data['cursor'] == {'state': 'withheld', 'reason': 'page-cap' if flag == '--max-pages' else 'entry-cap'}
    assert data['new_watermark'] is None


def test_actual_empty_no_watermark(route):
    route.state['mode'] = 'empty'
    result, data = route.invoke()
    assert result.exit_code == 0, data
    assert data['cursor'] == {'state': 'unchanged', 'reason': 'no-events'}
    assert data['new_watermark'] is None
    assert data['total'] == 0


@pytest.mark.parametrize(('route', 'mode'), [
    pytest.param(spec, mode, id=f'{spec[0]}-{mode}')
    for spec in ROUTES for mode in ('fail', 'redirect', 'page-two-fail')
    if mode != 'page-two-fail' or spec[0] in ('lastfm', 'deezer')
], indirect=['route'])
def test_actual_fetch_failure_before_delivery(route, mode):
    route.state['mode'] = mode
    result, data = route.invoke()
    assert result.exit_code == 2, data
    assert data['errors'] == [{'stage': 'fetch', 'message': 'source scan failed'}]
    assert data['total'] == data['posted'] == data['verified'] == 0
    assert data['cursor'] is None
    assert route.configured.posts == []
    assert 'SECRET' not in result.output and 'private.invalid' not in result.output
    if route.name == 'deezer' and mode == 'page-two-fail':
        assert [int(req.url.params['index']) for req in route.state['requests']] == [0, 4]
        assert route.state['deezer_responses'] == [(0, 200), (4, 503)]
        assert route.state['events'] == []
    if mode == 'redirect':
        assert len(route.state['requests']) == 1


def test_actual_cursor_kv_failure_retains_counts(route, monkeypatch):
    from fulcra_collect import plugin as runtime
    # The local writer completes before the cursor KV operation fails.
    actual = runtime.RunContext.kv_set
    def fail(self, key, value):
        if key.startswith('v1_cli_media_poll_cursor:'):
            raise RuntimeError('SECRET')
        return actual(self, key, value)
    monkeypatch.setattr(runtime.RunContext, 'kv_set', fail)
    result, data = route.invoke()
    assert result.exit_code == 2, data
    assert data['posted'] == data['verified'] == len(route.configured.posts) > 0
    assert data['cursor'] is data['new_watermark'] is None


def test_actual_committed_cursor_survives_cleanup_failure(route, monkeypatch):
    from contextlib import contextmanager
    from fulcra_media import poll_cli
    @contextmanager
    def failing(*a, **kw):
        with route.configured.open(**kw) as run:
            yield run
        raise RuntimeError('SECRET')
    monkeypatch.setattr(poll_cli, 'configured_media', failing)
    result, data = route.invoke()
    assert result.exit_code == 2, data
    assert data['posted'] == data['verified'] > 0
    assert data['cursor'] == {'state': 'advanced', 'reason': 'complete'}
    assert data['new_watermark']


def test_actual_unknown_write_then_failed_recovery_never_reads_source(route, monkeypatch):
    from fulcra_media import poll_cli
    route.configured.state['mode'] = 'unknown'
    result, data = route.invoke()
    assert result.exit_code == 2, data
    assert data['verified'] == 0 and data['cursor'] is None
    requests = len(route.state['requests'])
    monkeypatch.setattr(poll_cli, 'read_source_credentials', lambda *a: pytest.fail('credential acquisition before recovery'))
    result, data = route.invoke()
    assert result.exit_code == 2, data
    assert data['errors'][0]['stage'] == 'setup'
    assert data['total'] == data['posted'] == data['verified'] == 0
    assert len(route.state['requests']) == requests


def test_bounded_credentials_exact_limit_and_open_races(tmp_path, monkeypatch):
    from fulcra_media.poll_cursors import read_source_credentials
    path = tmp_path / 'creds'
    # Derive exact size independently of JSON whitespace/content.
    raw = b'{"access_token":"k"}'
    path.write_bytes(raw + b' ' * (65536 - len(raw)))
    assert read_source_credentials('deezer', credential_path=path) == {'access_token': 'k'}
    actual = os.open
    def raced(candidate, flags):
        path.unlink()
        os.mkfifo(path)
        return actual(candidate, flags)
    monkeypatch.setattr(os, 'open', raced)
    with pytest.raises(ValueError, match='^source credentials unavailable$'):
        read_source_credentials('deezer', credential_path=path)


def test_human_cursor_names_reason():
    from fulcra_media.cli_common import ImportEnvelope, emit_result
    import click
    @click.command()
    def command():
        emit_result(ImportEnvelope('generic-rss', True, cursor={'state': 'withheld', 'reason': 'page-cap'}), json_mode=False)
    result = CliRunner().invoke(command)
    assert 'cursor=withheld reason=page-cap' in result.output
    assert "{'" not in result.output


@pytest.mark.parametrize('empty', [False, True])
@pytest.mark.parametrize('route', ROUTES[:2], indirect=True, ids=['lastfm', 'deezer'])
def test_source_credentials_change_overrides_cap(route, monkeypatch, empty):
    from fulcra_media import poll_cli
    actual = poll_cli.decide_progress
    def changed(*a, **kw):
        decision = actual(*a, **kw)
        route.creds.write_text('{}')
        return decision
    monkeypatch.setattr(poll_cli, 'decide_progress', changed)
    route.state['mode'] = 'empty' if empty else 'ok'
    result, data = route.invoke(*([] if empty else ['--max-pages', '999']))
    assert result.exit_code == 0, data
    assert data['posted'] == data['verified']
    assert data['cursor'] == {'state': 'withheld', 'reason': 'source-changed'}


def test_explicit_since_no_overlap_and_corrupt_cursor_fails_before_source(route, monkeypatch):
    from fulcra_media import poll_cli
    actual = poll_cli.read_cursor
    scopes = []
    def observed(run, scope):
        scopes.append(scope)
        return actual(run, scope)
    monkeypatch.setattr(poll_cli, 'read_cursor', observed)
    result, first = route.invoke()
    assert result.exit_code == 0, first
    result, explicit = route.invoke('--since', first['new_watermark'])
    assert result.exit_code == 0, explicit
    assert explicit['since_watermark'] == first['new_watermark']
    with route.configured.open() as run:
        run.ctx.kv_set(scopes[-1].key, {'corrupt': True})
    before = len(route.state['requests'])
    result, failed = route.invoke('--since', '2020-01-01')
    assert result.exit_code == 2, failed
    assert failed['errors'][0]['stage'] == 'import'
    assert failed['cursor'] is None
    assert len(route.state['requests']) == before


def test_source_selection_isolation_and_old_explicit_no_regression(route):
    result, first = route.invoke()
    assert result.exit_code == 0, first
    result, older = route.invoke('--since', '2000-01-01')
    assert result.exit_code == 0, older
    assert older['new_watermark'] is None
    assert older['cursor'] == {'state': 'unchanged', 'reason': 'no-increase'}
    if route.creds:
        values = json.loads(route.creds.read_text())
        values['username' if route.name == 'lastfm' else 'access_token'] = 'other'
        route.creds.write_text(json.dumps(values))
    elif route.name == 'generic-rss':
        route.args[0] = 'https://feed.invalid/other'
    else:
        route.args[-1] = '456' if route.name == 'goodreads' else 'other'
    result, isolated = route.invoke()
    assert result.exit_code == 0, isolated
    assert isolated['since_watermark'] is None
    assert isolated['cursor'] == {'state': 'advanced', 'reason': 'complete'}
    if route.creds:
        assert isolated['posted'] == 0  # Actual ID dedup permits cursor adoption.


def test_recovered_a_then_b_source_failure_has_zero_current_counts(route):
    route.configured.state['mode'] = 'lost-reply'
    result, failed = route.invoke()
    assert result.exit_code == 2, failed
    assert failed['cursor'] is None
    route.configured.state['mode'] = 'ok'
    route.state['mode'] = 'fail'
    result, current = route.invoke()
    assert result.exit_code == 2, current
    assert current['errors'][0]['stage'] == 'fetch'
    assert current['total'] == current['posted'] == current['verified'] == 0
    assert current['cursor'] is None


@pytest.mark.parametrize('route', [ROUTES[2]], indirect=True, ids=['generic-rss'])
def test_malformed_outside_window_withholds_generic(route):
    route.state['mode'] = 'malformed'
    result, data = route.invoke('--since', '2100-01-01')
    assert result.exit_code == 0, data
    assert data['total'] == 0
    assert data['cursor'] == {'state': 'withheld', 'reason': 'normalization-skips'}


@pytest.mark.parametrize('flag,value', [('--since', ''), ('--max-pages', '0'), ('--max-pages', '10001'), ('--watermark-overlap-hours', '-1')])
def test_invalid_api_flags_before_source(flag, value, monkeypatch):
    from fulcra_media import poll_cli
    from fulcra_media.cli import cli
    monkeypatch.setattr(poll_cli, 'configured_media', lambda *a, **kw: pytest.fail('authority'))
    for name in ('lastfm', 'deezer'):
        result = CliRunner().invoke(cli, ['import', name, flag, value, '--json'])
        assert result.exit_code == 2
        assert json.loads(result.output)['errors'][0]['stage'] == 'args'


def test_first_actual_chunk_verified_then_unknown_preserves_lower_bound(route, monkeypatch):
    from fulcra_media.standalone_v1 import StandaloneMediaRun as MediaRun
    actual = MediaRun.import_events
    monkeypatch.setattr(MediaRun, 'import_events', lambda self, events: actual(self, events, chunk_size=1))
    route.configured.state['unknown_after'] = 1
    result, data = route.invoke()
    assert result.exit_code == 2, data
    assert data['total'] > 1
    assert data['posted'] == data['verified'] == 1
    assert data['cursor'] is data['new_watermark'] is None
    count = len(route.configured.posts)
    result, again = route.invoke()
    assert result.exit_code == 2, again
    assert again['posted'] == again['verified'] == 0
    assert len(route.configured.posts) == count


@pytest.mark.parametrize('route', ROUTES[:2], indirect=True, ids=['lastfm', 'deezer'])
def test_initial_credential_bounds_auth_before_source(route):
    route.creds.write_bytes(b'x' * 65537)
    result, data = route.invoke()
    assert result.exit_code == 2, data
    assert data['errors'] == [{'stage': 'auth', 'message': 'source credentials unavailable'}]
    assert data['cursor'] is None
    assert route.state['requests'] == []


def test_preview_after_actual_planning_failure_retains_would_post(route, monkeypatch):
    from fulcra_media import poll_cli
    result, first = route.invoke()
    assert result.exit_code == 0, first
    def fail(*a):
        raise RuntimeError('SECRET')
    monkeypatch.setattr(poll_cli, 'commit_progress', fail)
    result, data = route.invoke('--check-only')
    assert result.exit_code == 2, data
    assert data['total'] > 0
    assert data['would_post'] == data['posted']
    assert data['verified'] == 0
    assert data['cursor'] is data['new_watermark'] is None


def test_preview_pending_read_only_no_recovery_or_mutations(route):
    from fulcra_collect import db
    route.configured.state['mode'] = 'unknown'
    result, failed = route.invoke()
    assert result.exit_code == 2, failed
    conn = db.open(route.configured.profile / 'state.db')
    before = '\n'.join(conn.iterdump())
    posts = len(route.configured.posts)
    result, preview = route.invoke('--check-only')
    assert result.exit_code == 0, preview
    assert preview['cursor'] == {'state': 'withheld', 'reason': 'preview'}
    assert preview['would_post'] == preview['posted'] > 0
    assert preview['verified'] == 0
    assert len(route.configured.posts) == posts
    assert '\n'.join(conn.iterdump()) == before
    conn.close()


@pytest.mark.parametrize('route', [ROUTES[1]], indirect=True, ids=['deezer'])
def test_deezer_mixed_old_page_does_not_stop_later_eligible_page(route):
    from datetime import datetime, timezone
    route.state['mode'] = 'mixed'
    bound = datetime.fromtimestamp(1715896800, timezone.utc).isoformat()
    result, data = route.invoke('--since', bound)
    assert result.exit_code == 0, data
    expected = _deezer_expected_events(mixed=True, since=datetime.fromisoformat(bound))
    assert ([int(req.url.params['index']) for req in route.state['requests']],
            route.state['events']) == ([0, 4], expected)
    assert route.state['deezer_responses'] == [(0, 200), (4, 200)]
    assert [e.external_ids['deezer_track_id'] for e in expected] == [78901234, 4422111, 555111, 778899, 991122]
    assert len(expected) == data['total'] == data['posted'] == data['verified'] == 5
    assert [e.deterministic_id for e in route.state['events']] == [e.deterministic_id for e in expected]
    assert data['cursor'] == {'state': 'advanced', 'reason': 'complete'}


@pytest.mark.parametrize('name,args', [
    ('generic-rss', ['', '--service', 'synthetic', '--category', 'watched']),
    ('generic-rss', ['https://feed.invalid/rss', '--service', '', '--category', 'watched']),
    ('letterboxd', ['--username', 'SECRET/invalid']),
    ('goodreads', ['--user-id', 'SECRET']),
    ('letterboxd', ['--username', 'synthetic', '--max-entries', '0']),
    ('goodreads', ['--user-id', '123', '--max-entries', '-1']),
])
def test_invalid_rss_selection_args_before_authority(name, args, monkeypatch):
    from fulcra_media import poll_cli
    from fulcra_media.cli import cli
    monkeypatch.setattr(poll_cli, 'configured_media', lambda *a, **kw: pytest.fail('authority'))
    result = CliRunner().invoke(cli, ['import', name, *args, '--json'])
    assert result.exit_code == 2
    data = json.loads(result.output)
    assert data['errors'] == [{'stage': 'args', 'message': 'poll arguments unavailable'}]
    assert 'SECRET' not in result.output


@pytest.mark.parametrize('route', [ROUTES[2]], indirect=True, ids=['generic-rss'])
def test_generic_public_label_static_with_secret_service(route):
    route.args[2] = 'SECRET'
    result, data = route.invoke()
    assert result.exit_code == 0, data
    assert data['importer'] == 'generic-rss'
    assert 'SECRET' not in result.output
    assert data['cursor'] == {'state': 'advanced', 'reason': 'complete'}


@pytest.mark.parametrize('route', [ROUTES[0]], indirect=True, ids=['lastfm'])
def test_actual_cli_authority_change_after_scan_refuses_delivery(route, monkeypatch):
    from fulcra_media import poll_cli
    from fulcra_media.media_config import FILENAME
    actual = poll_cli._scan
    def changed(*a, **kw):
        events = actual(*a, **kw)
        path = route.configured.profile / FILENAME
        settings = json.loads(path.read_text())
        settings['account_id'] = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'
        path.write_text(json.dumps(settings))
        return events
    monkeypatch.setattr(poll_cli, '_scan', changed)
    result, data = route.invoke()
    assert result.exit_code == 2, data
    assert data['errors'][0]['stage'] == 'import'
    assert data['cursor'] is None
    assert data['posted'] == data['verified'] == 0
    assert route.configured.posts == []


def test_actual_cli_never_seeds_or_writes_legacy_watermarks(route):
    from fulcra_media.state import State, save
    path = route.configured.tmp / 'legacy'
    saved = State(watermarks={route.name: '2100-01-01T00:00:00+00:00',
                              'goodreads:123': '2100-01-01T00:00:00+00:00',
                              'generic-rss:https://feed.invalid/rss': '2100-01-01T00:00:00+00:00'})
    save(saved, path)
    before = path.read_bytes()
    result, data = route.invoke()
    assert result.exit_code == 0, data
    assert data['since_watermark'] is None
    assert data['posted'] == data['verified'] > 0
    assert data['cursor'] == {'state': 'advanced', 'reason': 'complete'}
    assert path.read_bytes() == before


def test_recovered_a_then_b_new_binding_has_no_adopted_cursor(route):
    route.configured.state['mode'] = 'lost-reply'
    result, failed = route.invoke()
    assert result.exit_code == 2, failed
    assert failed['cursor'] is None
    route.configured.state['mode'] = 'ok'
    if route.creds:
        values = json.loads(route.creds.read_text())
        values['username' if route.name == 'lastfm' else 'access_token'] = 'other'
        route.creds.write_text(json.dumps(values))
    elif route.name == 'generic-rss':
        route.args[0] = 'https://feed.invalid/other'
    else:
        route.args[-1] = '456' if route.name == 'goodreads' else 'other'
    result, data = route.invoke()
    assert result.exit_code == 0, data
    assert data['since_watermark'] is None
    assert data['cursor'] == {'state': 'advanced', 'reason': 'complete'}
    assert route.state['scopes'][0].key != route.state['scopes'][-1].key


def _deezer_expected_events(*, mixed=False, since=None):
    """Normalize every raw fixture row independently of the transport selector."""
    from pathlib import Path
    from fulcra_media.importers.deezer import normalize_track
    root = Path(__file__).parent / 'fixtures'
    rows = [row for page in (1, 2)
            for row in json.loads((root / f'deezer_history_page{page}.json').read_text())['data']]
    if mixed:
        rows[0]['timestamp'] = 1
    events = [normalize_track(row) for row in rows]
    assert all(event is not None for event in events)
    return [event for event in events if since is None or event.start_time >= since]


@pytest.mark.parametrize('route', [ROUTES[0]], indirect=True, ids=['lastfm'])
def test_valid_early_cursor_overlap_underflow_is_args_before_source(route):
    from datetime import datetime, timezone
    result, first = route.invoke()
    assert result.exit_code == 0, first
    scope = route.state['scopes'][-1]
    with route.configured.open() as run:
        run.ctx.kv_set(scope.key, {
            'version': 1, 'scope': scope._value(),
            'watermark': datetime.min.replace(tzinfo=timezone.utc).isoformat(),
        })
    before = len(route.state['requests'])
    result, data = route.invoke()
    assert result.exit_code == 2, data
    assert data['errors'] == [{'stage': 'args', 'message': 'poll arguments unavailable'}]
    assert data['cursor'] is data['new_watermark'] is None
    assert data['total'] == data['posted'] == data['verified'] == 0
    assert len(route.state['requests']) == before


@pytest.mark.parametrize('route', ROUTES[2:], indirect=True,
                         ids=['generic-rss', 'letterboxd', 'goodreads'])
def test_rss_entry_cap_selects_provider_order_after_since_filter(route, monkeypatch):
    from datetime import datetime, timezone
    from email.utils import format_datetime
    from fulcra_collect.plugin import RunContext
    from fulcra_media.standalone_v1 import StandaloneMediaRun
    from fulcra_media.v1 import build_media_record

    # Neither ascending nor descending sorting may pick the same two as the
    # eligible provider prefix. Capping the old first row before filtering also
    # yields a different count. All five rows must normalize before selection.
    titles = ['Out of window', 'First eligible', 'Second eligible',
              'Later provider low', 'Later provider high']
    days = [1, 20, 15, 10, 30]
    items = []
    for index, (title, day) in enumerate(zip(titles, days), 1):
        date = format_datetime(datetime(2026, 5, day, tzinfo=timezone.utc))
        items.append(f"""<item>
          <guid isPermaLink="false">entry-cap-{index}</guid>
          <title>{title}</title><link>https://fixture.invalid/items/{index}</link>
          <pubDate>{date}</pubDate><user_read_at>{date}</user_read_at>
          <book_id>{index}</book_id><author_name>Fixture Author</author_name>
          <letterboxd:filmTitle>{title}</letterboxd:filmTitle>
          <letterboxd:filmYear>2026</letterboxd:filmYear>
        </item>""")
    route.state['feed_override'] = (
        '<rss version="2.0" xmlns:letterboxd="https://letterboxd.com">'
        '<channel><title>Entry cap fixture</title>' + ''.join(items) + '</channel></rss>'
    )
    selected = []
    actual_import = StandaloneMediaRun.import_events
    def observed_import(self, events, **kwargs):
        selected.extend(events)
        return actual_import(self, events, **kwargs)
    monkeypatch.setattr(StandaloneMediaRun, 'import_events', observed_import)
    cursor_writes = []
    actual_set = RunContext.kv_set
    def observed_set(self, key, value):
        if key.startswith('v1_cli_media_poll_cursor:'):
            cursor_writes.append((key, value))
        return actual_set(self, key, value)
    monkeypatch.setattr(RunContext, 'kv_set', observed_set)

    result, data = route.invoke('--since', '2026-05-05T00:00:00Z', '--max-entries', '2')
    assert result.exit_code == 0, data
    all_events = route.state['events']
    assert [event.title for event in all_events] == titles
    assert [event.start_time.day for event in all_events] == days
    expected = all_events[1:3]  # Fixed provider-order eligible prefix, not sorted.
    assert selected == expected
    assert data['total'] == data['posted'] == data['verified'] == 2
    assert data['skipped_existing'] == 0
    assert data['cursor'] == {'state': 'withheld', 'reason': 'entry-cap'}
    assert data['new_watermark'] is None
    assert data['since_watermark'] == '2026-05-05T00:00:00+00:00'
    assert len(route.configured.posts) == 2
    destination = route.configured.settings['v1_destination_ids'][expected[0].category]
    expected_records = {
        build_media_record(event, destination)['id']: event for event in expected
    }
    assert {record['id'] for record in route.configured.posts} == set(expected_records)
    for record in route.configured.posts:
        assert record == build_media_record(
            expected_records[record['id']], destination, tags=record['tags'],
        )
    assert cursor_writes == []
    missing = object()
    with route.configured.open() as run:
        assert run.ctx.kv_get(route.state['scopes'][-1].key, missing) is missing
