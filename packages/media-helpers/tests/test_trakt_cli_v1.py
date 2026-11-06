"""Trakt activation: actual isolated configured authority and delivery kernel."""
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
import json

import click
import pytest
from click.testing import CliRunner

from test_poll_cursors import configured as configured  # noqa: F401
from fulcra_media.importers.base import NormalizedEvent


def event(*, importer, primary, extras, **overrides):
    start = datetime(2026, 1, 2, tzinfo=timezone.utc)
    return NormalizedEvent(importer=importer, service=importer, category='watched',
        note='Synthetic film', title='Synthetic film', start_time=start,
        end_time=start + timedelta(minutes=1),
        deterministic_id=f'com.fulcra.media.{importer}.v1.{primary}',
        timestamp_confidence='high', extra_source_ids=extras, **overrides)


def pair(n=0):
    high = event(importer='trakt', primary=f'high-{n}', extras=(),
                 external_ids={'content_fingerprint': f'synthetic-{n}'})
    low = replace(high, deterministic_id=f'com.fulcra.media.trakt.v1.low-{n}',
                  timestamp_confidence='low')
    return high, low


@pytest.fixture
def route(configured, monkeypatch):
    from fulcra_media import media_config
    from fulcra_media.importers import trakt
    state = {'events': [], 'calls': [], 'items': [], 'tty': False}
    monkeypatch.setattr(media_config, 'configured_media', lambda *a, **kw: configured.open(**kw))
    class Auth:
        def __init__(self, **kwargs):
            from types import SimpleNamespace
            self.binding = SimpleNamespace(check_before_source=lambda: None,
                                           check_local_after_authority=lambda: None)
            state['calls'].append('auth')
            if state.get('before_auth'):
                state['before_auth']()
            if state.get('fail') == 'auth':
                raise RuntimeError('SECRET https://private.invalid/SECRET')
        def headers(self, *, timeout=30, _remaining=None):
            assert 0 < timeout <= 30
            state['calls'].append('headers')
            return {'Authorization': 'synthetic'}
    def fetch(headers, *, strict_scan, _bounds, _before_request):
        assert strict_scan is True
        assert isinstance(_bounds, trakt._ScanBounds)
        state['calls'].append('fetch')
        if state.get('fail') == 'fetch':
            raise RuntimeError('SECRET https://private.invalid/SECRET')
        return iter(state['items'])
    actual_normalize = trakt.normalize_history
    state['normalize_actual'] = actual_normalize
    def normalize(items, **kw):
        state['calls'].append('normalize')
        if state.get('fail') == 'normalize':
            raise RuntimeError('SECRET https://private.invalid/SECRET')
        return actual_normalize(items, **kw) if state.get('real_normalize') else iter(state['events'])
    # This fixture isolates policy behavior; real identity acceptance is covered
    # by test_trakt_identity/test_trakt_source_callers with actual file owners.
    from fulcra_collect import trakt_identity
    from types import SimpleNamespace
    monkeypatch.setattr(trakt_identity, 'CurrentIdentity', lambda binding: SimpleNamespace(
        observe=lambda *a, **kw: None,
        check_local_after_authority=binding.check_local_after_authority,
        check_before_current=binding.check_before_source))
    monkeypatch.setattr(trakt, 'TraktAuth', Auth)
    monkeypatch.setattr(trakt, 'fetch_history_with_headers', fetch)
    monkeypatch.setattr(trakt, 'normalize_history', normalize)
    actual_stream = click.get_text_stream
    class Input:
        def isatty(self):
            return state['tty']
    monkeypatch.setattr(click, 'get_text_stream', lambda name: Input() if name == 'stdin' else actual_stream(name))
    def invoke(*args, json_mode=True):
        from fulcra_media.cli import cli
        result = CliRunner().invoke(cli, ['import', 'trakt', *args, *(['--json'] if json_mode else [])])
        if json_mode:
            assert len(result.stdout.splitlines()) == 1, (result.output, result.exception)
            return result, json.loads(result.stdout)
        return result, None
    state['invoke'] = invoke
    state['configured'] = configured
    return state


