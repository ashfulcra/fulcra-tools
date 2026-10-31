"""Lower-bound evidence through the real standalone runtime and synthetic HTTP."""
import base64
from copy import deepcopy
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
import json
import re
from types import SimpleNamespace

import httpx
import pytest

from fulcra_common.client import BaseFulcraClient
from fulcra_common.data_types import CreationJournal
from fulcra_common.record_exclusions import EXCLUSION_SPEC
from fulcra_media.importers.base import NormalizedEvent
from fulcra_media.standalone_v1 import open_media
from fulcra_media.v1 import media_spec

ACCOUNT = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
DEST = 'Event/11111111-2222-4333-8444-555555555555'
OTHER = 'Event/22222222-2222-4333-8444-555555555555'
CONTROL = 'Event/aaaaaaaa-2222-4333-8444-555555555555'
TAG = '11111111-1111-4111-8111-111111111111'


def counters(value):
    return tuple(getattr(value, key) for key in ('total', 'skipped_existing', 'posted', 'verified'))


def sample(number, category='watched'):
    start = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=number)
    return NormalizedEvent(importer='synthetic', service='synthetic', category=category,
        note='Synthetic', title='Synthetic', start_time=start, end_time=start + timedelta(seconds=30),
        deterministic_id=f'com.fulcra.media.synthetic.v1.{number}', timestamp_confidence='high',
        external_ids={})


@pytest.fixture
def world(tmp_path, monkeypatch):
    profile = tmp_path / 'profile'
    profile.mkdir()
    claims = base64.urlsafe_b64encode(json.dumps({'fulcradynamics.com/userid': ACCOUNT,
        'exp': 4102444800}).encode()).decode().rstrip('=')
    monkeypatch.setenv('FULCRA_COLLECT_HOME', str(profile))
    monkeypatch.setenv('FULCRA_ACCESS_TOKEN', 'synthetic.' + claims + '.signature')
    monkeypatch.setattr(httpx.HTTPTransport, 'handle_request', lambda *_: pytest.fail('external HTTP'))
    CreationJournal(profile / 'v1-type-creations.sqlite').resolved(ACCOUNT, EXCLUSION_SPEC, CONTROL)
    state = SimpleNamespace(rows={}, posts=[], reads=[], fail_post=None, partial_post=None,
                            after_post=lambda: None)
    def handle(request):
        path = request.url.path
        if path == '/user/v1alpha1/info':
            return httpx.Response(200, json={'userid': ACCOUNT})
        if path == '/input/v1/data_type':
            return httpx.Response(200, json=[{'id': full, 'fulcra_userid': ACCOUNT,
                'name': name, 'api_version': 'v1', 'recordable': True,
                'deprecated': False, 'record_spec': {'type': 'event'}}
                for full, name in ((DEST, 'Watched'), (OTHER, 'Listened'), (CONTROL, EXCLUSION_SPEC.name))])
        if path.endswith('/schema'):
            spec = EXCLUSION_SPEC if CONTROL in path else media_spec('listened' if OTHER in path else 'watched')
            return httpx.Response(200, json={'type': 'object', 'properties': {
                **{k: {} for k in ('id', 'sources', 'tags', 'start_time', 'end_time')},
                **spec.fields}, 'required': list(spec.required_fields)})
        if '/tag/name/' in path or '/tag/id/' in path:
            return httpx.Response(200, json={'id': TAG, 'name': 'synthetic', 'fulcra_userid': ACCOUNT})
        if path == '/data/v1/records':
            query = request.url.params['q']
            full = re.search('__name__="([^"]+)"', query).group(1)
            ids = re.findall('id="([^"]+)"', query)
            selected = [deepcopy(state.rows[full, i]) for i in ids if (full, i) in state.rows]
            state.reads.append((full, tuple(ids), selected))
            return httpx.Response(200, text='\n'.join(json.dumps(row) for row in selected))
        if path.startswith('/ingest/v1/record/'):
            full = path.removeprefix('/ingest/v1/record/')
            batch = [json.loads(line) for line in request.content.splitlines()]
            state.posts.append((full, batch))
            if state.fail_post == len(state.posts):
                raise KeyboardInterrupt('synthetic interrupt')
            visible = batch[:1] if state.partial_post == len(state.posts) else batch
            state.rows.update({(full, row['id']): row for row in visible})
            state.after_post()
            return httpx.Response(200, json={'upload_id': 'synthetic'})
        pytest.fail('unexpected path ' + path)
    state.client = BaseFulcraClient(base_url='https://example.test', transport=httpx.MockTransport(handle))
    state.settings = {'v1_destination_ids': {'watched': DEST, 'listened': OTHER}}
    return state


