import httpx
from fulcra_common.client import BaseFulcraClient

import json
from pathlib import Path
import pytest
from click.testing import CliRunner


def test_retired_reset_has_no_effects(monkeypatch):
    from fulcra_media.cli import cli
    from fulcra_media import state
    monkeypatch.setattr(state, 'load', lambda *_: pytest.fail('legacy read'))
    for flags in ([], ['--confirm'], ['--confirm', '--keep-watched', '--keep-listened', '--keep-read']):
        result = CliRunner().invoke(cli, ['reset', *flags])
        assert result.exit_code == 2
        assert 'retired' in result.output


def test_strict_configuration_and_legacy(tmp_path):
    from fulcra_media.media_config import read_config, legacy_selection
    path = tmp_path / 'config'
    assert read_config(path) is None
    for raw in ('{}', '{"version":1,"version":1}', 'NaN'):
        path.write_text(raw)
        with pytest.raises((ValueError, RuntimeError)):
            read_config(path)
    path.write_text('{"watched_definition_id":"bad"}')
    with pytest.raises(ValueError):
        legacy_selection(path)


def test_pending_status_unknown_is_never_empty(tmp_path):
    from fulcra_media.media_config import pending_status
    assert pending_status(tmp_path)['state'] == 'absent'
    (tmp_path / 'state.db').write_bytes(b'broken')
    assert pending_status(tmp_path)['state'] == 'UNKNOWN'

ACCOUNT = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
PIN = 'Event/11111111-2222-4333-8444-555555555555'

@pytest.fixture
def authority(tmp_path, monkeypatch):
    import httpx
    from fulcra_common.client import BaseFulcraClient
    monkeypatch.setenv('FULCRA_COLLECT_HOME', str(tmp_path / 'profile'))
    monkeypatch.setenv('FULCRA_ACCESS_TOKEN', 'synthetic')
    monkeypatch.setattr(httpx.HTTPTransport, 'handle_request', lambda *_: pytest.fail('external HTTP forbidden'))
    return BaseFulcraClient(base_url='https://example.test', transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={'userid': ACCOUNT})))


def saved_config(root):
    from fulcra_media.media_config import CATEGORIES, FILENAME
    root.mkdir(exist_ok=True)
    data = {'version': 1, 'account_id': ACCOUNT,
            'v1_destination_ids': dict.fromkeys(CATEGORIES, PIN),
            'legacy_selection': dict.fromkeys(CATEGORIES)}
    (root / FILENAME).write_text(json.dumps(data))
    return data


def test_account_checked_before_resume(authority, tmp_path, monkeypatch):
    from fulcra_media.media_config import configured_media, FILENAME
    from fulcra_media.collect_v1 import MediaRecordWriter
    root = tmp_path / 'profile'
    data = saved_config(root)
    data['account_id'] = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'
    (root / FILENAME).write_text(json.dumps(data))
    monkeypatch.setattr(MediaRecordWriter, 'resume', lambda *_: pytest.fail('wrong-account resume'))
    with pytest.raises(RuntimeError, match='account mismatch'):
        with configured_media(tmp_path / 'legacy', client=authority):
            pytest.fail('source')


@pytest.mark.parametrize('preview', [False, True])
def test_explicit_pin_cannot_bypass_journal(authority, tmp_path, preview):
    from fulcra_media.media_config import configured_media
    from fulcra_common.data_types import CreationJournal
    from fulcra_media.v1 import media_spec
    root = tmp_path / 'profile'
    saved_config(root)
    CreationJournal(root / 'v1-type-creations.sqlite').resolved(ACCOUNT, media_spec('watched'),
        'Event/22222222-2222-4333-8444-555555555555')
    with pytest.raises(ValueError, match='binding conflicts'):
        with configured_media(tmp_path / 'legacy', client=authority, check_only=preview):
            pytest.fail('source')


def test_missing_config_pending_holds_before_auth(authority, tmp_path, monkeypatch):
    from fulcra_media.media_config import configured_media
    from fulcra_collect import db
    root = tmp_path / 'profile'
    root.mkdir()
    conn = db.open(root / 'state.db')
    db.set_record_outbox(conn, 'cli-media', 'v1_media_pending', {'groups': []})
    monkeypatch.setattr(authority, 'get_token', lambda: pytest.fail('auth'))
    with pytest.raises(RuntimeError, match='missing media configuration'):
        with configured_media(tmp_path / 'legacy', client=authority, bootstrap=True):
            pytest.fail('source')


