"""End-to-end integration: import → cache populated → next import sees twins."""
import json
from datetime import datetime, timezone

import pytest
from click.testing import CliRunner

from fulcra_media.cli import cli
from fulcra_media.state import State, save


@pytest.fixture
def isolated_env(tmp_path, monkeypatch):
    """Tmp state.json + tmp twin cache so tests don't poison real config."""
    state_path = tmp_path / "state.json"
    save(State(
        watched_definition_id="w-uuid",
        listened_definition_id="l-uuid",
        tag_ids={"netflix": "t-netflix", "trakt": "t-trakt"},
    ), state_path)
    monkeypatch.setattr("fulcra_media.cli.STATE_PATH", state_path)
    cache_path = tmp_path / "twin_cache.json"
    monkeypatch.setattr("fulcra_media.twin_cache.DEFAULT_CACHE_PATH", cache_path)
    return tmp_path


ACCOUNT = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
OTHER_ACCOUNT = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'
DESTINATION = 'Event/11111111-2222-4333-8444-555555555555'
OTHER_DESTINATION = 'Event/22222222-2222-4333-8444-555555555555'


def make_configured_netflix(tmp_path, monkeypatch):
    """Real configured CLI/writer with only its HTTP transport synthesized."""
    import base64
    import re
    from types import SimpleNamespace
    import httpx
    from fulcra_common.client import BaseFulcraClient
    from fulcra_common.data_types import CreationJournal
    from fulcra_common.record_exclusions import EXCLUSION_SPEC
    from fulcra_media import media_config
    from fulcra_media.v1 import media_spec

    profile = tmp_path / 'profile'
    profile.mkdir()
    monkeypatch.setenv('FULCRA_COLLECT_HOME', str(profile))
    claims = base64.urlsafe_b64encode(json.dumps({'fulcradynamics.com/userid': ACCOUNT,
        'exp': 4102444800}).encode()).decode().rstrip('=')
    monkeypatch.setenv('FULCRA_ACCESS_TOKEN', 'synthetic.' + claims + '.signature')
    monkeypatch.setattr(httpx.HTTPTransport, 'handle_request', lambda *_: pytest.fail('external HTTP forbidden'))
    state_path = tmp_path / 'state.json'
    save(State(), state_path)
    monkeypatch.setattr('fulcra_media.cli.STATE_PATH', state_path)
    legacy_cache = tmp_path / 'twin_cache.json'
    legacy_cache.write_text('{"sentinel":"retained legacy bytes"}\n')
    monkeypatch.setattr('fulcra_media.twin_cache.DEFAULT_CACHE_PATH', legacy_cache)
    control = 'Event/aaaaaaaa-2222-4333-8444-555555555555'
    tag = '11111111-1111-4111-8111-111111111111'
    pins = {'watched': DESTINATION, 'listened': OTHER_DESTINATION,
            'read': 'Event/33333333-2222-4333-8444-555555555555'}
    specs = {destination: media_spec(category) for category, destination in pins.items()}
    specs[control] = EXCLUSION_SPEC
    CreationJournal(profile / 'v1-type-creations.sqlite').resolved(ACCOUNT, EXCLUSION_SPEC, control)
    settings = {'version': 1, 'account_id': ACCOUNT, 'v1_destination_ids': pins,
                'legacy_selection': dict.fromkeys(media_config.CATEGORIES)}
    (profile / media_config.FILENAME).write_text(json.dumps(settings))
    state = {'account': ACCOUNT, 'unknown': False}
    rows, posts, requests = {}, [], []

    def handle(request):
        requests.append((request.method, request.url.path))
        path = request.url.path
        if path == '/user/v1alpha1/info':
            return httpx.Response(200, json={'userid': state['account']})
        if path == '/input/v1/data_type':
            assert request.method == 'GET', 'configured targets must not create metadata'
            return httpx.Response(200, json=[{'id': full, 'name': spec.name,
                'fulcra_userid': ACCOUNT, 'api_version': 'v1', 'recordable': True,
                'deprecated': False, 'record_spec': {'type': 'event'}} for full, spec in specs.items()])
        if path.endswith('/schema'):
            destination = path.removeprefix('/data/v1/catalog/').removesuffix('/v1/schema')
            spec = specs[destination]
            return httpx.Response(200, json={'type': 'object', 'properties': {
                **{key: {} for key in ('id', 'sources', 'tags', 'start_time', 'end_time')},
                **spec.fields}, 'required': list(spec.required_fields)})
        if '/tag/name/' in path or '/tag/id/' in path:
            assert request.method == 'GET', 'existing synthetic tag must be read only'
            return httpx.Response(200, json={'id': tag, 'name': 'netflix', 'fulcra_userid': ACCOUNT})
        if path == '/data/v1/records':
            query = request.url.params['q']
            destination = re.search('__name__="([^\"]+)"', query).group(1)
            identifiers = re.findall('id="([^\"]+)"', query)
            return httpx.Response(200, text='\n'.join(json.dumps(rows[destination, identifier])
                for identifier in identifiers if (destination, identifier) in rows))
        if path == '/ingest/v1/record/' + DESTINATION:
            batch = [json.loads(line) for line in request.content.splitlines()]
            posts.extend(batch)
            if not state['unknown']:
                rows.update({(DESTINATION, row['id']): row for row in batch})
            return httpx.Response(200, json={'upload_id': 'synthetic-upload'})
        pytest.fail('unexpected HTTP path ' + path)

    client = BaseFulcraClient(base_url='https://example.test', transport=httpx.MockTransport(handle))
    real_open = media_config.open_media
    monkeypatch.setattr(media_config, 'open_media',
        lambda settings, **kw: real_open(settings, **{**kw, 'client': client}))
    return SimpleNamespace(root=tmp_path, profile=profile, state=state, rows=rows, posts=posts,
        requests=requests, legacy_cache=legacy_cache, legacy_before=legacy_cache.read_bytes(),
        legacy_state=state_path, legacy_state_before=state_path.read_bytes(),
        index_path=profile / 'v1-media-twins.sqlite')