@pytest.mark.parametrize('option', [('--twin-policy', 'ask'), ('--clusters', 'ask'),
    ('--clusters', 'sentinel:1900'), ('--clusters', 'sentinel:SECRET'), ('--clusters', 'SECRET')])
@pytest.mark.parametrize('tty', [False, True])
def test_early_args_json_even_empty(route, option, tty):
    route['tty'] = tty
    result, data = route['invoke'](*option)
    assert result.exit_code == 2
    assert data['errors'][0]['stage'] == 'args'
    assert route['calls'] == []
    assert 'SECRET' not in result.output


@pytest.mark.parametrize('option', ['--clusters', '--twin-policy'])
def test_early_explicit_ask_non_tty(route, option):
    result, _ = route['invoke'](option, 'ask', json_mode=False)
    assert result.exit_code == 2
    assert route['calls'] == []


def test_actual_success_replay_totals_and_default_warning(route):
    high, low = pair()
    route['events'] = [high, low]
    result, data = route['invoke']()
    assert result.exit_code == 0, data
    assert (data['total'], data['posted'], data['verified'], data['cursor']) == (2, 2, 2, None)
    assert 'warning' in result.stderr
    assert data['since_watermark'] is data['new_watermark'] is None
    result, data = route['invoke']('--twin-policy', 'keep')
    assert result.exit_code == 0
    assert data['skipped_existing'] == 2
    assert data['posted'] == 0
    assert 'warning' not in result.stderr


def test_empty_has_no_warning_or_prompt(route, monkeypatch):
    monkeypatch.setattr(click, 'prompt', lambda *a, **kw: pytest.fail('prompt'))
    result, data = route['invoke']()
    assert result.exit_code == 0 and data['total'] == 0
    assert not result.stderr
    assert route['calls'] == ['auth', 'headers', 'fetch', 'normalize']


@pytest.mark.parametrize('answer,posted', [('yes', 7), ('no', 14)])
def test_every_offered_pair_current_input_and_yes_no(route, monkeypatch, answer, posted):
    route['tty'] = True
    route['events'] = [e for n in range(7) for e in pair(n)]
    monkeypatch.setattr(click, 'prompt', lambda *a, **kw: answer)
    result, _ = route['invoke']('--twin-policy', 'ask', json_mode=False)
    assert result.exit_code == 0, result.output
    assert len(route['configured'].posts) == posted
    for n in range(7):
        assert f'low-{n}' in result.output
    assert result.output.count('current-input') == 7


@pytest.mark.parametrize('stage', ['auth', 'fetch', 'normalize'])
def test_sanitized_stages_after_cleanup(route, stage):
    route['fail'] = stage
    result, data = route['invoke']()
    assert result.exit_code == 2
    assert data['errors'][0]['stage'] == ('fetch' if stage == 'normalize' else stage)
    assert 'SECRET' not in result.output and 'https://' not in result.output
    with route['configured'].open():
        pass  # lease was released


@pytest.mark.parametrize('raised', [click.Abort, EOFError])
def test_prompt_cancellation_structured_after_cleanup(route, monkeypatch, raised):
    route['events'] = list(pair())
    route['tty'] = True
    def fail(*a, **kw):
        raise raised()
    monkeypatch.setattr(click, 'prompt', fail)
    result, _ = route['invoke'](json_mode=False)
    assert result.exit_code == 2
    assert 'error in args' in result.output
    assert not route['configured'].posts
    with route['configured'].open():
        pass


def test_auto_discard_writer_total_includes_loose_skip(route):
    route['events'] = list(pair())
    result, data = route['invoke']('--twin-policy', 'auto-discard')
    assert result.exit_code == 0, data
    assert (data['total'], data['skipped_existing'], data['posted'], data['verified']) == (2, 1, 1, 1)