def test_settings_lock_bounded_released(authority, tmp_path, monkeypatch):
    from fulcra_media.media_config import configured_media
    from fulcra_media.collect_v1 import MediaRecordWriter
    saved_config(tmp_path / 'profile')
    monkeypatch.setattr(MediaRecordWriter, 'prepare', lambda *_: None)
    with configured_media(tmp_path / 'legacy', client=authority):
        with pytest.raises(TimeoutError):
            with configured_media(tmp_path / 'legacy', client=authority, lease_timeout_s=0):
                pytest.fail('competing source')
    with configured_media(tmp_path / 'legacy', client=authority):
        pass


def test_bootstrap_requires_all_legacy_mappings_before_metadata(authority, tmp_path):
    from fulcra_media.media_config import configured_media, CATEGORIES
    legacy = tmp_path / 'legacy'
    legacy.write_text(json.dumps({c + '_definition_id': ACCOUNT for c in CATEGORIES}))
    before = legacy.read_bytes()
    with pytest.raises(RuntimeError, match='every retained legacy'):
        with configured_media(legacy, client=authority, bootstrap=True, pins={'watched': PIN}):
            pytest.fail('source')
    assert legacy.read_bytes() == before

@pytest.mark.parametrize('raw', ['null', '[]'])
def test_existing_invalid_json_is_not_absent(tmp_path, raw):
    from fulcra_media.media_config import read_config, legacy_selection
    path = tmp_path / 'existing'
    path.write_text(raw)
    for read in (read_config, legacy_selection):
        with pytest.raises(ValueError):
            read(path)