@pytest.fixture
def netflix_env(tmp_path, monkeypatch):
    return make_configured_netflix(tmp_path, monkeypatch)


def _rich_csv(env):
    csv = env.root / 'rich.csv'
    csv.write_text(
        'Profile Name,Start Time,Duration,Attributes,Title,Supplemental Video Type,'
        'Device Type,Bookmark,Latest Bookmark,Country\n'
        'User,2026-05-12 20:32:15,01:42:30,,Dune: Part Two,,Apple TV 4K,01:42:30,01:42:30,US\n')
    return csv


def _assert_legacy_unchanged(env):
    assert env.legacy_cache.read_bytes() == env.legacy_before
    assert env.legacy_state.read_bytes() == env.legacy_state_before


def test_cache_populated_after_successful_high_conf_import(netflix_env):
    """Only verified rich v1 delivery seeds account/destination scoped hints."""
    from fulcra_media.collect_twins import MediaTwinIndex
    from fulcra_media.v1 import build_media_record
    from fulcra_media.importers.netflix import parse_auto
    csv = _rich_csv(netflix_env)
    events = list(parse_auto(csv))
    res = CliRunner().invoke(cli, ['import', 'netflix', str(csv), '--json'])
    assert res.exit_code == 0, res.output
    envelope = json.loads(res.output)
    assert envelope['posted'] == envelope['verified'] == len(events) == 1
    index = MediaTwinIndex(netflix_env.index_path)
    fingerprint = events[0].external_ids['content_fingerprint']
    record_id = build_media_record(events[0], DESTINATION)['id']
    assert index.lookup(ACCOUNT, DESTINATION, [fingerprint]) == {fingerprint: record_id}
    assert index.lookup(OTHER_ACCOUNT, DESTINATION, [fingerprint]) == {}
    assert index.lookup(ACCOUNT, OTHER_DESTINATION, [fingerprint]) == {}
    assert netflix_env.posts[0]['id'] == record_id
    _assert_legacy_unchanged(netflix_env)


def test_cache_NOT_populated_by_low_conf_import(netflix_env):
    csv = netflix_env.root / 'slim.csv'
    csv.write_text('Title,Date\nMovie,5/12/26\n')
    res = CliRunner().invoke(cli, ['import', 'netflix', str(csv), '--json'])
    assert res.exit_code == 0, res.output
    envelope = json.loads(res.output)
    assert envelope['posted'] == envelope['verified'] == 1
    assert netflix_env.posts[0]['timestamp_confidence'] == 'low'
    assert not netflix_env.index_path.exists()
    _assert_legacy_unchanged(netflix_env)