@pytest.mark.parametrize('mutation', ['delete', 'confidence', 'fingerprint', 'replace', 'new-pair', 'remove-input', 'change-input'])
def test_offer_intersects_fresh_pairs(configured, mutation):
    from fulcra_media.collect_twins import MediaTwinIndex
    from fulcra_media.v1 import build_media_record
    high, low = pair()
    destination = configured.settings['v1_destination_ids']['watched']
    if mutation not in ('remove-input', 'change-input'):
        with configured.open() as run:
            run.import_events([high])
    with configured.open() as run:
        events = [high, low] if mutation in ('remove-input', 'change-input') else [low]
        offer = run.inspect_twins(events)
        assert len(offer.pairs) == 1
        row_id = build_media_record(high, destination)['id']
        if mutation == 'delete':
            del configured.rows[destination, row_id]
        if mutation in ('confidence', 'fingerprint'):
            row = configured.rows[destination, row_id]
            if mutation == 'confidence':
                row['timestamp_confidence'] = 'low'
            else:
                row['external_ids']['content_fingerprint'] = 'changed'
        if mutation == 'replace':
            replacement = replace(high, deterministic_id=high.deterministic_id + '-replacement')
            row = build_media_record(replacement, destination)
            configured.rows[destination, row['id']] = row
            MediaTwinIndex().record(run.session.account_id, destination, [row])
        if mutation == 'new-pair':
            events += list(pair(1))
        if mutation == 'remove-input':
            events = [low]
        if mutation == 'change-input':
            events[0] = replace(high, timestamp_confidence='low')
        result = run.import_events(events, twin_policy='auto-discard', approved_twins=offer)
        assert result.skipped_existing == (1 if mutation == 'new-pair' else 0)
        assert result.posted == (2 if mutation == 'new-pair' else len(events))


@pytest.mark.parametrize('bad', ['cross-run', 'binding', 'account', 'destination', 'category', 'origin', 'duplicate', 'type', 'keep'])
def test_invalid_approval_before_effects(configured, bad):
    events = list(pair())
    with configured.open() as run:
        offer = run.inspect_twins(events)
    with configured.open() as run:
        current = run.inspect_twins(events)
        if bad != 'cross-run':
            offer = current
        if bad == 'binding':
            offer = replace(offer, binding=object())
        if bad in ('account', 'destination', 'category', 'origin'):
            value = {'destination': 'Event/99999999-2222-4333-8444-555555555555'}.get(bad, 'wrong')
            offer = replace(offer, pairs=(replace(offer.pairs[0], **{bad: value}),))
        if bad == 'duplicate':
            offer = replace(offer, pairs=offer.pairs * 2)
        if bad == 'type':
            offer = {'pairs': offer.pairs}
        with pytest.raises((ValueError, RuntimeError)):
            run.import_events(events, twin_policy='keep' if bad == 'keep' else 'auto-discard', approved_twins=offer)
    assert configured.posts == []


def test_immutable_empty_approval_and_config_unchanged(configured):
    events = list(pair())
    before = deepcopy(configured.settings)
    with configured.open() as run:
        offer = run.inspect_twins(events)
        with pytest.raises((FrozenInstanceError, AttributeError)):
            offer.pairs[0].high = 'changed'
        with pytest.raises(TypeError):
            vars(offer)
        empty = replace(offer, pairs=())
        assert run.import_events(events, twin_policy='auto-discard', approved_twins=empty).posted == 2
        assert run.ctx.config.get('twin_policy') is None
    assert configured.settings == before


