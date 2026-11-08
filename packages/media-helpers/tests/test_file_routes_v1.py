"""Offline CLI integration: configured authority, real parsers and real writer."""
import base64
import json
import re
from pathlib import Path

import httpx
import pytest
from click.testing import CliRunner
from fulcra_common.client import BaseFulcraClient
from fulcra_media.cli import cli

ACCOUNT = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
FIXTURES = Path(__file__).parent / 'fixtures'
ROUTES = [
    ('spotify-extended', ['spotify_extended_sample.zip']),
    ('spotify-ifttt', ['spotify_ifttt_small.zip']),
    ('apple-podcasts', ['--db', 'apple_podcasts_mtlibrary.sqlite']),
    ('apple-podcasts-timemachine', []),
    ('apple-takeout', ['apple_takeout_playback_sample.csv']),
    ('youtube', ['youtube_watch_history_small.json']),
    ('generic-csv', ['generic_listened.csv', '--service', 'synthetic', '--category', 'listened']),
]

@pytest.fixture
def server(tmp_path, monkeypatch):
    from fulcra_common.data_types import CreationJournal
    from fulcra_common.record_exclusions import EXCLUSION_SPEC
    from fulcra_media import media_config
    from fulcra_media.v1 import media_spec
    profile = tmp_path / 'profile'
    profile.mkdir()
    claims = base64.urlsafe_b64encode(json.dumps({'fulcradynamics.com/userid': ACCOUNT, 'exp': 4102444800}).encode()).decode().rstrip('=')
    monkeypatch.setenv('FULCRA_COLLECT_HOME', str(profile))
    monkeypatch.setenv('FULCRA_ACCESS_TOKEN', 'synthetic.' + claims + '.signature')
    monkeypatch.setattr(httpx.HTTPTransport, 'handle_request', lambda *_: pytest.fail('external HTTP'))
    ids = {category: f'Event/{i}1111111-2222-4333-8444-555555555555' for i, category in enumerate(media_config.CATEGORIES, 1)}
    control = 'Event/aaaaaaaa-2222-4333-8444-555555555555'
    CreationJournal(profile / 'v1-type-creations.sqlite').resolved(ACCOUNT, EXCLUSION_SPEC, control)
    settings = {'version': 1, 'account_id': ACCOUNT, 'v1_destination_ids': ids, 'legacy_selection': dict.fromkeys(ids)}
    (profile / media_config.FILENAME).write_text(json.dumps(settings))
    rows, posts, requests, tags = {}, [], [], {}
    state = {'mode': 'ok', 'reading': True}
    def handle(request):
        path = request.url.path
        requests.append((request.method, path))
        if path == '/user/v1alpha1/info':
            return httpx.Response(200, json={'userid': ACCOUNT})
        if path == '/input/v1/data_type':
            return httpx.Response(200, json=[{'id': full, 'fulcra_userid': ACCOUNT, 'name': name, 'api_version': 'v1', 'recordable': True, 'deprecated': False, 'record_spec': {'type': 'event'}} for full, name in [*( (full, cat.title()) for cat, full in ids.items() if not state.get('missing_metadata')), (control, EXCLUSION_SPEC.name)]])
        if path.endswith('/schema'):
            spec = EXCLUSION_SPEC if control in path else media_spec(next(cat for cat, full in ids.items() if full in path))
            return httpx.Response(200, json={'type': 'object', 'properties': {**{k: {} for k in ('id', 'sources', 'tags', 'start_time', 'end_time')}, **spec.fields}, 'required': list(spec.required_fields)})
        if '/tag/name/' in path or '/tag/id/' in path:
            name = path.rsplit('/', 1)[1]
            if '/tag/id/' in path:
                name = next(n for n, tag in tags.items() if tag == name)
            tag = tags.setdefault(name, f'{len(tags)+1:08d}-1111-4111-8111-111111111111')
            return httpx.Response(200, json={'id': tag, 'name': name, 'fulcra_userid': ACCOUNT})
        if path == '/data/v1/records':
            if not state['reading']:
                return httpx.Response(503)
            query = request.url.params['q']
            full = re.search('__name__="([^"]+)"', query).group(1)
            wanted = re.findall('id="([^"]+)"', query)
            return httpx.Response(200, text='\n'.join(json.dumps(rows[full, i]) for i in wanted if (full, i) in rows))
        if path.startswith('/ingest/v1/record/'):
            full = path.removeprefix('/ingest/v1/record/')
            batch = [json.loads(line) for line in request.content.splitlines()]
            posts.append((full, batch))
            if state['mode'] == 'rejected':
                return httpx.Response(400, json={'error': 'synthetic rejection'})
            if state['mode'] != 'unknown':
                rows.update({(full, row['id']): row for row in batch})
            if state['mode'] != 'ok':
                raise httpx.ReadError('synthetic lost receipt')
            return httpx.Response(200, json={'upload_id': 'synthetic-upload'})
        pytest.fail('unexpected path ' + path)
    client = BaseFulcraClient(base_url='https://example.test', transport=httpx.MockTransport(handle))
    real_open = media_config.open_media
    monkeypatch.setattr(media_config, 'open_media', lambda settings, **kw: real_open(settings, **{**kw, 'client': client}))
    monkeypatch.setattr('fulcra_media.cli.STATE_PATH', tmp_path / 'legacy')
    monkeypatch.setattr('fulcra_media.importers.apple_podcasts.find_timemachine_snapshots', lambda: [FIXTURES / 'apple_podcasts_mtlibrary.sqlite'])
    return profile, ids, rows, posts, tags, state, requests


