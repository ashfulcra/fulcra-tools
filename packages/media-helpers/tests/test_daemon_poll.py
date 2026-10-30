"""Offline actual daemon strict poll + configured Collect/native wire evidence."""
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import base64
import importlib
import json
import logging
import re

import httpx
import pytest

from fulcra_collect import config, credentials, db, state
from fulcra_collect.plugin import RunContext
from fulcra_collect.v1_session import CollectRecordSession
from fulcra_common.data_types import CreationJournal
from fulcra_common.record_exclusions import EXCLUSION_SPEC
from fulcra_media import collect_v1
from fulcra_media.v1 import media_spec

ACCOUNT = 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee'
DESTINATION = 'Event/11111111-2222-4333-8444-555555555555'
CONTROL = 'Event/aaaaaaaa-2222-4333-8444-555555555555'
ROUTES = ['lastfm', 'deezer', 'generic_rss', 'letterboxd', 'goodreads']


@pytest.fixture(params=ROUTES)
def daemon(request, tmp_path, monkeypatch):
    name = request.param
    plugin = importlib.import_module('fulcra_media.plugins.' + name)
    importer = importlib.import_module('fulcra_media.importers.' + name)
    category = {'lastfm': 'listened', 'deezer': 'listened', 'goodreads': 'read'}.get(name, 'watched')
    settings = {'v1_destination_ids': {category: DESTINATION}, **{
        'lastfm': {'username': 'synthetic'}, 'deezer': {}, 'letterboxd': {'username': '@synthetic'},
        'goodreads': {'user_id': '123-synthetic'},
        'generic_rss': {'feed_url': 'https://feed.invalid/rss', 'service': 'synthetic', 'category': category},
    }[name]}
    monkeypatch.setenv('FULCRA_COLLECT_HOME', str(tmp_path))
    monkeypatch.setattr(httpx.HTTPTransport, 'handle_request', lambda *_: pytest.fail('external HTTP'))
    cfg = config.load()
    cfg.enable(plugin.PLUGIN.id)
    cfg.update_plugin_settings(plugin.PLUGIN.id, settings)
    config.save(cfg)
    CreationJournal(tmp_path / 'v1-type-creations.sqlite').resolved(ACCOUNT, EXCLUSION_SPEC, CONTROL)
    claims = base64.urlsafe_b64encode(json.dumps({'fulcradynamics.com/userid': ACCOUNT, 'exp': 4102444800}).encode()).decode().rstrip('=')
    token = 'synthetic.' + claims + '.signature'
    monkeypatch.setattr(credentials, 'get_user_secret_fresh', lambda _: token)
    info = {'mode': 'ok', 'secret': 'SECRET', 'requests': [], 'posts': [], 'rows': {}, 'events': [], 'after_post': lambda: None}
    monkeypatch.setattr(credentials, 'get_secret', lambda *_: info['secret'])
    root = Path(__file__).parent / 'fixtures'
    def source(req):
        info['requests'].append(req)
        if info['mode'] == 'fail':
            return httpx.Response(503)
        if info['mode'] == 'redirect':
            return httpx.Response(302, headers={'location': 'https://hostile.invalid/'})
        if name in ('lastfm', 'deezer'):
            second = req.url.params.get('page') == '2' if name == 'lastfm' else req.url.params.get('index', '0') != '0'
            if second and info['mode'] == 'page-two-fail':
                return httpx.Response(503)
            file = f'lastfm_recent_tracks_page{2 if second else 1}.json' if name == 'lastfm' else f'deezer_history_page{2 if second else 1}.json'
            payload = json.loads((root / file).read_text())
            if info['mode'] == 'empty':
                payload = {'recenttracks': {'track': [], '@attr': {'totalPages': '1'}}} if name == 'lastfm' else {'data': []}
            if name == 'deezer' and not second and info['mode'] == 'hostile':
                payload['next'] = 'https://hostile.invalid/?access_token=SECRET&index=4'
            if name == 'lastfm' and info['mode'] == 'metadata':
                payload['recenttracks'].pop('@attr', None)
            if name == 'deezer' and not second and info['mode'] == 'mixed':
                payload['data'][0]['timestamp'] = 1
            payload = info.get('payload', payload)
            info.get('mutate_payload', lambda _payload, _second: None)(payload, second)
            content = json.dumps(payload).encode()
        else:
            file = {'generic_rss': 'generic_atom_sample.xml', 'letterboxd': 'letterboxd_sample.xml', 'goodreads': 'goodreads_sample.xml'}[name]
            content = (root / file).read_bytes()
            if name == 'generic_rss':
                text = content.decode()
                content = (text[:text.rfind('  <entry>')] + '</feed>').encode()
            content = info.get('feed', content)
        info.get('after_source', lambda: None)()
        return httpx.Response(200, stream=httpx.ByteStream(content))
    method = {'lastfm': 'fetch_recent_tracks', 'deezer': 'fetch_history', 'generic_rss': 'normalize_feed', 'letterboxd': 'fetch_diary', 'goodreads': 'fetch_diary'}[name]
    original = getattr(importer, method)
    def forwarded(*a, **kw):
        kw['transport'] = httpx.MockTransport(source)
        if name in ('lastfm', 'deezer'):
            kw['sleep_between_pages'] = 0
        info['strict'] = kw.get('strict_scan')
        return original(*a, **kw)
    monkeypatch.setattr(importer, method, forwarded)
    if name == 'lastfm':
        monkeypatch.setattr(plugin, method, forwarded)
    def native(req):
        path = req.url.path
        if path == '/input/v1/data_type':
            return httpx.Response(200, json=[{'id': full, 'fulcra_userid': ACCOUNT, 'name': spec.name,
                'api_version': 'v1', 'recordable': True, 'deprecated': False, 'record_spec': {'type': 'event'}}
                for full, spec in [(DESTINATION, media_spec(category)), (CONTROL, EXCLUSION_SPEC)]])
        if path.endswith('/schema'):
            spec = EXCLUSION_SPEC if CONTROL in path else media_spec(category)
            return httpx.Response(200, json={'type': 'object', 'properties': {**{k: {} for k in ('id', 'sources', 'tags', 'start_time', 'end_time')}, **spec.fields}, 'required': list(spec.required_fields)})
        if '/tag/name/' in path or '/tag/id/' in path:
            return httpx.Response(200, json={'id': 'aaaaaaaa-bbbb-4ccc-8ddd-123456789abc', 'name': 'synthetic' if name == 'generic_rss' else name, 'fulcra_userid': ACCOUNT})
        if path == '/data/v1/records':
            ids = re.findall(r'id="([^"]+)"', req.url.params['q'])
            return httpx.Response(200, text='\n'.join(json.dumps(info['rows'][i]) for i in ids if i in info['rows']))
        if path == '/ingest/v1/record/' + DESTINATION:
            batch = [json.loads(line) for line in req.content.splitlines()]
            info['posts'].extend(batch)
            if info['mode'] != 'unknown':
                info['rows'].update({r['id']: r for r in batch})
            info['after_post']()
            return httpx.Response(201, json={'upload_id': 'synthetic'})
        pytest.fail('unexpected native path ' + path)
    monkeypatch.setattr(collect_v1, 'CollectRecordSession', lambda ctx: CollectRecordSession(ctx, transport=httpx.MockTransport(native)))
    original_import = collect_v1.MediaRecordWriter.run_import
    def observed(writer, events, **kw):
        info['events'] = list(events)
        return original_import(writer, events, **kw)
    monkeypatch.setattr(collect_v1.MediaRecordWriter, 'run_import', observed)
    kv = {}
    emitted = []
    ctx = RunContext(plugin_id=plugin.PLUGIN.id, config=deepcopy(settings), config_epoch=cfg.plugin_epochs[plugin.PLUGIN.id],
        credentials={'api-key': 'SECRET', 'access-token': 'SECRET'}, state=state.PluginState(plugin.PLUGIN.id),
        log=logging.getLogger('synthetic'), _emit=emitted.append)
    ctx.state.watermark = '2020-01-01T00:00:00Z'
    monkeypatch.setattr(ctx, 'kv_get', lambda k, default=None: deepcopy(kv[k]) if k in kv else default)
    monkeypatch.setattr(ctx, 'kv_set', lambda k, v: kv.__setitem__(k, deepcopy(v)))
    ctx._record_outbox_get = lambda k, d: db.get_record_outbox(db.open(), plugin.PLUGIN.id, k, d)
    ctx._record_outbox_set = lambda k, v: db.set_record_outbox(db.open(), plugin.PLUGIN.id, k, v)
    def invoke():
        return plugin.PLUGIN.run(ctx)
    return SimpleNamespace(name=name, plugin=plugin, ctx=ctx, kv=kv, info=info, emitted=emitted, invoke=invoke, settings=settings)