@pytest.mark.parametrize('preview', [False, True])
def test_excluded_current_input_high_never_suppresses(configured, preview):
    from fulcra_common.v1 import build_event_record
    from fulcra_media.v1 import build_media_record
    high, low = pair()
    destination = configured.settings['v1_destination_ids']['watched']
    identifier = build_media_record(high, destination)['id']
    control = 'Event/aaaaaaaa-2222-4333-8444-555555555555'
    exclusion = build_event_record(data_type=control,
        source_id=f'collect:v1:excluded:{destination}:{identifier}', start_time=high.start_time,
        fields={'schema_version': 1, 'target_data_type': destination,
                'target_record_id': identifier, 'action_id': configured.settings['account_id'],
                'reason': 'undo', 'evidence': []})
    configured.rows[control, exclusion['id']] = exclusion
    with configured.open(check_only=preview) as run:
        assert not run.inspect_twins([high, low]).pairs
        result = run.import_events([high, low], twin_policy='auto-discard')
    assert result.posted == 1 and result.skipped_existing == 1
    assert result.verified == (0 if preview else 1)


@pytest.mark.parametrize('policy', ['drop', 'keep', 'sentinel:2015', 'ask'])
def test_cluster_payload_parity_and_writer_selected_total(route, monkeypatch, policy):
    from fulcra_csv import ClusterPolicy, apply_cluster_policy
    from fulcra_media.v1 import build_media_record
    # Same watched_at, same content, distinct history IDs preserve stable order.
    route['items'] = [{'id': i, 'watched_at': '2026-01-02T00:00:00Z', 'action': 'scrobble',
        'type': 'movie', 'movie': {'title': 'Synthetic film', 'year': 2020,
            'runtime': 94, 'ids': {'trakt': 1, 'imdb': 'tt0000001'}}} for i in range(1, 6)]
    expected = list(route['normalize_actual'](route['items'], cluster_threshold=5))
    action = 'sentinel' if policy in ('ask', 'sentinel:2015') else policy
    expected = apply_cluster_policy(expected, ClusterPolicy(action=action, sentinel_year=2015,
                                                           cluster_size_threshold=5))
    route['real_normalize'] = True
    route['tty'] = policy == 'ask'
    answers = iter(['sentinel', 2015])
    monkeypatch.setattr(click, 'prompt', lambda *a, **kw: next(answers))
    result, data = route['invoke']('--clusters', policy, '--twin-policy', 'keep', json_mode=policy != 'ask')
    assert result.exit_code == 0, result.output
    if data:
        assert data['total'] == len(expected)
    posts = route['configured'].posts
    assert len(posts) == len(expected)
    destination = route['configured'].settings['v1_destination_ids']['watched']
    assert posts == [build_media_record(e, destination, tags=posts[i]['tags']) for i, e in enumerate(expected)]
    assert all('source_payload' not in p['external_ids'] for p in posts)


def test_omitted_clusters_errors_only_when_present(route):
    high, low = pair()
    route['events'] = [replace(low, external_ids={**low.external_ids, 'timestamp_cluster_size': 5})]
    result, data = route['invoke']()
    assert result.exit_code == 2 and data['errors'][0]['stage'] == 'args'
    assert route['configured'].posts == []


@pytest.mark.parametrize('mode', ['unknown', 'reject', 'empty', 'drop', 'fetch-fail'])
def test_pending_precedence_original_bytes_and_option_changes(route, mode):
    from fulcra_collect import db
    from fulcra_media import media_config
    configured = route['configured']
    high, low = pair()
    configured.state['mode'] = 'unknown'
    with pytest.raises(Exception):
        with configured.open() as run:
            run.import_events([high])
    conn = db.open(configured.profile / 'state.db')
    before = deepcopy(db.get_record_outbox(conn, 'cli-media', 'v1_media_pending'))
    raw = json.dumps(before, sort_keys=True)
    saved = (configured.profile / media_config.FILENAME).read_bytes()
    if mode in ('unknown', 'reject'):
        configured.state['mode'] = mode
    else:
        configured.state['mode'] = 'ok'
        destination = configured.settings['v1_destination_ids']['watched']
        configured.rows.update({(destination, p['id']): deepcopy(p) for p in configured.posts})
        def recovered():
            assert db.get_record_outbox(conn, 'cli-media', 'v1_media_pending') == {'groups': []}
        route['before_auth'] = recovered
        if mode == 'drop':
            route['events'] = [replace(low, external_ids={**low.external_ids, 'timestamp_cluster_size': 5})]
        if mode == 'fetch-fail':
            route['fail'] = 'fetch'
    result, data = route['invoke']('--clusters', 'drop', '--twin-policy', 'auto-discard')
    after = db.get_record_outbox(conn, 'cli-media', 'v1_media_pending')
    if mode in ('unknown', 'reject'):
        assert result.exit_code == 2 and route['calls'] == []
        assert json.dumps(after, sort_keys=True) == raw
    else:
        assert route['calls'][0] == 'auth'
        assert after == {'groups': []}
        assert data['total'] == data['posted'] == data['verified'] == 0
    assert (configured.profile / media_config.FILENAME).read_bytes() == saved