def args_for(route, args):
    return ['import', route, *[str(FIXTURES / arg) if (FIXTURES / arg).is_file() else arg for arg in args], '--json']

@pytest.mark.parametrize('route,args', ROUTES)
def test_pending_blocks_all_source_access(server, monkeypatch, route, args):
    from fulcra_collect import db
    profile, _, _, posts, _, _, _ = server
    conn = db.open(profile / 'state.db')
    db.set_record_outbox(conn, 'cli-media', 'v1_media_pending', {'groups': 'malformed', 'private': 'DO NOT PRINT'})
    before = conn.execute('SELECT value_json FROM record_outboxes').fetchone()[0]
    monkeypatch.setattr('fulcra_media.library.resolve', lambda *_: pytest.fail('source before recovery'))
    monkeypatch.setattr('fulcra_media.importers.apple_podcasts.parse_db', lambda *_: pytest.fail('DB before recovery'))
    monkeypatch.setattr('fulcra_media.importers.apple_podcasts.find_timemachine_snapshots', lambda: pytest.fail('discovery before recovery'))
    result = CliRunner().invoke(cli, args_for(route, args))
    assert result.exit_code == 2
    body = json.loads(result.output)
    assert not body['ok'] and body['errors'][0]['stage'] == 'import'
    assert 'DO NOT PRINT' not in result.output
    assert conn.execute('SELECT value_json FROM record_outboxes').fetchone()[0] == before
    assert posts == []


def parsed(route):
    from fulcra_media.importers import spotify, spotify_ifttt, apple_podcasts, apple_takeout, youtube, generic_csv
    from zoneinfo import ZoneInfo
    if route == 'spotify-extended':
        return list(spotify.parse_extended_zip(FIXTURES / 'spotify_extended_sample.zip'))
    if route == 'spotify-ifttt':
        return list(spotify_ifttt.parse_ifttt_zip(FIXTURES / 'spotify_ifttt_small.zip', tz=ZoneInfo('UTC')))
    if route.startswith('apple-podcasts'):
        return list(apple_podcasts.parse_db(FIXTURES / 'apple_podcasts_mtlibrary.sqlite'))
    if route == 'apple-takeout':
        return list(apple_takeout.parse_playback_csv(FIXTURES / 'apple_takeout_playback_sample.csv'))
    if route == 'youtube':
        return list(youtube.parse_takeout_json(FIXTURES / 'youtube_watch_history_small.json'))
    return list(generic_csv.parse_media_csv(FIXTURES / 'generic_listened.csv', service='synthetic', category='listened'))