def test_snapshot_success_reset_preview_and_generator(world):
    with open_media(world.settings, client=world.client) as run:
        assert run.progress is None
        result = run.import_events([sample(1), sample(1), sample(2)], chunk_size=1)
        saved = run.progress
        assert counters(saved) == counters(result) == (3, 1, 2, 2)
        with pytest.raises(FrozenInstanceError):
            saved.posted = 99
        result.posted = 99
        assert saved.posted == 2
        replay = run.import_events([sample(1), sample(2)])
        assert counters(replay) == counters(run.progress) == (2, 2, 0, 0)
        with pytest.raises(ValueError):
            run.import_events([], chunk_size=0)
        assert counters(run.progress) == (0, 0, 0, 0)
        def broken():
            yield sample(3)
            raise LookupError('source generator')
        with pytest.raises(LookupError):
            run.import_events(broken())
        assert counters(run.progress) == (0, 0, 0, 0)
        assert counters(run.import_events([])) == counters(run.progress) == (0, 0, 0, 0)
    assert counters(saved) == (3, 1, 2, 2)
    with open_media(world.settings, client=world.client, check_only=True) as run:
        result = run.import_events([sample(1), sample(4)])
        assert counters(run.progress) == counters(result) == (2, 1, 1, 0)
    assert len(world.posts) == 2


def test_partial_chunk_retains_only_complete_group_and_never_reissues(world):
    world.partial_post = 2
    with open_media(world.settings, client=world.client) as run:
        with pytest.raises(RuntimeError):
            run.import_events([sample(i) for i in range(4)], chunk_size=2)
        assert counters(run.progress) == (4, 0, 2, 2)
        assert run.writer.outbox.progress.verified == 0
        original = run.ctx.outbox_get('v1_media_pending')
        assert len(original['groups'][0]['records']) == 2
        assert original['dispatch'][DEST]['phase'] == 'issued'
        assert len(original['dispatch'][DEST]['positive_ids']) == 1
        with pytest.raises(RuntimeError):
            run.writer.resume()
        assert run.ctx.outbox_get('v1_media_pending') == original
        assert len(world.posts) == 2
        assert any(len(rows) == 1 for dest, ids, rows in world.reads if dest == DEST and len(ids) == 2)
        # Completing recovery is not evidence for the next source invocation.
        for row in original['groups'][0]['records']:
            world.rows[DEST, row['id']] = row
        assert run.writer.resume() == 2
        assert counters(run.progress) == (4, 0, 2, 2)
        assert counters(run.import_events([])) == counters(run.progress) == (0, 0, 0, 0)