def test_daemon_poll_all_five_select_strict_and_preserve_source_ids(daemon):
    daemon.invoke()
    assert daemon.info['strict'] is True
    assert daemon.info['posts']
    from fulcra_media.v1 import build_media_record
    assert all(r['importer'] == daemon.plugin.PLUGIN.id for r in daemon.info['posts'])
    assert {r['id'] for r in daemon.info['posts']} == {build_media_record(e, DESTINATION)['id'] for e in daemon.info['events']}
    if daemon.name == 'lastfm':
        assert daemon.info['requests'][0].url.params['user'] == 'synthetic'
        assert daemon.info['requests'][0].url.params['api_key'] == 'SECRET'
    if daemon.name == 'deezer':
        assert [r.url.params['index'] for r in daemon.info['requests']] == ['0', '4']
    assert daemon.ctx.state.watermark == '2020-01-01T00:00:00Z'
    assert any(k.startswith('v2_daemon_media_poll_cursor:') for k in daemon.kv)


def cursors(daemon):
    return {k: deepcopy(v) for k, v in daemon.kv.items() if k.startswith('v2_daemon_media_poll_cursor:')}


def cursor_status(daemon):
    return [e for e in daemon.emitted if e.get('stage') == 'cursor'][-1]


def save_settings(daemon, **changes):
    daemon.ctx.config.update(changes)
    cfg = config.load()
    cfg.plugin_settings[daemon.plugin.PLUGIN.id] = deepcopy(daemon.ctx.config)
    config.save(cfg)
    daemon.ctx.config_epoch = config.load().plugin_epochs[daemon.plugin.PLUGIN.id]