@pytest.mark.parametrize('route,args', ROUTES)
def test_actual_fixture_payload_delivery_and_repeat(server, monkeypatch, route, args):
    from fulcra_media.v1 import build_media_record
    _, ids, rows, posts, tags, _, _ = server
    samples = parsed(route)
    assert samples
    if route == 'spotify-extended':
        assert len(samples) == 2
        assert {e.external_ids['kind'] for e in samples} == {'music', 'podcast'}
        assert {int((e.end_time - e.start_time).total_seconds()) for e in samples} == {210, 2400}
    if route == 'spotify-ifttt':
        assert len(samples) == 7  # Workbook overlap collapses exact plays only.
        assert all(e.timestamp_confidence == 'medium' for e in samples)
    if route == 'youtube':
        assert all(not e.extra_source_ids for e in samples)
        assert all((e.end_time - e.start_time).total_seconds() == 1 for e in samples)
    monkeypatch.setattr('fulcra_media.state.save', lambda *_: pytest.fail('legacy state save'))
    monkeypatch.setattr('fulcra_media.fulcra.FulcraClient.run_import', lambda *_ , **kw: pytest.fail('legacy writer'))
    monkeypatch.setattr('fulcra_media.twin_cache.load_for_twin_lookup', lambda *_: pytest.fail('legacy twin cache'))
    result = CliRunner().invoke(cli, args_for(route, args))
    assert result.exit_code == 0, result.output
    body = json.loads(result.output)
    assert body['ok'] and body['total'] == len(samples)
    assert body['posted'] == body['verified'] == len(samples)
    assert body['since_watermark'] is body['new_watermark'] is body['would_post'] is None
    expected = [build_media_record(e, ids[e.category], tags=(tags[e.service],)) for e in sorted(samples, key=lambda e: e.start_time)]
    assert [row for _, batch in posts for row in batch] == expected
    assert all(full == ids[row['category']] for full, batch in posts for row in batch)
    assert len(rows) == len(samples)
    count = len(posts)
    again = CliRunner().invoke(cli, args_for(route, args))
    assert again.exit_code == 0, again.output
    assert json.loads(again.output)['skipped_existing'] == len(samples)
    assert len(posts) == count


@pytest.mark.parametrize('route,args', ROUTES)
@pytest.mark.parametrize('missing_metadata', [False, True])
def test_preview_retains_pending_and_delivery_state(server, route, args, missing_metadata):
    from fulcra_collect import db
    profile, _, rows, posts, _, state, requests = server
    state['missing_metadata'] = missing_metadata
    conn = db.open(profile / 'state.db')
    db.set_record_outbox(conn, 'cli-media', 'v1_media_pending', {'groups': 'malformed', 'private': 'retained'})
    before = conn.execute('SELECT value_json FROM record_outboxes').fetchone()[0]
    config = (profile / 'cli-media-v1.json').read_bytes()
    result = CliRunner().invoke(cli, [*args_for(route, args), '--check-only'])
    body = json.loads(result.output)
    if missing_metadata:
        assert result.exit_code == 2 and not body['ok']
    else:
        assert result.exit_code == 0, result.output
        assert body['ok'] and body['would_post'] == len(parsed(route)) and body['verified'] == 0
    assert not posts and not rows
    assert all(method == 'GET' for method, _ in requests)
    assert conn.execute('SELECT value_json FROM record_outboxes').fetchone()[0] == before
    assert (profile / 'cli-media-v1.json').read_bytes() == config