@pytest.mark.parametrize('failure', ['annotation', 'clear', 'index', 'interrupt'])
def test_finish_and_index_failure_keep_evidence_and_exception(world, monkeypatch, failure):
    from fulcra_media.collect_twins import MediaTwinIndex
    with open_media(world.settings, client=world.client) as run:
        def fail(*args, **kwargs):
            raise SystemExit('synthetic finish')
        if failure == 'annotation':
            monkeypatch.setattr(run.ctx, 'annotation', fail)
        elif failure == 'clear':
            original_set = run.writer.outbox._set
            def set_value(key, value):
                if value == {'groups': []}:
                    fail()
                original_set(key, value)
            run.writer.outbox._set = set_value
        elif failure == 'index':
            monkeypatch.setattr(MediaTwinIndex, 'record', fail)
        else:
            world.fail_post = 2
        expected = KeyboardInterrupt if failure == 'interrupt' else SystemExit
        with pytest.raises(expected):
            run.import_events([sample(1), sample(2)], chunk_size=1)
        assert counters(run.progress) == (2, 0, 1, 1)
        pending = run.ctx.outbox_get('v1_media_pending')
        assert bool(pending['groups']) == (failure != 'index')
    # A fresh context can acquire the released lease after BaseException.
    with open_media(world.settings, client=world.client, check_only=True):
        pass


def test_multi_destination_partial_exclusions_and_no_failed_index(world, monkeypatch):
    from fulcra_common.v1 import build_event_record
    from fulcra_media.collect_twins import MediaTwinIndex
    indexed = []
    monkeypatch.setattr(MediaTwinIndex, 'record', lambda *args: indexed.append(args))
    with open_media(world.settings, client=world.client) as run:
        stage = run.writer.outbox.stage
        def stage_with_late_exclusion(records, **kwargs):
            stage(records, **kwargs)
            target = records[DEST][-1]
            exclusion = build_event_record(data_type=CONTROL,
                source_id=f"collect:v1:excluded:{DEST}:{target['id']}",
                start_time=datetime(2026, 1, 1, tzinfo=timezone.utc),
                fields={'schema_version': 1, 'target_data_type': DEST,
                    'target_record_id': target['id'], 'action_id': ACCOUNT,
                    'reason': 'undo', 'evidence': []})
            world.rows[CONTROL, exclusion['id']] = exclusion
        run.writer.outbox.stage = stage_with_late_exclusion
        world.partial_post = 2
        with pytest.raises(RuntimeError):
            run.import_events([sample(1), sample(2), sample(3), sample(4, 'listened'), sample(5, 'listened')])
        assert counters(run.progress) == (5, 1, 2, 2)
        assert (run.writer.outbox.progress.verified, run.writer.outbox.progress.excluded) == (2, 1)
        with pytest.raises(FrozenInstanceError):
            run.writer.outbox.progress.verified = 0
        assert not indexed
        pending = run.ctx.outbox_get('v1_media_pending')
        assert [len(g['records']) for g in pending['groups']] == [3, 2]
        assert pending['dispatch'][OTHER]['phase'] == 'issued'
        # Existing public envelope can carry the lower bound without success.
        from fulcra_media.cli_common import ImportEnvelope
        from dataclasses import asdict
        envelope = ImportEnvelope('synthetic', ok=False, **asdict(run.progress))
        assert not envelope.ok and envelope.posted == envelope.verified == 2
        assert envelope.new_watermark is None


@pytest.mark.parametrize('boundary', ['stage', 'drain-entry'])
def test_recovered_drain_evidence_never_leaks_to_failed_current_chunk(world, monkeypatch, boundary):
    from contextlib import contextmanager
    with open_media(world.settings, client=world.client) as run:
        from fulcra_media.v1 import build_media_record
        recovered = [build_media_record(sample(i), DEST, tags=(TAG,)) for i in range(7)]
        run.writer.outbox.stage({DEST: recovered}, message='synthetic recovery')
        if boundary == 'stage':
            def fail(*args, **kwargs):
                raise LookupError('failed stage')
            monkeypatch.setattr(run.writer.outbox, 'stage', fail)
        else:
            stage = run.writer.outbox.stage
            def stage_then_hold(*args, **kwargs):
                stage(*args, **kwargs)
                @contextmanager
                def hold():
                    raise LookupError('entry authority hold')
                    yield
                monkeypatch.setattr(run.session, 'record_write_scope', hold)
            monkeypatch.setattr(run.writer.outbox, 'stage', stage_then_hold)
        with pytest.raises(LookupError):
            run.import_events([sample(10)])
        assert counters(run.progress) == (1, 0, 0, 0)
        if boundary == 'drain-entry':
            assert run.writer.outbox.progress.verified == run.writer.outbox.progress.excluded == 0
        assert len(world.posts) == 1