def test_daemon_poll_new_scope_no_adoption_replay_and_no_increase(daemon):
    old = {'v1_media_cursor:old': {'watermark': '2099-01-01T00:00:00Z', 'untouched': True}, 'annotation_cursor': '2099'}
    daemon.kv.update(deepcopy(old))
    daemon.invoke()
    first = cursors(daemon)
    assert len(first) == 1
    assert all(daemon.kv[k] == v for k, v in old.items())
    if daemon.name == 'lastfm':
        assert 'from' not in daemon.info['requests'][0].url.params
    count = len(daemon.info['posts'])
    daemon.info['requests'].clear()
    daemon.invoke()
    assert len(daemon.info['posts']) == count
    assert cursors(daemon) == first
    assert cursor_status(daemon)['cursor_reason'] == 'no-increase'
    if daemon.name == 'lastfm':
        prior = datetime.fromisoformat(next(iter(first.values()))['watermark'])
        assert int(daemon.info['requests'][0].url.params['from']) == int(prior.timestamp()) - 86400
    assert daemon.ctx.state.watermark == '2020-01-01T00:00:00Z'


@pytest.mark.parametrize('mode', ['fail', 'redirect', 'page-two-fail', 'metadata', 'hostile'])
def test_daemon_materializes_before_any_current_delivery(daemon, mode):
    if mode in ('page-two-fail',) and daemon.name not in ('lastfm', 'deezer'):
        pytest.skip('API pagination only')
    if mode == 'metadata' and daemon.name != 'lastfm' or mode == 'hostile' and daemon.name != 'deezer':
        pytest.skip('route-specific validation')
    daemon.info['mode'] = mode
    with pytest.raises(RuntimeError, match='source scan failed'):
        daemon.invoke()
    assert not daemon.info['posts'] and not cursors(daemon)
    assert all(r.url.host != 'hostile.invalid' for r in daemon.info['requests'])
    assert daemon.ctx.state.watermark == '2020-01-01T00:00:00Z'
    assert cursor_status(daemon)['cursor_state'] == 'unknown'