@pytest.mark.parametrize('mode', ['visible', 'unknown', 'account', 'malformed', 'legacy', 'preview', 'rejected', 'excluded'])
def test_configured_netflix_actual_writer_delivery_guards(tmp_path, monkeypatch, mode):
    import json
    import base64
    import re
    from fulcra_collect import db
    from fulcra_common.data_types import CreationJournal
    from fulcra_common.record_exclusions import EXCLUSION_SPEC
    from fulcra_media.v1 import media_spec
    from fulcra_media.media_config import configured_media, FILENAME, CATEGORIES
    def open_media(settings, **kwargs):
        return configured_media(tmp_path / 'legacy', **kwargs)

    profile = tmp_path / 'profile'
    claims = base64.urlsafe_b64encode(json.dumps({'fulcradynamics.com/userid': ACCOUNT, 'exp': 4102444800}).encode()).decode().rstrip('=')
    monkeypatch.setenv('FULCRA_COLLECT_HOME', str(profile))
    monkeypatch.setenv('FULCRA_ACCESS_TOKEN', 'synthetic.' + claims + '.signature')
    monkeypatch.setattr(httpx.HTTPTransport, 'handle_request', lambda *_: pytest.fail('external HTTP'))
    destination = 'Event/11111111-2222-4333-8444-555555555555'
    control = 'Event/aaaaaaaa-2222-4333-8444-555555555555'
    tag = '11111111-1111-4111-8111-111111111111'
    profile.mkdir()
    CreationJournal(profile / 'v1-type-creations.sqlite').resolved(ACCOUNT, EXCLUSION_SPEC, control)
    rows, posts, sources = {}, [], []
    account = [ACCOUNT]
    reading = [True]

    def handle(request):
        path = request.url.path
        if path == '/user/v1alpha1/info':
            return httpx.Response(200, json={'userid': account[0]})
        if path == '/input/v1/data_type':
            return httpx.Response(200, json=[{'id': full, 'fulcra_userid': ACCOUNT,
                'name': name, 'api_version': 'v1', 'recordable': True,
                'deprecated': False, 'record_spec': {'type': 'event'}}
                for full, name in ((destination, 'Watched'), (control, EXCLUSION_SPEC.name))])
        if path.endswith('/schema'):
            spec = EXCLUSION_SPEC if control in path else media_spec('watched')
            return httpx.Response(200, json={'type': 'object', 'properties': {
                **{k: {} for k in ('id', 'sources', 'tags', 'start_time', 'end_time')},
                **spec.fields}, 'required': list(spec.required_fields)})
        if '/tag/name/' in path or '/tag/id/' in path:
            return httpx.Response(200, json={'id': tag, 'name': 'netflix', 'fulcra_userid': ACCOUNT})
        if path == '/data/v1/records':
            if not reading[0]:
                return httpx.Response(503)
            query = request.url.params['q']
            full = re.search('__name__="([^"]+)"', query).group(1)
            ids = re.findall('id="([^"]+)"', query)
            return httpx.Response(200, text='\n'.join(json.dumps(rows[full, i])
                for i in ids if (full, i) in rows))
        if path == '/ingest/v1/record/' + destination:
            batch = [json.loads(line) for line in request.content.splitlines()]
            posts.extend(batch)
            if mode == 'rejected':
                return httpx.Response(400, json={'error': 'synthetic rejection'})
            if mode != 'unknown':
                rows.update({(destination, row['id']): row for row in batch})
            raise httpx.ReadError('synthetic lost receipt')
        pytest.fail('unexpected path ' + path)

    transport = httpx.MockTransport(handle)
    client = BaseFulcraClient(base_url='https://example.test', transport=transport)
    settings = {'version': 1, 'account_id': ACCOUNT, 'v1_destination_ids': dict.fromkeys(CATEGORIES, destination), 'legacy_selection': dict.fromkeys(CATEGORIES)}
    (profile / FILENAME).write_text(json.dumps(settings))
    from fulcra_media.importers.netflix import parse_auto
    csv = tmp_path / 'Netflix.csv'
    csv.write_text('Title,Date\nMovie,1/1/26\n')
    sample = list(parse_auto(csv))[0]
    start = sample.start_time
    if mode == 'excluded':
        from fulcra_common.v1 import build_event_record
        from fulcra_media.v1 import build_media_record
        record = build_media_record(sample, destination)
        exclusion = build_event_record(data_type=control,
            source_id=f"collect:v1:excluded:{destination}:{record['id']}", start_time=start,
            fields={'schema_version': 1, 'target_data_type': destination,
                'target_record_id': record['id'], 'action_id': ACCOUNT,
                'reason': 'undo', 'evidence': []})
        rows[control, exclusion['id']] = exclusion
        with open_media(settings, client=client) as run:
            result = run.import_events([sample])
            assert result.posted == result.verified == 0
            assert result.skipped_existing == 1
        assert posts == []
        return
    with pytest.raises(httpx.HTTPError):
        with open_media(settings, client=client) as run:
            sources.append('first')
            run.import_events([sample])
    assert len(posts) == 1
    conn = db.open(profile / 'state.db')
    before = db.get_record_outbox(conn, 'cli-media', 'v1_media_pending')
    assert before['groups'] and before['dispatch_version'] == 1
    if mode == 'settings':
        settings = {**settings, 'twin_policy': 'keep'}
    if mode == 'account':
        account[0] = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'
    if mode == 'unknown':
        reading[0] = False
    if mode == 'malformed':
        before['groups'] = 'malformed'
        db.set_record_outbox(conn, 'cli-media', 'v1_media_pending', before)
    if mode == 'legacy':
        before.pop('dispatch_version')
        before.pop('dispatch')
        rows.clear()
        db.set_record_outbox(conn, 'cli-media', 'v1_media_pending', before)
    if mode == 'preview':
        with open_media(settings, client=client, check_only=True) as run:
            assert run.import_events([sample]).verified == 0
            with pytest.raises(RuntimeError):
                run.writer.run_import([sample])
        assert db.get_record_outbox(conn, 'cli-media', 'v1_media_pending') == before
        assert len(posts) == 1
        return
    if mode == 'visible':
        with open_media(settings, client=client) as run:
            sources.append('second')
            assert run.import_events([]).total == 0
        assert db.get_record_outbox(conn, 'cli-media', 'v1_media_pending') == {'groups': []}
        assert sources == ['first', 'second']
    else:
        with pytest.raises((RuntimeError, ValueError, httpx.HTTPError)):
            with open_media(settings, client=client):
                sources.append('must not acquire even empty source')
        assert db.get_record_outbox(conn, 'cli-media', 'v1_media_pending') == before
        assert sources == ['first']
    assert len(posts) == 1


def test_final_save_preserves_original_on_interruption(tmp_path, monkeypatch):
    from fulcra_media.media_config import _save, FILENAME
    data = saved_config(tmp_path)
    path = tmp_path / FILENAME
    before = path.read_bytes()
    monkeypatch.setattr('fulcra_media.media_config.os.replace', lambda *_: (_ for _ in ()).throw(OSError('interrupted')))
    with pytest.raises(OSError):
        _save(path, data, data)
    assert path.read_bytes() == before
    assert not list(tmp_path.glob('.cli-media-*'))