@pytest.mark.parametrize('route,args', ROUTES)
@pytest.mark.parametrize('failure_stage', ['resolve', 'parse'])
def test_source_error_scrubbed_and_context_released(server, monkeypatch, route, args, failure_stage):
    from fulcra_media import media_config
    profile, _, _, posts, _, _, _ = server
    def fail(*_, **kw):
        raise RuntimeError('source https://example.test/?api_key=SECRET_PRIVATE&token=SECOND_PRIVATE')
    if route == 'apple-podcasts':
        monkeypatch.setattr('fulcra_media.importers.apple_podcasts.parse_db', fail)
    elif route == 'apple-podcasts-timemachine':
        target = 'parse_db' if failure_stage == 'parse' else 'find_timemachine_snapshots'
        monkeypatch.setattr('fulcra_media.importers.apple_podcasts.' + target, fail)
    elif failure_stage == 'parse':
        module, function = {
            'spotify-extended': ('spotify', 'parse_extended_zip'),
            'spotify-ifttt': ('spotify_ifttt', 'parse_ifttt_zip'),
            'apple-takeout': ('apple_takeout', 'parse_playback_csv'),
            'youtube': ('youtube', 'parse_takeout_json'),
            'generic-csv': ('generic_csv', 'parse_media_csv'),
        }[route]
        monkeypatch.setattr(f'fulcra_media.importers.{module}.{function}', fail)
    else:
        monkeypatch.setattr('fulcra_media.library.resolve', fail)
    result = CliRunner().invoke(cli, args_for(route, args))
    assert result.exit_code == 2
    assert len(result.output.splitlines()) == 1
    body = json.loads(result.output)
    assert body['importer'] == ('generic-csv:synthetic' if route == 'generic-csv' else route)
    assert not body['ok'] and 'api_key=REDACTED' in body['errors'][0]['message']
    assert 'SECRET_PRIVATE' not in result.output and 'SECOND_PRIVATE' not in result.output
    with media_config.configured_media(profile.parent / 'legacy'):
        pass
    assert not posts


@pytest.mark.parametrize('route,args', ROUTES)
def test_raw_nonexistent_source_and_authority_precedence(server, monkeypatch, tmp_path, route, args):
    from fulcra_media import media_config
    profile, _, _, posts, _, _, _ = server
    missing = str(tmp_path / 'genuinely-nonexistent')
    source_args = [] if route.endswith('timemachine') else (['--db', missing] if route == 'apple-podcasts' else [missing])
    if route == 'generic-csv':
        source_args += ['--service', 'synthetic', '--category', 'listened']
    monkeypatch.setattr('fulcra_media.importers.apple_podcasts.find_timemachine_snapshots', lambda: [])
    command = ['import', route, *source_args, '--json']
    result = CliRunner().invoke(cli, command)
    assert result.exit_code == 2
    assert json.loads(result.output)['errors'][0]['stage'] in ('args', 'fetch', 'snapshot')
    settings = json.loads((profile / media_config.FILENAME).read_text())
    settings['account_id'] = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'
    (profile / media_config.FILENAME).write_text(json.dumps(settings))
    result = CliRunner().invoke(cli, command)
    assert result.exit_code == 2
    body = json.loads(result.output)
    assert body['errors'][0]['stage'] == 'import' and 'account mismatch' in body['errors'][0]['message']
    assert not posts


@pytest.mark.parametrize('route', ['spotify-ifttt', 'generic-csv'])
def test_invalid_timezone_is_structured(server, route):
    args = next(args for name, args in ROUTES if name == route)
    result = CliRunner().invoke(cli, [*args_for(route, args), '--tz', 'Not/A_Timezone'])
    assert result.exit_code == 2
    assert json.loads(result.output)['errors'][0]['stage'] == 'args'


def test_apple_directory_discovery_remains_narrow(server, tmp_path):
    import shutil
    root = tmp_path / 'export'
    root.mkdir()
    shutil.copy(FIXTURES / 'apple_takeout_playback_sample.csv', root / 'Video Play Activity.csv')
    result = CliRunner().invoke(cli, ['import', 'apple-takeout', str(root), '--json'])
    assert result.exit_code == 2 and "No 'Playback Activity.csv'" in result.output
    shutil.copy(FIXTURES / 'apple_takeout_playback_sample.csv', root / 'Playback Activity.csv')
    result = CliRunner().invoke(cli, ['import', 'apple-takeout', str(root), '--json'])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)['verified'] == len(parsed('apple-takeout'))