@pytest.mark.parametrize('failure', ['chunk', 'index', 'cleanup'])
def test_cli_failure_preserves_actual_verified_counts(route, monkeypatch, failure):
    from fulcra_media.collect_v1 import MediaRecordWriter
    from fulcra_media.collect_twins import MediaTwinIndex
    from fulcra_media import media_config
    configured = route['configured']
    route['events'] = [pair(n)[0] for n in range(2)]
    if failure == 'chunk':
        actual = MediaRecordWriter.run_import
        def small(self, events, **kw):
            return actual(self, events, **{**kw, 'chunk_size': 1})
        monkeypatch.setattr(MediaRecordWriter, 'run_import', small)
        configured.state['unknown_after'] = 1
    if failure == 'index':
        def fail(*a, **kw):
            raise RuntimeError('SECRET https://private.invalid/')
        monkeypatch.setattr(MediaTwinIndex, 'record', fail)
    if failure == 'cleanup':
        @contextmanager
        def cleanup(*a, **kw):
            with configured.open(**kw) as run:
                yield run
            raise RuntimeError('SECRET https://private.invalid/')
        monkeypatch.setattr(media_config, 'configured_media', cleanup)
    result, data = route['invoke']('--twin-policy', 'keep')
    assert result.exit_code == 2, data
    assert data['posted'] == data['verified'] == (1 if failure == 'chunk' else 2)
    assert data['total'] == 2 and data['errors'][0]['stage'] == 'import'
    assert 'SECRET' not in result.output and 'https://' not in result.output


@pytest.mark.parametrize('change', ['missing', 'low', 'fingerprint', 'wrong-account', 'wrong-destination', 'corrupt', 'excluded'])
def test_actual_index_hints_require_owned_fresh_readback(configured, change):
    from fulcra_media.collect_twins import MediaTwinIndex
    from fulcra_media.v1 import build_media_record
    from fulcra_common.v1 import build_event_record
    high, low = pair()
    destination = configured.settings['v1_destination_ids']['watched']
    with configured.open() as run:
        run.import_events([high])
    row_id = build_media_record(high, destination)['id']
    row = configured.rows[destination, row_id]
    if change == 'missing':
        del configured.rows[destination, row_id]
    if change == 'low':
        row['timestamp_confidence'] = 'low'
    if change == 'fingerprint':
        row['external_ids']['content_fingerprint'] = 'wrong'
    if change in ('wrong-account', 'wrong-destination'):
        MediaTwinIndex().path.unlink()
        MediaTwinIndex().record('other' if change == 'wrong-account' else configured.settings['account_id'],
            configured.settings['v1_destination_ids']['read'] if change == 'wrong-destination' else destination, [row])
    if change == 'corrupt':
        MediaTwinIndex().path.write_bytes(b'not sqlite')
    if change == 'excluded':
        control = 'Event/aaaaaaaa-2222-4333-8444-555555555555'
        exclusion = build_event_record(data_type=control,
            source_id=f'collect:v1:excluded:{destination}:{row_id}', start_time=high.start_time,
            fields={'schema_version': 1, 'target_data_type': destination, 'target_record_id': row_id,
                'action_id': configured.settings['account_id'], 'reason': 'undo', 'evidence': []})
        configured.rows[control, exclusion['id']] = exclusion
    with configured.open(check_only=True) as run:
        if change == 'corrupt':
            with pytest.raises(Exception):
                run.inspect_twins([low])
            with pytest.raises(Exception):
                run.import_events([low], twin_policy='auto-discard')
        else:
            assert run.inspect_twins([low]).pairs == ()
            assert run.import_events([low], twin_policy='auto-discard').posted == 1
    assert len(configured.posts) == 1