@pytest.mark.parametrize('offset', [-1, 0, 1])
def test_daemon_poll_caps_are_never_completeness(daemon, offset):
    daemon.invoke()
    count = len(daemon.info['events'])
    cap = max(1, count + offset)
    save_settings(daemon, max_entries=cap)
    for key in cursors(daemon):
        del daemon.kv[key]
    daemon.invoke()
    assert daemon.info['posts'] and not cursors(daemon)
    assert cursor_status(daemon)['cursor_reason'] == 'entry-cap'
    if daemon.name not in ('lastfm', 'deezer'):
        starts = [event.start_time for event in daemon.info['events']]
        assert starts == sorted(starts)
        assert len(starts) <= cap


@pytest.mark.parametrize('cap', [True, False, 0, -1, '2', 1.5])
@pytest.mark.parametrize('field', ['max_entries', 'max_pages'])
def test_daemon_invalid_caps_rejected_before_source(daemon, cap, field):
    save_settings(daemon, **{field: cap})
    with pytest.raises(ValueError, match='positive integer'):
        daemon.invoke()
    assert not daemon.info['requests'] and not daemon.info['posts'] and not cursors(daemon)
    assert daemon.ctx.state.watermark == '2020-01-01T00:00:00Z'


def test_daemon_page_cap_coinciding_with_terminal(daemon):
    if daemon.name not in ('lastfm', 'deezer'):
        pytest.skip('API page cap')
    save_settings(daemon, max_pages=2)
    daemon.invoke()
    assert len(daemon.info['requests']) == 2 and daemon.info['posts']
    assert not cursors(daemon) and cursor_status(daemon)['cursor_reason'] == 'page-cap'


@pytest.mark.parametrize('mode', ['changed', 'deleted', 'unreadable', 'settings', 'disabled', 'account'])
def test_daemon_poll_source_fence_after_keychain(daemon, monkeypatch, mode):
    if daemon.name not in ('lastfm', 'deezer'):
        pytest.skip('consumed API secrets only')
    def fresh(plugin, key):
        assert plugin == daemon.plugin.PLUGIN.id
        assert key == ('api-key' if daemon.name == 'lastfm' else 'access-token')
        assert daemon.info['posts']  # canonical fresh sampling after actual delivery
        if mode == 'changed':
            return 'ROTATED'
        if mode == 'deleted':
            return None
        if mode == 'unreadable':
            raise TimeoutError('SECRET')
        cfg = config.load()
        if mode == 'settings':
            cfg.plugin_settings[plugin]['changed_during_keychain'] = True
        elif mode == 'disabled':
            cfg.enabled.discard(plugin)
        else:
            monkeypatch.setattr(credentials, 'get_user_secret_fresh', lambda _: None)
        monkeypatch.setattr(config, 'load', lambda: cfg)
        return 'SECRET'
    monkeypatch.setattr(credentials, 'get_secret', fresh)
    if mode in ('settings', 'disabled', 'account'):
        with pytest.raises(RuntimeError, match='cursor state unavailable'):
            daemon.invoke()
        assert cursor_status(daemon)['cursor_state'] == 'unknown'
    else:
        daemon.invoke()
        assert cursor_status(daemon)['cursor_state'] == 'withheld'
    assert daemon.info['posts'] and not cursors(daemon)
    assert any(e.get('stage') == 'imported' and e.get('posted', 0) > 0 for e in daemon.emitted)
    assert 'SECRET' not in json.dumps(daemon.emitted)