@pytest.mark.parametrize('mode', ['lost', 'unknown', 'rejected'])
def test_original_pending_recovery_precedes_different_route(server, monkeypatch, mode):
    from fulcra_collect import db
    profile, _, rows, posts, _, state, _ = server
    state['mode'] = mode
    first = CliRunner().invoke(cli, args_for(*ROUTES[0]))
    assert first.exit_code == 2
    conn = db.open(profile / 'state.db')
    before = conn.execute('SELECT value_json FROM record_outboxes').fetchone()[0]
    state['mode'] = 'ok'
    if mode == 'lost':
        def resolve(path):
            assert db.get_record_outbox(conn, 'cli-media', 'v1_media_pending') == {'groups': []}
            return Path(path)
        monkeypatch.setattr('fulcra_media.library.resolve', resolve)
        second = CliRunner().invoke(cli, args_for(*ROUTES[5]))
        assert second.exit_code == 0, second.output
        assert len(posts) == 2  # Recovery reads original IDs, without reposting Spotify.
    else:
        state['reading'] = False
        monkeypatch.setattr('fulcra_media.library.resolve', lambda *_: pytest.fail('source before unknown recovery'))
        second = CliRunner().invoke(cli, args_for(*ROUTES[5]))
        assert second.exit_code == 2
        assert conn.execute('SELECT value_json FROM record_outboxes').fetchone()[0] == before


@pytest.mark.parametrize('conflict', [False, True])
def test_timemachine_exact_overlap_replays_and_conflicts(server, monkeypatch, tmp_path, conflict):
    import shutil
    import sqlite3
    _, _, _, posts, _, _, _ = server
    original = FIXTURES / 'apple_podcasts_mtlibrary.sqlite'
    later = tmp_path / 'later.sqlite'
    shutil.copy(original, later)
    conn = sqlite3.connect(later)
    columns = [r[1] for r in conn.execute('PRAGMA table_info(ZMTEPISODE)')]
    assert 'ZLASTDATEPLAYED' in columns
    if conflict:
        conn.execute('UPDATE ZMTEPISODE SET ZCLEANEDTITLE = ZCLEANEDTITLE || " changed"')
    else:
        conn.execute('UPDATE ZMTEPISODE SET ZLASTDATEPLAYED = ZLASTDATEPLAYED + 86400')
    conn.commit()
    conn.close()
    monkeypatch.setattr('fulcra_media.importers.apple_podcasts.find_timemachine_snapshots', lambda: [original, original, later])
    result = CliRunner().invoke(cli, ['import', 'apple-podcasts-timemachine', '--json'])
    if conflict:
        assert result.exit_code == 2 and not posts
    else:
        assert result.exit_code == 0, result.output
        assert json.loads(result.output)['verified'] == 2 * len(parsed('apple-podcasts'))


@pytest.mark.parametrize('fingerprint', ['auto', 'none', 'music', 'movie', 'tv', 'podcast'])
@pytest.mark.parametrize('empty_optional', [False, True])
def test_generic_all_mappings_options_and_fingerprints(server, tmp_path, fingerprint, empty_optional):
    from datetime import timezone
    from zoneinfo import ZoneInfo
    from fulcra_csv import ColumnMap
    from fulcra_media.importers.generic_csv import parse_media_csv, _FP_AUTO
    from fulcra_media.v1 import build_media_record
    _, ids, _, posts, tags, _, _ = server
    source = tmp_path / 'custom.csv'
    source.write_text('when,label,sub,key,seconds,until\n2024-11-03T01:30:00,Episode,Show,raw-id,99,2024-11-03T01:31:00\n2024-11-04T01:30:00,Episode,Show,raw-id,99,2024-11-04T01:31:00\n')
    sub, key = ('', '') if empty_optional else ('sub', 'key')
    command = ['import', 'generic-csv', str(source), '--service', 'custom', '--category', 'watched', '--ts-col', 'when', '--title-col', 'label', '--subtitle-col', sub, '--id-col', key, '--duration-col', 'seconds', '--end-col', 'until', '--confidence', 'low', '--tz', 'America/New_York', '--fingerprint', fingerprint, '--json']
    result = CliRunner().invoke(cli, command)
    assert result.exit_code == 0, result.output
    events = list(parse_media_csv(source, service='custom', category='watched', column_map=ColumnMap(timestamp='when', title='label', subtitle=sub or None, source_id=key or None,
        duration_seconds='seconds', end_time='until'), tz=ZoneInfo('America/New_York'), confidence='low', fingerprint_kind=_FP_AUTO if fingerprint == 'auto' else None if fingerprint == 'none' else fingerprint))
    assert len(events) == 2 and events[0].start_time.astimezone(timezone.utc).hour == 6
    assert (events[0].end_time - events[0].start_time).total_seconds() == 60
    assert events[0].timestamp_confidence == 'low' and events[0].deterministic_id != 'raw-id'
    assert ('content_fingerprint' in events[0].external_ids) == (fingerprint in ('auto', 'movie') or (fingerprint == 'music' and not empty_optional))
    assert posts == [(ids['watched'], [build_media_record(e, ids['watched'], tags=(tags['custom'],)) for e in events])]