def test_status_reads_pending_from_live_wal(tmp_path):
    import hashlib
    from fulcra_collect import db
    from fulcra_media.media_config import pending_status
    conn = db.open(tmp_path / 'state.db')
    db.set_record_outbox(conn, 'cli-media', 'v1_media_pending', {'groups': []})
    raw = conn.execute('SELECT value_json FROM record_outboxes').fetchone()[0]
    assert (tmp_path / 'state.db-wal').exists()
    status = pending_status(tmp_path)
    assert status['state'] == 'UNKNOWN' and status['retained'] is True
    assert status['sha256'] == hashlib.sha256(raw.encode()).hexdigest()
    assert 'raw' not in status


@pytest.mark.parametrize('bits', range(16))
def test_reset_all_flag_combinations_no_io(monkeypatch, bits):
    from fulcra_media.cli import cli
    monkeypatch.setattr('fulcra_media.state.load', lambda *_: pytest.fail('legacy read'))
    monkeypatch.setattr('fulcra_media.fulcra.FulcraClient', lambda *_: pytest.fail('client'))
    flags = [flag for index, flag in enumerate(('--confirm', '--keep-watched', '--keep-listened', '--keep-read')) if bits & (1 << index)]
    result = CliRunner().invoke(cli, ['reset', *flags])
    assert result.exit_code == 2
    assert 'retired' in result.output


def test_partial_legacy_state_holds(tmp_path):
    from fulcra_media.media_config import legacy_selection
    path = tmp_path / 'legacy'
    path.write_text('{"watched_definition_id":null}')
    with pytest.raises(ValueError, match='incomplete'):
        legacy_selection(path)


def test_cached_status_never_discloses_pending_payload(tmp_path, monkeypatch):
    from fulcra_collect import db
    from fulcra_media.media_config import cached_status
    monkeypatch.setenv('FULCRA_COLLECT_HOME', str(tmp_path))
    conn = db.open(tmp_path / 'state.db')
    private = {'groups': [], 'note': 'PRIVATE PERSONAL NOTE SENTINEL'}
    db.set_record_outbox(conn, 'cli-media', 'v1_media_pending', private)
    before = conn.execute('SELECT value_json FROM record_outboxes').fetchone()[0]
    rendered = json.dumps(cached_status(tmp_path / 'legacy'))
    assert 'PRIVATE PERSONAL NOTE SENTINEL' not in rendered
    assert 'raw' not in rendered
    assert conn.execute('SELECT value_json FROM record_outboxes').fetchone()[0] == before


@pytest.mark.parametrize('extra', ['watermarks', 'extra'])
def test_overflow_float_anywhere_in_legacy_holds(tmp_path, extra):
    from fulcra_media.media_config import legacy_selection
    path = tmp_path / 'legacy'
    path.write_text('{"watched_definition_id":null,"listened_definition_id":null,"read_definition_id":null,"' + extra + '":{"bad":1e999}}')
    with pytest.raises(ValueError):
        legacy_selection(path)


@pytest.mark.parametrize('mode', ['missing_pk', 'missing_notnull', 'future', 'missing_versions', 'bad_versions'])
def test_invalid_existing_outbox_schema_never_reports_absent(tmp_path, mode):
    import sqlite3
    from fulcra_collect import db
    from fulcra_media.media_config import pending_status
    if mode in ('future', 'missing_versions', 'bad_versions'):
        conn = db.open(tmp_path / 'state.db')
        if mode == 'future':
            conn.execute('INSERT INTO schema_version VALUES (?,?)', (db.LATEST_VERSION + 1, 'synthetic'))
        elif mode == 'missing_versions':
            conn.execute('DROP TABLE schema_version')
        else:
            conn.execute('DELETE FROM schema_version WHERE version=8')
    else:
        conn = sqlite3.connect(tmp_path / 'state.db')
        conn.execute('CREATE TABLE schema_version(version INTEGER PRIMARY KEY,applied_at TEXT NOT NULL)')
        conn.executemany('INSERT INTO schema_version VALUES (?,?)', [(i, 'synthetic') for i in range(1, db.LATEST_VERSION + 1)])
        columns = 'plugin_id TEXT NOT NULL,key TEXT NOT NULL,value_json TEXT NOT NULL,updated_at TEXT NOT NULL'
        if mode == 'missing_notnull':
            columns = columns.replace(' NOT NULL', '') + ',PRIMARY KEY(plugin_id,key)'
        conn.execute('CREATE TABLE record_outboxes(' + columns + ')')
        conn.commit()
    assert pending_status(tmp_path)['state'] == 'UNKNOWN'