@pytest.mark.parametrize('mode', ['before-write', 'write-unknown', 'readback-fail', 'mismatch'])
def test_daemon_poll_cursor_ack_unknown(daemon, monkeypatch, mode):
    original_set, original_get = daemon.ctx.kv_set, daemon.ctx.kv_get
    pending = {}
    def set_value(k, v):
        if not k.startswith('v2_daemon_media_poll_cursor:'):
            return original_set(k, v)
        pending[k] = deepcopy(v)
        if mode != 'before-write':
            original_set(k, v)
        if mode in ('before-write', 'write-unknown'):
            raise RuntimeError('unknown acknowledgement')
    def get_value(k, default=None):
        if k in pending and mode == 'readback-fail':
            raise RuntimeError('readback failed')
        if k in pending and mode == 'mismatch':
            return {**pending[k], 'unknown': True}
        return original_get(k, default)
    monkeypatch.setattr(daemon.ctx, 'kv_set', set_value)
    monkeypatch.setattr(daemon.ctx, 'kv_get', get_value)
    if mode == 'write-unknown':
        daemon.invoke()
        assert cursor_status(daemon)['cursor_state'] == 'advanced'  # exact authoritative read establishes value
    else:
        with pytest.raises(RuntimeError, match='cursor state unavailable'):
            daemon.invoke()
        assert cursor_status(daemon)['cursor_state'] == 'unknown'
    assert daemon.info['posts']
    monkeypatch.setattr(daemon.ctx, 'kv_set', original_set)
    monkeypatch.setattr(daemon.ctx, 'kv_get', original_get)
    daemon.invoke()  # eventual exact read can recover; no additional source POST
    assert len(cursors(daemon)) == 1


@pytest.mark.parametrize('bad', ['extra', 'bool-version', 'naive', 'oversized', 'unknown-version', 'extra-scope', 'null'])
def test_daemon_closed_envelope_fail_loud_preserved(daemon, bad):
    daemon.invoke()
    key, envelope = next(iter(cursors(daemon).items()))
    if bad == 'extra':
        envelope['extra'] = 1
    elif bad == 'bool-version':
        envelope['version'] = True
    elif bad == 'unknown-version':
        envelope['version'] = 999
    elif bad == 'extra-scope':
        envelope['scope']['extra'] = 1
    elif bad == 'naive':
        envelope['watermark'] = '2026-01-01T00:00:00'
    elif bad == 'oversized':
        envelope['watermark'] = 'x' * 4097
    else:
        envelope = None
    daemon.kv[key] = deepcopy(envelope)
    daemon.info['requests'].clear()
    with pytest.raises(RuntimeError, match='cursor state unavailable'):
        daemon.invoke()
    assert daemon.kv[key] == envelope and not daemon.info['requests']
    assert daemon.ctx.state.watermark == '2020-01-01T00:00:00Z'


def test_daemon_poll_preview_never_drains_or_commits(daemon, monkeypatch):
    save_settings(daemon, preview_only=True)
    monkeypatch.setattr(collect_v1.MediaRecordWriter, 'resume', lambda _: pytest.fail('preview drained pending'))
    daemon.invoke()
    assert not daemon.info['posts'] and not cursors(daemon)
    assert cursor_status(daemon)['cursor_reason'] == 'preview'
    assert not any(e.get('stage') == 'imported' for e in daemon.emitted)


def test_daemon_poll_recovery_precedes_source_and_restores_worker_state(daemon):
    daemon.info['mode'] = 'unknown'
    with pytest.raises(RuntimeError):
        daemon.invoke()
    assert daemon.info['posts'] and not cursors(daemon)
    daemon.info['requests'].clear()
    # First invocation's rows become visible. Recovery accounts for them without
    # another POST, before a new source can run or its credential is validated.
    daemon.info['rows'].update({r['id']: deepcopy(r) for r in daemon.info['posts']})
    daemon.info['mode'] = 'fail'
    if daemon.name in ('lastfm', 'deezer'):
        daemon.ctx.credentials = {}
    with pytest.raises(RuntimeError):
        daemon.invoke()
    from fulcra_collect import db
    pending = db.get_record_outbox(db.open(), daemon.plugin.PLUGIN.id, 'v1_media_pending')
    assert not pending['groups'] and not cursors(daemon)
    assert daemon.ctx.state.watermark == '2020-01-01T00:00:00Z'


def test_daemon_feed_returned_snapshot_only_and_malformed_hold(daemon):
    if daemon.name in ('lastfm', 'deezer'):
        pytest.skip('feeds')
    daemon.info['feed'] = b'<html><body>not a feed</body></html>'
    with pytest.raises(RuntimeError, match='source scan failed'):
        daemon.invoke()
    assert not daemon.info['posts']
    daemon.info['feed'] = b'''<?xml version="1.0"?><rss version="2.0"><channel><title>Synthetic</title>
    <item><title>Film</title><link>https://example.invalid/film</link><pubDate>Fri, 02 Jan 2026 12:00:00 GMT</pubDate></item>
    <item><title>Malformed</title></item></channel></rss>'''
    daemon.invoke()
    assert daemon.info['posts'] and not cursors(daemon)
    assert cursor_status(daemon)['cursor_reason'] == 'normalization-skips'