def test_context_exit_failure_retains_success_and_snapshot_is_pure_memory(world, monkeypatch):
    from fulcra_collect.v1_session import CollectRecordSession
    close = CollectRecordSession.close
    def close_then_fail(self):
        close(self)
        raise RuntimeError('synthetic close')
    monkeypatch.setattr(CollectRecordSession, 'close', close_then_fail)
    run = result = None
    with pytest.raises(RuntimeError, match='synthetic close'):
        with open_media(world.settings, client=world.client) as run:
            result = run.import_events([sample(1)])
    assert counters(run.progress) == counters(result) == (1, 0, 1, 1)
    world.client._transport = None
    monkeypatch.setattr(run.session, 'commit', lambda *_: pytest.fail('snapshot consulted session'))
    assert counters(run.progress) == (1, 0, 1, 1)


def test_source_failure_after_recovery_has_no_invocation_evidence(world):
    from fulcra_media.v1 import build_media_record
    records = [build_media_record(sample(i), DEST, tags=(TAG,)) for i in range(7)]
    with open_media(world.settings, client=world.client) as run:
        run.writer.outbox.stage({DEST: records}, message='synthetic recovery')
    saved = None
    with pytest.raises(LookupError):
        with open_media(world.settings, client=world.client) as saved:
            assert saved.writer.outbox.progress.verified == 7
            raise LookupError('source failed')
    assert saved.progress is None
    assert len(world.posts) == 1


@pytest.mark.parametrize('invalid', ['chunk', 'check-only', 'policy', 'preflight'])
def test_validation_resets_before_input_and_preflight_duplicates_publish_only_when_complete(world, invalid):
    from dataclasses import replace
    with open_media(world.settings, client=world.client) as run:
        run.import_events([sample(1)])
        events = [sample(2), sample(2)]
        kwargs = {}
        if invalid == 'chunk':
            kwargs['chunk_size'] = False
        elif invalid == 'check-only':
            kwargs['check_only'] = 1
        elif invalid == 'policy':
            run.ctx.config['twin_policy'] = 'invalid'
        else:
            events.append(replace(sample(2), note='conflicting payload'))
        with pytest.raises(ValueError):
            run.writer.run_import(events, **kwargs)
        assert counters(run.progress) == ((3, 0, 0, 0) if invalid == 'preflight' else (0, 0, 0, 0))
        assert len(world.posts) == 1


def test_successful_whole_input_preflight_duplicates_survive_later_hold(world, monkeypatch):
    with open_media(world.settings, client=world.client) as run:
        def hold(*args, **kwargs):
            raise LookupError('destination unavailable')
        monkeypatch.setattr(run.writer, '_destinations', hold)
        with pytest.raises(LookupError):
            run.import_events([sample(1), sample(1)])
        assert counters(run.progress) == (2, 1, 0, 0)
        assert not world.posts


def test_identical_uuid_across_real_destinations_counts_each_destination(world):
    from fulcra_media.v1 import build_media_record
    row = build_media_record(sample(1), DEST, tags=(TAG,))
    with open_media(world.settings, client=world.client) as run:
        run.writer.outbox.stage({DEST: [row], OTHER: [row]}, message='synthetic destination identity')
        assert run.writer.outbox.drain() == 2
        assert run.writer.outbox.progress.verified == 2
        assert {(destination, records[0]['id']) for destination, records in world.posts} == {
            (DEST, row['id']), (OTHER, row['id'])}
        assert run.progress is None