@pytest.mark.parametrize('policy', ['keep', 'auto-discard', None])
def test_preview_no_delivery_index_or_authority_writes(route, monkeypatch, policy):
    from fulcra_media.collect_twins import MediaTwinIndex
    from fulcra_media.collect_v1 import MediaRecordWriter
    from fulcra_collect.v1_outbox import V1RecordOutbox
    configured = route['configured']
    route['events'] = list(pair())
    saved = {p.name: p.read_bytes() for p in configured.profile.iterdir() if p.suffix == '.json'}
    def forbidden(*a, **kw):
        pytest.fail('preview write')
    monkeypatch.setattr(MediaRecordWriter, 'resume', forbidden)
    monkeypatch.setattr(V1RecordOutbox, 'stage', forbidden)
    monkeypatch.setattr(V1RecordOutbox, 'drain', forbidden)
    monkeypatch.setattr(MediaTwinIndex, 'record', forbidden)
    args = [] if policy is None else ['--twin-policy', policy]
    result, data = route['invoke']('--check-only', *args)
    assert result.exit_code == 0, data
    assert data['would_post'] == (1 if policy == 'auto-discard' else 2)
    assert data['posted'] == data['would_post'] and data['verified'] == 0
    assert configured.posts == [] and not MediaTwinIndex().path.exists()
    assert saved == {p.name: p.read_bytes() for p in configured.profile.iterdir() if p.suffix == '.json'}


def test_existing_owned_prompt_and_exclusion_race(route, monkeypatch):
    from fulcra_media.v1 import build_media_record
    from fulcra_common.v1 import build_event_record
    high, low = pair()
    configured = route['configured']
    destination = configured.settings['v1_destination_ids']['watched']
    with configured.open() as run:
        run.import_events([high])
    # This scenario exercises retained native exclusion enforcement too.
    # Extend the synthetic server with real tombstone/absence behavior.
    import httpx
    transport = run.session.authority._transport
    actual_handler = transport.handler
    tombstones = []
    def handle(request):
        if request.url.path == '/data/v1/catalog/DeletedRecord/v1/schema':
            return httpx.Response(200, json={'type': 'object', 'properties':
                {k: {'type': 'string'} for k in ('id', 'record_id', 'data_type')},
                'required': ['id', 'record_id', 'data_type']})
        if request.url.path == '/ingest/v1/record/DeletedRecord':
            batch = [json.loads(line) for line in request.content.splitlines()]
            tombstones.extend(batch)
            for row in batch:
                configured.rows.pop((destination, row['record_id']), None)
            return httpx.Response(200, json={'upload_id': 'synthetic-deletion'})
        return actual_handler(request)
    transport.handler = handle
    route['events'] = [low]
    route['tty'] = True
    def prompt(*a, **kw):
        identifier = build_media_record(high, destination)['id']
        control = 'Event/aaaaaaaa-2222-4333-8444-555555555555'
        exclusion = build_event_record(data_type=control,
            source_id=f'collect:v1:excluded:{destination}:{identifier}', start_time=high.start_time,
            fields={'schema_version': 1, 'target_data_type': destination,
                'target_record_id': identifier, 'action_id': configured.settings['account_id'],
                'reason': 'undo', 'evidence': []})
        configured.rows[control, exclusion['id']] = exclusion
        return 'yes'
    monkeypatch.setattr(click, 'prompt', prompt)
    result, _ = route['invoke'](json_mode=False)
    assert result.exit_code == 0, result.output
    assert 'existing-owned-record' in result.output
    assert len(configured.posts) == 2  # excluded offered high cannot suppress low
    assert len(tombstones) == 1