@pytest.mark.parametrize('value', [float('nan'), float('inf'), -1, '10', True])
def test_invalid_settings_timeout_rejected_before_lock(tmp_path, monkeypatch, value):
    from fulcra_media.media_config import configured_media
    monkeypatch.setenv('FULCRA_COLLECT_HOME', str(tmp_path / 'profile'))
    monkeypatch.setattr('fulcra_media.media_config._file_lock', lambda *_a, **_k: pytest.fail('invalid timeout reached lock'))
    with pytest.raises(ValueError, match='timeout'):
        with configured_media(tmp_path / 'legacy', lease_timeout_s=value):
            pytest.fail('source')


@pytest.mark.parametrize('change', ['config', 'profile', 'legacy', 'journal'])
def test_save_rechecks_local_authority_after_temp_fsync(tmp_path, monkeypatch, change):
    from fulcra_media.media_config import _save, FILENAME
    root = tmp_path / 'profile'
    original = saved_config(root)
    path = root / FILENAME
    before = path.read_bytes()
    legacy = tmp_path / 'legacy'
    legacy.write_text('original')
    real_fsync = __import__('os').fsync
    mutation = []
    def fsync(fd):
        real_fsync(fd)
        if not mutation:
            mutation.append(change)
            if change == 'config':
                modified = {**original, 'account_id': 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'}
                path.write_text(json.dumps(modified))
            elif change == 'legacy':
                legacy.write_text('changed')
    def guard():
        if change in ('profile', 'journal') and mutation:
            raise RuntimeError(change + ' changed')
        if legacy.read_text() != 'original':
            raise RuntimeError('legacy changed')
    monkeypatch.setattr('fulcra_media.media_config.os.fsync', fsync)
    with pytest.raises(RuntimeError, match='changed'):
        _save(path, original, original, guard=guard)
    if change == 'config':
        assert path.read_bytes() != before
        assert json.loads(path.read_text())['account_id'].startswith('bbbb')
    else:
        assert path.read_bytes() == before
    assert not list(root.glob('.cli-media-*'))


@pytest.fixture
def bootstrap_server(tmp_path, monkeypatch):
    import base64
    from fulcra_media.v1 import media_spec
    from fulcra_media.media_config import CATEGORIES
    profile = tmp_path / 'profile'
    monkeypatch.setenv('FULCRA_COLLECT_HOME', str(profile))
    claims = base64.urlsafe_b64encode(json.dumps({'fulcradynamics.com/userid': ACCOUNT, 'exp': 4102444800}).encode()).decode().rstrip('=')
    monkeypatch.setenv('FULCRA_ACCESS_TOKEN', 'synthetic.' + claims + '.signature')
    monkeypatch.setattr(httpx.HTTPTransport, 'handle_request', lambda *_: pytest.fail('external HTTP forbidden'))
    specs = {media_spec(category).name: media_spec(category) for category in CATEGORIES}
    from fulcra_common.record_exclusions import EXCLUSION_SPEC
    specs[EXCLUSION_SPEC.name] = EXCLUSION_SPEC
    catalog, posts, state = [], [], {'failure': None, 'fired': False}
    def handle(request):
        path = request.url.path
        if path == '/user/v1alpha1/info':
            return httpx.Response(200, json={'userid': ACCOUNT})
        if path == '/input/v1/data_type/Event' and request.method == 'POST':
            body = json.loads(request.content)
            name = body['name']
            assert name in specs
            assert name not in posts, 'metadata must never be POSTed twice'
            posts.append(name)
            destination = 'Event/' + str(__import__('uuid').UUID(int=len(posts)))
            catalog.append({'id': destination, 'name': name, 'api_version': 'v1',
                'recordable': True, 'deprecated': False, 'fulcra_userid': ACCOUNT,
                'record_spec': body['record_spec']})
            if state['failure'] == 'lost_reply' and not state['fired']:
                state['fired'] = True
                raise httpx.ReadError('synthetic lost metadata reply')
            return httpx.Response(201, json={'id': destination})
        if path == '/input/v1/data_type':
            if state['failure'] == 'proof_failure' and posts and not state['fired']:
                state['fired'] = True
                return httpx.Response(503)
            return httpx.Response(200, json=catalog)
        if path.endswith('/schema'):
            destination = path.removeprefix('/data/v1/catalog/').removesuffix('/v1/schema')
            row = next(row for row in catalog if row['id'] == destination)
            spec = specs[row['name']]
            return httpx.Response(200, json={'type': 'object', 'properties': {
                **{key: {} for key in ('id', 'sources', 'tags', 'start_time', 'end_time')},
                **spec.fields}, 'required': list(spec.required_fields)})
        pytest.fail('unexpected synthetic path ' + path)
    client = BaseFulcraClient(base_url='https://example.test', transport=httpx.MockTransport(handle))
    return profile, client, catalog, posts, state


@pytest.mark.parametrize('failure', ['persist', 'lost_reply', 'proof_failure'])
def test_actual_bootstrap_reopen_retains_original_creation_ids(bootstrap_server, tmp_path, monkeypatch, failure):
    from fulcra_media.media_config import configured_media, FILENAME, read_config, CATEGORIES
    from fulcra_common.data_types import CreationJournal
    from fulcra_media.v1 import media_spec
    profile, client, catalog, posts, state = bootstrap_server
    state['failure'] = failure
    original_replace = __import__('os').replace
    if failure == 'persist':
        monkeypatch.setattr('fulcra_media.media_config.os.replace',
            lambda *_: (_ for _ in ()).throw(OSError('synthetic interrupted final save')))
    with pytest.raises((OSError, httpx.HTTPError)):
        with configured_media(tmp_path / 'legacy', client=client, bootstrap=True):
            pytest.fail('interrupted bootstrap yielded')
    assert not (profile / FILENAME).exists()
    retained = {row['name']: row['id'] for row in catalog}
    journal = CreationJournal(profile / 'v1-type-creations.sqlite', read_only=True)
    for name, destination in retained.items():
        category = next(c for c in CATEGORIES if media_spec(c).name == name)
        row = journal.read(ACCOUNT, media_spec(category))
        if failure == 'lost_reply':
            assert row[1:] == ('pending', None)
        else:
            assert row[2] == destination
    monkeypatch.setattr('fulcra_media.media_config.os.replace', original_replace)
    with configured_media(tmp_path / 'legacy', client=client, bootstrap=True):
        pass
    saved = read_config(profile / FILENAME)
    assert len(posts) == len(set(posts)) == 3
    for category in CATEGORIES:
        destination = saved['v1_destination_ids'][category]
        assert next(row['id'] for row in catalog if row['name'] == media_spec(category).name) == destination
        if media_spec(category).name in retained:
            assert destination == retained[media_spec(category).name]
        assert journal.read(ACCOUNT, media_spec(category))[1:] == ('resolved', destination)


@pytest.mark.parametrize('change', ['config', 'profile', 'legacy', 'journal'])
def test_actual_bootstrap_temp_io_mutations_preserve_changed_authority(bootstrap_server, tmp_path, monkeypatch, change):
    import os
    from fulcra_media.media_config import configured_media, FILENAME, CATEGORIES
    from fulcra_common.data_types import CreationJournal
    profile, client, catalog, posts, state = bootstrap_server
    legacy = tmp_path / 'legacy'
    real_fsync = os.fsync
    mutations = []
    def fsync(fd):
        real_fsync(fd)
        if mutations:
            return
        mutations.append(change)
        if change == 'config':
            saved_config(profile)
        elif change == 'profile':
            monkeypatch.setenv('FULCRA_COLLECT_HOME', str(tmp_path / 'changed-profile'))
        elif change == 'legacy':
            legacy.write_text(json.dumps({c + '_definition_id': ACCOUNT for c in CATEGORIES}))
        else:
            journal = CreationJournal(profile / 'v1-type-creations.sqlite', read_only=True)
            with journal._connect() as conn:
                # Direct test-only corruption represents an external local edit.
                pass
            import sqlite3
            with sqlite3.connect(journal.path) as conn:
                conn.execute("UPDATE creations SET status='pending', destination=NULL")
    monkeypatch.setattr('fulcra_media.media_config.os.fsync', fsync)
    with pytest.raises((RuntimeError, ValueError)):
        with configured_media(legacy, client=client, bootstrap=True):
            pytest.fail('changed authority committed')
    assert len(posts) == 3
    if change == 'config':
        assert json.loads((profile / FILENAME).read_text())['v1_destination_ids']['watched'] == PIN
    else:
        assert not (profile / FILENAME).exists()
    assert not (tmp_path / 'changed-profile').exists()


def test_actual_configured_rich_fixture_preserves_all_parser_payloads(tmp_path, monkeypatch):
    mode = 'visible'
    import json
    import base64
    import re
    from fulcra_common.data_types import CreationJournal
    from fulcra_common.record_exclusions import EXCLUSION_SPEC
    from fulcra_media.v1 import media_spec
    from fulcra_media.media_config import configured_media, FILENAME, CATEGORIES
    def open_media(settings, **kwargs):
        return configured_media(tmp_path / 'legacy', **kwargs)

    profile = tmp_path / 'profile'
    claims = base64.urlsafe_b64encode(json.dumps({'fulcradynamics.com/userid': ACCOUNT, 'exp': 4102444800}).encode()).decode().rstrip('=')
    monkeypatch.setenv('FULCRA_COLLECT_HOME', str(profile))
    monkeypatch.setenv('FULCRA_ACCESS_TOKEN', 'synthetic.' + claims + '.signature')
    monkeypatch.setattr(httpx.HTTPTransport, 'handle_request', lambda *_: pytest.fail('external HTTP'))
    destination = 'Event/11111111-2222-4333-8444-555555555555'
    control = 'Event/aaaaaaaa-2222-4333-8444-555555555555'
    tag = '11111111-1111-4111-8111-111111111111'
    profile.mkdir()
    CreationJournal(profile / 'v1-type-creations.sqlite').resolved(ACCOUNT, EXCLUSION_SPEC, control)
    rows, posts = {}, []
    account = [ACCOUNT]
    reading = [True]

    def handle(request):
        path = request.url.path
        if path == '/user/v1alpha1/info':
            return httpx.Response(200, json={'userid': account[0]})
        if path == '/input/v1/data_type':
            return httpx.Response(200, json=[{'id': full, 'fulcra_userid': ACCOUNT,
                'name': name, 'api_version': 'v1', 'recordable': True,
                'deprecated': False, 'record_spec': {'type': 'event'}}
                for full, name in ((destination, 'Watched'), (control, EXCLUSION_SPEC.name))])
        if path.endswith('/schema'):
            spec = EXCLUSION_SPEC if control in path else media_spec('watched')
            return httpx.Response(200, json={'type': 'object', 'properties': {
                **{k: {} for k in ('id', 'sources', 'tags', 'start_time', 'end_time')},
                **spec.fields}, 'required': list(spec.required_fields)})
        if '/tag/name/' in path or '/tag/id/' in path:
            return httpx.Response(200, json={'id': tag, 'name': 'netflix', 'fulcra_userid': ACCOUNT})
        if path == '/data/v1/records':
            if not reading[0]:
                return httpx.Response(503)
            query = request.url.params['q']
            full = re.search('__name__="([^"]+)"', query).group(1)
            ids = re.findall('id="([^"]+)"', query)
            return httpx.Response(200, text='\n'.join(json.dumps(rows[full, i])
                for i in ids if (full, i) in rows))
        if path == '/ingest/v1/record/' + destination:
            batch = [json.loads(line) for line in request.content.splitlines()]
            posts.extend(batch)
            if mode == 'rejected':
                return httpx.Response(400, json={'error': 'synthetic rejection'})
            if mode != 'unknown':
                rows.update({(destination, row['id']): row for row in batch})
            raise httpx.ReadError('synthetic lost receipt')
        pytest.fail('unexpected path ' + path)

    transport = httpx.MockTransport(handle)
    client = BaseFulcraClient(base_url='https://example.test', transport=transport)
    settings = {'version': 1, 'account_id': ACCOUNT, 'v1_destination_ids': dict.fromkeys(CATEGORIES, destination), 'legacy_selection': dict.fromkeys(CATEGORIES)}
    (profile / FILENAME).write_text(json.dumps(settings))
    from fulcra_media.importers.netflix import parse_auto
    csv = Path(__file__).parent / 'fixtures' / 'netflix_rich_small.csv'
    samples = list(parse_auto(csv))
    assert len(samples) == 5  # Existing fixture: trailer rows remain filtered.
    from fulcra_media.v1 import build_media_record
    with pytest.raises(httpx.ReadError):
        with configured_media(tmp_path / 'legacy', client=client) as run:
            run.import_events(samples)
    assert len(posts) == len(samples)
    assert posts == [build_media_record(event, destination, tags=(tag,))
                     for event in sorted(samples, key=lambda event: event.start_time)]
    assert all(record['importer'] == 'netflix-rich' for record in posts)
    with configured_media(tmp_path / 'legacy', client=client) as run:
        result = run.import_events(samples)
        assert result.skipped_existing == len(samples)
        assert result.posted == result.verified == 0
    assert len(posts) == len(samples)


@pytest.mark.parametrize('stage', ['resolve', 'parse'])
def test_cli_netflix_json_scrubs_source_errors_after_actual_recovery(bootstrap_server, tmp_path, monkeypatch, stage):
    from fulcra_media import media_config
    from fulcra_media.cli import cli
    profile, client, catalog, posts, state = bootstrap_server
    with media_config.configured_media(tmp_path / 'legacy', client=client, bootstrap=True):
        pass
    real_open = media_config.open_media
    monkeypatch.setattr(media_config, 'open_media', lambda settings, **kw: real_open(settings, **{**kw, 'client': client}))
    monkeypatch.setattr('fulcra_media.cli.STATE_PATH', tmp_path / 'legacy')
    error = RuntimeError('source failed https://example.test/?api_key=SYNTHETIC_PRIVATE&token=SECOND_PRIVATE')
    def fail(*_):
        raise error
    if stage == 'resolve':
        monkeypatch.setattr('fulcra_media.library.resolve', fail)
        monkeypatch.setattr('fulcra_media.cli.netflix_importer.parse_auto', lambda *_: pytest.fail('parse after resolver failure'))
    else:
        monkeypatch.setattr('fulcra_media.library.resolve', lambda *_: tmp_path / 'synthetic.csv')
        monkeypatch.setattr('fulcra_media.cli.netflix_importer.parse_auto', fail)
    result = CliRunner().invoke(cli, ['import', 'netflix', 'fulcra:/synthetic.csv', '--json'])
    assert result.exit_code == 2
    assert len(result.output.splitlines()) == 1
    body = json.loads(result.output)
    assert body['ok'] is False and body['importer'] == 'netflix'
    assert 'api_key=REDACTED' in body['errors'][0]['message']
    assert 'SYNTHETIC_PRIVATE' not in result.output and 'SECOND_PRIVATE' not in result.output


@pytest.mark.parametrize('empty_source', [False, True])
def test_cli_recovery_holds_before_library_or_parser_even_empty(bootstrap_server, tmp_path, monkeypatch, empty_source):
    from fulcra_media import media_config
    from fulcra_media.cli import cli
    from fulcra_collect import db
    profile, client, catalog, posts, state = bootstrap_server
    with media_config.configured_media(tmp_path / 'legacy', client=client, bootstrap=True):
        pass
    conn = db.open(profile / 'state.db')
    db.set_record_outbox(conn, 'cli-media', 'v1_media_pending', {'groups': 'malformed', 'note': 'KEEP PRIVATE'})
    before = conn.execute('SELECT value_json FROM record_outboxes').fetchone()[0]
    real_open = media_config.open_media
    monkeypatch.setattr(media_config, 'open_media', lambda settings, **kw: real_open(settings, **{**kw, 'client': client}))
    monkeypatch.setattr('fulcra_media.cli.STATE_PATH', tmp_path / 'legacy')
    monkeypatch.setattr('fulcra_media.library.resolve', lambda *_: pytest.fail('library touched before recovery'))
    def parse(*_):
        if empty_source:
            return []
        pytest.fail('parser touched before recovery')
    monkeypatch.setattr('fulcra_media.cli.netflix_importer.parse_auto', parse)
    result = CliRunner().invoke(cli, ['import', 'netflix', 'fulcra:/synthetic.csv', '--json'])
    assert result.exit_code == 2
    assert json.loads(result.output)['ok'] is False
    assert 'KEEP PRIVATE' not in result.output
    assert conn.execute('SELECT value_json FROM record_outboxes').fetchone()[0] == before
    assert len(posts) == 3  # Recovery failure precedes new metadata too.