def test_daemon_empty_source_never_invents_watermark(daemon):
    daemon.info['mode'] = 'empty'
    if daemon.name not in ('lastfm', 'deezer'):
        daemon.info['feed'] = b'<?xml version="1.0"?><rss version="2.0"><channel><title>Empty</title></channel></rss>'
    daemon.invoke()
    assert not daemon.info['posts'] and not cursors(daemon)
    assert cursor_status(daemon)['cursor_reason'] == 'no-events'


def test_daemon_other_callers_keep_original_lifecycle():
    from fulcra_media.collect_v1 import media_run
    for name in ('netflix', 'spotify_extended', 'youtube', 'apple_takeout', 'apple_music_takeout',
                 'apple_podcasts', 'apple_podcasts_timemachine', 'apple_tv', 'generic_csv', 'media_webhook', 'spotify_ifttt'):
        plugin = importlib.import_module('fulcra_media.plugins.' + name).PLUGIN
        assert plugin.run.__code__ is media_run(lambda _: None).__code__


@pytest.mark.parametrize('field,value', [('total', True), ('total', -1), ('total', 999), ('posted', 0), ('verified', 0), ('skipped_existing', 99)])
def test_daemon_poll_current_result_only(daemon, monkeypatch, field, value):
    from dataclasses import replace
    original = collect_v1.MediaRecordWriter.run_import
    def inconsistent(writer, events, **kwargs):
        actual = original(writer, events, **kwargs)
        return replace(actual, **{field: value})  # negative seam after real verified delivery
    monkeypatch.setattr(collect_v1.MediaRecordWriter, 'run_import', inconsistent)
    with pytest.raises(RuntimeError, match='selected coverage unavailable'):
        daemon.invoke()
    assert daemon.info['posts'] and not cursors(daemon)
    assert cursor_status(daemon)['cursor_reason'] == 'current-result-accounting'


def test_daemon_duplicate_only_positive_coverage_can_advance(daemon):
    daemon.invoke()
    count = len(daemon.info['posts'])
    for key in cursors(daemon):
        del daemon.kv[key]
    daemon.invoke()
    assert len(daemon.info['posts']) == count
    assert len(cursors(daemon)) == 1
    assert cursor_status(daemon)['cursor_state'] == 'advanced'
    assert any(e.get('stage') == 'imported' and e.get('skipped', 0) > 0 for e in daemon.emitted)


def test_daemon_changed_source_starts_fresh_scope(daemon):
    daemon.invoke()
    original = cursors(daemon)
    if daemon.name in ('lastfm', 'deezer'):
        field = 'api-key' if daemon.name == 'lastfm' else 'access-token'
        daemon.ctx.credentials[field] = 'ROTATED'
        daemon.info['secret'] = 'ROTATED'
    elif daemon.name == 'generic_rss':
        save_settings(daemon, feed_url='https://new.invalid/rss', service='synthetic')
    elif daemon.name == 'letterboxd':
        save_settings(daemon, username='changed')
    else:
        save_settings(daemon, user_id='456')
    daemon.info['requests'].clear()
    daemon.invoke()
    assert len(cursors(daemon)) == 2
    assert all(daemon.kv[k] == v for k, v in original.items())
    if daemon.name == 'lastfm':
        assert 'from' not in daemon.info['requests'][0].url.params


def test_daemon_goodreads_invalid_nonempty_read_at_fallback_holds(daemon):
    if daemon.name != 'goodreads':
        pytest.skip('Goodreads fallback')
    raw = (Path(__file__).parent / 'fixtures' / 'goodreads_sample.xml').read_bytes()
    daemon.info['feed'] = re.sub(rb'<user_read_at>.*?</user_read_at>', b'<user_read_at>invalid</user_read_at>', raw, flags=re.S)
    daemon.invoke()
    assert daemon.info['posts'] and not cursors(daemon)
    assert cursor_status(daemon)['cursor_reason'] == 'normalization-skips'