def test_actual_partial_multidestination_observed_lower_bound(configured):
    from fulcra_media.cli_common import media_failure_envelope
    high = pair()[0]
    second = replace(pair(1)[0], category='read')
    configured.state['unknown_after'] = 1
    run = None
    with pytest.raises(Exception):
        with configured.open() as run:
            run.import_events([high, second], twin_policy='keep')
    envelope = media_failure_envelope('trakt', stage='import', message='fixed', run=run)
    assert envelope.total == 2
    assert envelope.posted == envelope.verified == 1


def test_none_preserves_config_and_default_policy(configured):
    from fulcra_media.standalone_v1 import open_media
    # Direct actual runtime permits testing plugin-compatible config precedence;
    # the standalone persisted authority intentionally never adds twin_policy.
    with configured.open() as seed:
        client = seed.session.authority
    settings = {'v1_destination_ids': configured.settings['v1_destination_ids'], 'twin_policy': 'keep'}
    with open_media(settings, client=client) as run:
        run.writer.default_twin_policy = 'auto-discard'
        assert run.import_events(list(pair()), twin_policy=None).posted == 2
    settings['twin_policy'] = 'ask'
    with open_media(settings, client=client) as run:
        with pytest.raises(ValueError, match='headless'):
            run.import_events([], twin_policy=None)


def test_file_auth_and_actual_source_transport_after_configured_recovery(configured, monkeypatch):
    from types import SimpleNamespace
    import httpx
    from fulcra_media import media_config
    from fulcra_media.importers import trakt
    from fulcra_media.cli import cli
    creds = configured.tmp / 'synthetic-trakt.json'
    creds.write_text(json.dumps({'client_id': 'synthetic-id', 'client_secret': 'SECRET',
        'access_token': 'synthetic-token', 'refresh_token': 'synthetic-refresh',
        'created_at': 4102444800, 'expires_in': 86400}))
    monkeypatch.setattr(trakt, 'CREDS_PATH', creds)
    calls = []
    def source(req):
        if req.url.path == '/users/settings':
            return httpx.Response(200, stream=httpx.ByteStream(json.dumps({'user': {'ids': {'uuid': 'Synthetic opaque identity'}}}).encode()))
        calls.append(req)
        assert req.headers['Authorization'] == 'Bearer synthetic-token'
        assert req.headers['trakt-api-key'] == 'synthetic-id'
        return httpx.Response(200, headers={'X-Pagination-Page': '1',
            'X-Pagination-Limit': '250', 'X-Pagination-Item-Count': '1',
            'X-Pagination-Page-Count': '1'}, stream=httpx.ByteStream(json.dumps([{'id': 123, 'watched_at': '2026-01-02T00:00:00Z',
            'action': 'scrobble', 'type': 'movie', 'movie': {'title': 'Synthetic',
            'year': 2020, 'runtime': 80, 'ids': {'trakt': 1}}}]).encode()))
    monkeypatch.setattr(trakt, 'httpx', SimpleNamespace(Client=lambda **kw:
        httpx.Client(**{**kw, "transport": httpx.MockTransport(source)}), HTTPStatusError=httpx.HTTPStatusError))
    from fulcra_collect import trakt_identity
    monkeypatch.setattr(trakt_identity, 'httpx', trakt.httpx)
    monkeypatch.setattr(media_config, 'configured_media', lambda *a, **kw: configured.open(**kw))
    result = CliRunner().invoke(cli, ['import', 'trakt', '--json', '--twin-policy', 'keep'])
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data['posted'] == data['verified'] == 1 and data['cursor'] is None
    assert len(calls) == 1 and len(configured.posts) == 1
    assert 'SECRET' not in result.output and 'synthetic-token' not in result.output