def test_cache_not_populated_on_check_only(netflix_env):
    from fulcra_collect import db
    csv = _rich_csv(netflix_env)
    conn = db.open(netflix_env.profile / 'state.db')
    db.set_record_outbox(conn, 'cli-media', 'v1_media_pending', {'groups': 'UNKNOWN preview evidence'})
    original_pending = conn.execute('SELECT value_json FROM record_outboxes').fetchone()[0]
    res = CliRunner().invoke(cli, ['import', 'netflix', str(csv), '--check-only', '--json'])
    assert res.exit_code == 0, res.output
    envelope = json.loads(res.output)
    assert envelope['would_post'] == 1 and envelope['verified'] == 0
    assert not netflix_env.index_path.exists()
    assert netflix_env.posts == []
    assert conn.execute('SELECT value_json FROM record_outboxes').fetchone()[0] == original_pending
    assert all(method == 'GET' for method, _ in netflix_env.requests)
    _assert_legacy_unchanged(netflix_env)


@pytest.mark.parametrize('failure', ['account', 'unknown'])
def test_unproven_import_cannot_seed_twin_index(netflix_env, failure):
    from fulcra_collect import db
    csv = _rich_csv(netflix_env)
    if failure == 'account':
        netflix_env.state['account'] = OTHER_ACCOUNT
    else:
        netflix_env.state['unknown'] = True
    res = CliRunner().invoke(cli, ['import', 'netflix', str(csv), '--json'])
    assert res.exit_code == 2, res.output
    assert json.loads(res.output)['ok'] is False
    assert not netflix_env.index_path.exists()
    if failure == 'account':
        assert netflix_env.posts == []
    else:
        conn = db.open(netflix_env.profile / 'state.db')
        pending = db.get_record_outbox(conn, 'cli-media', 'v1_media_pending')
        assert pending['groups'] and len(netflix_env.posts) == 1
        original = conn.execute('SELECT value_json FROM record_outboxes').fetchone()[0]
        again = CliRunner().invoke(cli, ['import', 'netflix', str(csv), '--json'])
        assert again.exit_code == 2
        assert len(netflix_env.posts) == 1
        assert conn.execute('SELECT value_json FROM record_outboxes').fetchone()[0] == original
    _assert_legacy_unchanged(netflix_env)


def test_low_conf_twin_detected_against_cached_event(isolated_env, monkeypatch):
    """Pre-populate cache, then ingest a low-conf event with same fingerprint."""
    from fulcra_media import twin_cache
    from fulcra_csv import find_low_conf_twins

    # Seed cache as if Netflix had imported Dune Part Two
    twin_cache.save(
        {
            "movie:dune-part-two": {
                "source_id": "com.fulcra.media.netflix-rich.abc123",
                "importer": "netflix-rich",
                "start_time": "2026-04-01T20:00:00+00:00",
                "confidence": "high",
            }
        },
        isolated_env / "twin_cache.json",
    )

    # Construct a low-conf incoming event with the same fingerprint
    from fulcra_media.importers.base import NormalizedEvent
    incoming = NormalizedEvent(
        importer="trakt", service="trakt", category="watched",
        note="Dune Part Two",
        title="Dune Part Two",
        start_time=datetime(2026, 5, 16, 18, tzinfo=timezone.utc),
        end_time=datetime(2026, 5, 16, 20, tzinfo=timezone.utc),
        deterministic_id="com.fulcra.media.trakt.v1.history.999",
        timestamp_confidence="low",
        external_ids={
            "content_fingerprint": "movie:dune-part-two",
            "timestamp_cluster_size": 1000,
        },
    )
    cached = twin_cache.load_for_twin_lookup(isolated_env / "twin_cache.json")
    pairs = find_low_conf_twins([incoming], extra_pool=cached)
    assert len(pairs) == 1
    low, high = pairs[0]
    assert low.deterministic_id == "com.fulcra.media.trakt.v1.history.999"
    assert high.source_id == "com.fulcra.media.netflix-rich.abc123"