def test_direct_modern_apple_video_csv(server, tmp_path):
    source = tmp_path / 'Video Play Activity.csv'
    source.write_text('Event Type,Content Title,UTC Start Time,UTC End Time,Play Duration\nplayActivity,Modern Movie,2025-06-15 21:00:00,2025-06-15 21:58:00,3480000\n')
    result = CliRunner().invoke(cli, ['import', 'apple-takeout', str(source), '--json'])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)['verified'] == 1
    record = server[3][0][1][0]
    assert record['title'] == 'Modern Movie' and record['timestamp_confidence'] == 'high'


def test_real_pending_preview_never_drains_or_writes_legacy(server, monkeypatch):
    from fulcra_collect import db
    profile, _, _, posts, _, state, _ = server
    state['mode'] = 'lost'
    assert CliRunner().invoke(cli, args_for(*ROUTES[0])).exit_code == 2
    conn = db.open(profile / 'state.db')
    before = conn.execute('SELECT value_json FROM record_outboxes').fetchone()[0]
    kv = conn.execute('SELECT * FROM plugin_kv').fetchall()
    monkeypatch.setattr('fulcra_media.state.save', lambda *_: pytest.fail('legacy state save'))
    state['mode'] = 'ok'
    result = CliRunner().invoke(cli, [*args_for(*ROUTES[5]), '--check-only'])
    assert result.exit_code == 0, result.output
    assert len(posts) == 1
    assert conn.execute('SELECT value_json FROM record_outboxes').fetchone()[0] == before
    assert conn.execute('SELECT * FROM plugin_kv').fetchall() == kv


@pytest.mark.parametrize('route,args', ROUTES)
def test_unknown_original_visibility_blocks_every_new_source(server, monkeypatch, route, args):
    from fulcra_collect import db
    profile, _, _, posts, _, state, requests = server
    state['mode'] = 'unknown'
    assert CliRunner().invoke(cli, args_for(*ROUTES[0])).exit_code == 2
    conn = db.open(profile / 'state.db')
    before = conn.execute('SELECT value_json FROM record_outboxes').fetchone()[0]
    state['reading'] = False
    monkeypatch.setattr('fulcra_media.library.resolve', lambda *_: pytest.fail('source before visibility proof'))
    monkeypatch.setattr('fulcra_media.importers.apple_podcasts.parse_db', lambda *_: pytest.fail('DB before visibility proof'))
    monkeypatch.setattr('fulcra_media.importers.apple_podcasts.find_timemachine_snapshots', lambda: pytest.fail('discovery before visibility proof'))
    request_count = len(requests)
    result = CliRunner().invoke(cli, args_for(route, args))
    assert result.exit_code == 2 and not json.loads(result.output)['ok']
    assert conn.execute('SELECT value_json FROM record_outboxes').fetchone()[0] == before
    assert len(posts) == 1
    # Original recovery may revalidate destination/schema metadata by GET.
    assert all(method == 'GET' for method, _ in requests[request_count:])