def test_inspection_and_filter_share_stable_high_precedence(configured):
    first, low = pair()
    earlier = replace(first, deterministic_id=first.deterministic_id + '-earlier',
                      start_time=first.start_time - timedelta(days=1),
                      end_time=first.end_time - timedelta(days=1))
    with configured.open() as run:
        offer = run.inspect_twins([first, earlier, low])
        assert offer.pairs[0].high == 'watched:' + earlier.deterministic_id
        result = run.import_events([first, earlier, low], twin_policy='auto-discard', approved_twins=offer)
    assert result.skipped_existing == 1 and result.posted == 2


def test_provider_unauthorized_classifies_auth_without_leak(route, monkeypatch):
    import httpx
    from fulcra_media.importers import trakt
    def unauthorized(*a, **kw):
        response = httpx.Response(401, request=httpx.Request('GET', 'https://private.invalid/SECRET'))
        response.raise_for_status()
    monkeypatch.setattr(trakt, 'fetch_history_with_headers', unauthorized)
    result, data = route['invoke']()
    assert result.exit_code == 2 and data['errors'][0]['stage'] == 'auth'
    assert 'SECRET' not in result.output and 'https://' not in result.output


@pytest.mark.parametrize('change', ['account', 'destination', 'category'])
def test_prompt_authority_or_input_scope_change_fails_before_new_delivery(route, monkeypatch, change):
    from fulcra_media import media_config
    configured = route['configured']
    route['events'] = list(pair())
    route['tty'] = True
    def prompt(*a, **kw):
        if change == 'account':
            configured.state['account'] = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'
        elif change == 'destination':
            path = configured.profile / media_config.FILENAME
            saved = json.loads(path.read_text())
            saved['v1_destination_ids']['watched'] = saved['v1_destination_ids']['read']
            path.write_text(json.dumps(saved))
        else:
            # NormalizedEvent is mutable: approval must not retain its authority.
            route['events'][1].category = 'read'
        return 'yes'
    monkeypatch.setattr(click, 'prompt', prompt)
    result, _ = route['invoke'](json_mode=False)
    assert result.exit_code == 2, result.output
    assert configured.posts == []


def test_legacy_cache_never_authority_and_same_id_update_preserves_enrichment(route, monkeypatch):
    from fulcra_media import twin_cache
    configured = route['configured']
    monkeypatch.setattr(twin_cache, 'load_for_twin_lookup', lambda: pytest.fail('legacy authority'))
    high, low = pair()
    route['events'] = [high]
    result, data = route['invoke']('--twin-policy', 'keep')
    assert result.exit_code == 0 and data['posted'] == 1
    destination = configured.settings['v1_destination_ids']['watched']
    row = next(r for (d, _), r in configured.rows.items() if d == destination)
    row['external_ids']['retained_enrichment'] = {'synthetic': 'value'}
    route['events'] = [replace(high, note='Changed source note')]
    result, data = route['invoke']('--twin-policy', 'keep')
    assert result.exit_code == 0 and data['posted'] == 1
    assert configured.posts[-1]['external_ids']['retained_enrichment'] == {'synthetic': 'value'}
    assert configured.posts[-1]['id'] == configured.posts[0]['id']
    route['events'] = [low]
    result, data = route['invoke']('--twin-policy', 'keep')
    assert result.exit_code == 0 and data['posted'] == 1


def test_keyboard_interrupt_not_reclassified_and_lease_released(route, monkeypatch):
    from fulcra_media.importers import trakt
    def interrupt(*a, **kw):
        raise KeyboardInterrupt()
    monkeypatch.setattr(trakt, 'normalize_history', interrupt)
    result, _ = route['invoke'](json_mode=False)
    assert result.exit_code == 1
    assert 'Invalid or cancelled' not in result.output
    assert not route['configured'].posts
    with route['configured'].open():
        pass