def test_daemon_enrichment_exception_delivers_no_current_batch(daemon, monkeypatch):
    if daemon.name != 'generic_rss':
        pytest.skip('generic feed enrichment')
    from fulcra_media.importers import generic_rss
    original = generic_rss.normalize_entry
    calls = []
    def normalize(*args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError('private source enrichment failed')
        return original(*args, **kwargs)
    monkeypatch.setattr(generic_rss, 'normalize_entry', normalize)
    with pytest.raises(RuntimeError, match='source scan failed'):
        daemon.invoke()
    assert not daemon.info['posts'] and not cursors(daemon)


def test_daemon_deezer_mixed_old_page_continues(daemon):
    if daemon.name != 'deezer':
        pytest.skip('Deezer continuation')
    daemon.invoke()
    key, prior = next(iter(cursors(daemon).items()))
    # Prior replay boundary is near the first page. An old row must not stop
    # traversal; second-page newer activity remains part of this current scan.
    prior['watermark'] = '2024-05-17T00:00:00+00:00'
    daemon.kv[key] = prior
    daemon.info['mode'] = 'mixed'
    daemon.info['requests'].clear()
    daemon.invoke()
    assert [r.url.params['index'] for r in daemon.info['requests']] == ['0', '4']
    assert daemon.info['events']
    assert all(e.start_time >= datetime(2024, 5, 16, tzinfo=timezone.utc) for e in daemon.info['events'])


def test_daemon_strict_initial_resource_bound_preserves_unknown(daemon, monkeypatch):
    from fulcra_media.importers import poll_scan
    monkeypatch.setattr(poll_scan, 'MAX_ROWS', 1)
    with pytest.raises(RuntimeError, match='source scan failed'):
        daemon.invoke()
    assert not daemon.info['posts'] and not cursors(daemon)
    assert cursor_status(daemon)['cursor_state'] == 'unknown'


@pytest.mark.parametrize('moment', ['fetch', 'delivery'])
def test_daemon_secret_rotation_during_source_or_delivery_holds(daemon, moment):
    if daemon.name not in ('lastfm', 'deezer'):
        pytest.skip('API consumed secrets')
    def rotate():
        daemon.info['secret'] = 'ROTATED'
    daemon.info['after_source' if moment == 'fetch' else 'after_post'] = rotate
    daemon.invoke()
    assert daemon.info['posts'] and not cursors(daemon)
    assert cursor_status(daemon)['cursor_state'] == 'withheld'


def test_daemon_feed_has_no_keychain_sweep(daemon, monkeypatch):
    if daemon.name in ('lastfm', 'deezer'):
        pytest.skip('feeds only')
    monkeypatch.setattr(credentials, 'get_secret', lambda *_: pytest.fail('feed read provider Keychain'))
    daemon.invoke()
    assert len(cursors(daemon)) == 1


def test_daemon_getter_blocked_then_second_guard_observes_revocation(daemon, monkeypatch):
    if daemon.name not in ('lastfm', 'deezer'):
        pytest.skip('API source fence')
    from threading import Event, Thread
    entered, revoked = Event(), Event()
    original_load = config.load
    changed = original_load()
    changed.plugin_settings[daemon.plugin.PLUGIN.id]['revoked_while_reading'] = True
    # Simulate an external authority view changing while a slow canonical read
    # is blocked. config.save is deliberately not called under commit's lease.
    def revoke():
        assert entered.wait(3)
        revoked.set()
    actor = Thread(target=revoke)
    actor.start()
    monkeypatch.setattr(config, 'load', lambda: changed if revoked.is_set() else original_load())
    def blocked(*_):
        assert daemon.info['posts']
        entered.set()
        assert revoked.wait(3)
        return 'SECRET'
    monkeypatch.setattr(credentials, 'get_secret', blocked)
    try:
        with pytest.raises(RuntimeError, match='cursor state unavailable'):
            daemon.invoke()
    finally:
        actor.join(3)
    assert not actor.is_alive() and not cursors(daemon)
    assert cursor_status(daemon)['cursor_state'] == 'unknown'


@pytest.mark.parametrize('next_url', [
    'http://api.deezer.com/user/me/history?index=4',
    'https://api.deezer.com/wrong?index=4',
    'https://api.deezer.com/user/me/history?index=4&access_token=OTHER',
    'https://api.deezer.com/user/me/history?index=4&index=5',
    'https://api.deezer.com/user/me/history?index=0',
    'https://api.deezer.com/user/me/history?index=4&unrecognized=1',
])
def test_daemon_deezer_continuation_authority_rejected_before_request(daemon, next_url):
    if daemon.name != 'deezer':
        pytest.skip('Deezer continuation authority')
    def mutate(payload, second):
        assert not second
        payload['next'] = next_url
    daemon.info['mutate_payload'] = mutate
    with pytest.raises(RuntimeError, match='source scan failed'):
        daemon.invoke()
    assert len(daemon.info['requests']) == 1
    assert not daemon.info['posts'] and not cursors(daemon)


@pytest.mark.parametrize('mode', ['changed-total', 'page-mismatch', 'missing-track'])
def test_daemon_lastfm_complete_metadata_required(daemon, mode):
    if daemon.name != 'lastfm':
        pytest.skip('Last.fm metadata authority')
    def mutate(payload, second):
        if mode == 'missing-track':
            payload['recenttracks'].pop('track')
        elif second:
            payload['recenttracks']['@attr']['totalPages' if mode == 'changed-total' else 'page'] = '3'
    daemon.info['mutate_payload'] = mutate
    with pytest.raises(RuntimeError, match='source scan failed'):
        daemon.invoke()
    assert not daemon.info['posts'] and not cursors(daemon)


def test_daemon_actual_config_roundtrip_positive_entry_cap(daemon):
    save_settings(daemon, max_entries=1)
    # Mirror worker.run_plugin's actual saved settings representation rather
    # than the plain dict used to configure the synthetic fixture.
    raw = config.load().plugin_settings[daemon.plugin.PLUGIN.id]
    from tomlkit.items import Integer
    assert type(raw['max_entries']) is Integer
    daemon.ctx.config = dict(raw)
    daemon.ctx._persisted_config = deepcopy(raw)
    before = config.settings_fingerprint(raw)
    daemon.invoke()
    assert not cursors(daemon)
    assert cursor_status(daemon)['cursor_reason'] == 'entry-cap'
    if daemon.name not in ('lastfm', 'deezer'):
        assert len(daemon.info['events']) == 1
    assert config.settings_fingerprint(daemon.ctx._persisted_config) == before
    assert type(daemon.ctx.config['max_entries']) is Integer


@pytest.mark.parametrize('literal', ['true', 'false', '0', '-1', '"2"', '1.5'])
def test_daemon_toml_cap_normalization_still_rejects_invalid(literal):
    import tomlkit
    from fulcra_media.daemon_poll import _cap
    with pytest.raises(ValueError, match='positive integer'):
        _cap(tomlkit.parse('cap=' + literal)['cap'])


def test_daemon_partial_writer_failure_retains_receipt_pending_and_no_cursor(daemon, monkeypatch):
    original = collect_v1.MediaRecordWriter.run_import
    def one_at_a_time(writer, events, **kwargs):
        return original(writer, events, chunk_size=1, **kwargs)
    monkeypatch.setattr(collect_v1.MediaRecordWriter, 'run_import', one_at_a_time)
    # The first native batch becomes visible, then the second has UNKNOWN
    # visibility. Exercise actual writer/outbox/SourceDispatch, no fake receipt.
    daemon.info['after_post'] = lambda: daemon.info.__setitem__('mode', 'unknown')
    with pytest.raises(RuntimeError, match='not yet visible'):
        daemon.invoke()
    assert len(daemon.info['posts']) == 2 and len(daemon.info['rows']) == 1
    assert any(e.get('type') == 'annotation' and e.get('ok') for e in daemon.emitted)
    assert not cursors(daemon)
    requests = len(daemon.info['requests'])
    with pytest.raises(RuntimeError, match='UNKNOWN'):
        daemon.invoke()
    assert len(daemon.info['requests']) == requests  # pending recovery precedes source
    assert len(daemon.info['posts']) == 2  # once-only source dispatch refuses replay
    assert daemon.ctx.state.watermark == '2020-01-01T00:00:00Z'
