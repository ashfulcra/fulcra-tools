"""Actual configured CLI error adaptation, using offline HTTP and real delivery."""
import json
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from fulcra_media.cli import cli
import test_file_routes_v1 as file_routes
from test_file_routes_v1 import ROUTES, args_for
from test_media_progress import sample

# Register the shared actual-runtime fixture in this module for pytest discovery.
server = file_routes.server

ALL_ROUTES = [('netflix', ['synthetic.csv']), *ROUTES]
PARSERS = {
    'netflix': 'netflix.parse_auto',
    'spotify-extended': 'spotify.parse_extended_zip',
    'spotify-ifttt': 'spotify_ifttt.parse_ifttt_zip',
    'apple-podcasts': 'apple_podcasts.parse_db',
    'apple-podcasts-timemachine': 'apple_podcasts.parse_db',
    'apple-takeout': 'apple_takeout.parse_playback_csv',
    'youtube': 'youtube.parse_takeout_json',
    'generic-csv': 'generic_csv.parse_media_csv',
}


def source(monkeypatch, route, events=None, error=None):
    monkeypatch.setattr('fulcra_media.library.resolve', lambda p: p)
    def parse(*args, **kwargs):
        if error:
            raise error
        return events
    monkeypatch.setattr('fulcra_media.importers.' + PARSERS[route], parse)


def invoke(route='netflix', args=None, *, preview=False, human=False):
    command = args_for(route, args if args is not None else ['synthetic.csv'])
    if preview:
        command.append('--check-only')
    if human:
        command.remove('--json')
    result = CliRunner().invoke(cli, command)
    assert result.exit_code == 2, result.output
    if human:
        return result.output
    assert len(result.stdout.splitlines()) == 1
    body = json.loads(result.stdout)
    assert set(body) == {'importer', 'ok', 'total', 'skipped_existing', 'posted',
                         'verified', 'since_watermark', 'new_watermark', 'would_post', 'errors', 'cursor'}
    assert body['cursor'] is None
    assert body['ok'] is False
    assert body['since_watermark'] is body['new_watermark'] is None
    return body


def counts(body):
    return tuple(body[k] for k in ('total', 'skipped_existing', 'posted', 'verified'))


def fail(*args, **kwargs):
    raise RuntimeError('synthetic boundary')


@pytest.mark.parametrize('route,args', ALL_ROUTES)
def test_eight_routes_retain_actual_delivery_after_index_failure(server, monkeypatch, route, args):
    source(monkeypatch, route, [sample(1), sample(1), sample(2)])
    monkeypatch.setattr('fulcra_media.collect_twins.MediaTwinIndex.record', fail)
    body = invoke(route, args)
    assert counts(body) == (3, 1, 2, 2)
    assert body['would_post'] is None
    assert body['errors'] == [{'stage': 'import', 'message': 'synthetic boundary'}]
    assert len(server[3]) == 1 and len(server[3][0][1]) == 2


@pytest.mark.parametrize('preview', [False, True])
def test_saved_actual_result_survives_context_close(server, monkeypatch, preview):
    from fulcra_collect.v1_session import CollectRecordSession
    close = CollectRecordSession.close
    def close_fail(self):
        close(self)
        fail()
    monkeypatch.setattr(CollectRecordSession, 'close', close_fail)
    source(monkeypatch, 'netflix', [sample(1), sample(1), sample(2)])
    before = (server[0] / 'cli-media-v1.json').read_bytes()
    body = invoke(preview=preview)
    assert counts(body) == (3, 1, 2, 0 if preview else 2)
    assert body['would_post'] == (2 if preview else None)
    assert len(server[3]) == (0 if preview else 1)
    assert (server[0] / 'cli-media-v1.json').read_bytes() == before


def test_source_errors_reset_and_redact_current_invocation(server, monkeypatch):
    source(monkeypatch, 'netflix', [sample(1)])
    monkeypatch.setattr('fulcra_media.collect_twins.MediaTwinIndex.record', fail)
    assert counts(invoke()) == (1, 0, 1, 1)
    source(monkeypatch, 'netflix', error=RuntimeError('https://example.test/?token=PRIVATE'))
    body = invoke()
    assert counts(body) == (0, 0, 0, 0) and body['would_post'] is None
    assert 'PRIVATE' not in json.dumps(body) and 'REDACTED' in body['errors'][0]['message']
    assert len(server[3]) == 1


def test_human_output_keeps_observed_counts(server, monkeypatch):
    source(monkeypatch, 'netflix', [sample(1), sample(2)])
    monkeypatch.setattr('fulcra_media.collect_twins.MediaTwinIndex.record', fail)
    text = invoke(human=True)
    for expected in ('ok=False', 'total=2', 'skipped_existing=0', 'posted=2', 'verified=2', 'synthetic boundary'):
        assert expected in text


def test_helper_copies_validated_snapshot_and_result_precedes_run():
    from fulcra_media.cli_common import media_failure_envelope
    from fulcra_media.fulcra import ImportResult
    result = ImportResult(total=3, skipped_existing=1, posted=2, verified=2)
    body = media_failure_envelope('x', stage='import', message='failure', result=result,
                                  run=SimpleNamespace(progress='malformed'))
    result.posted = 99
    assert body.posted == 2 and not body.ok
    snapshot = SimpleNamespace(total=4, skipped_existing=1, posted=2, verified=2)
    body = media_failure_envelope('x', stage='import', message='failure', run=SimpleNamespace(progress=snapshot))
    snapshot.total = 99
    assert body.total == 4
    for value in (-1, True, 1.5, '1'):
        snapshot.total = value
        with pytest.raises((ValueError, TypeError)):
            media_failure_envelope('x', stage='import', message='failure', run=SimpleNamespace(progress=snapshot))
    snapshot.total = 1
    with pytest.raises(ValueError):
        media_failure_envelope('x', stage='import', message='failure', run=SimpleNamespace(progress=snapshot))


def pending_bytes(profile):
    import sqlite3
    conn = sqlite3.connect(f'file:{profile / "state.db"}?mode=ro', uri=True)
    try:
        return conn.execute("SELECT value_json FROM record_outboxes WHERE key='v1_media_pending'").fetchone()[0]
    finally:
        conn.close()


def stage_original(server):
    from fulcra_media.media_config import configured_media
    from fulcra_media.v1 import build_media_record
    from fulcra_media.cli import STATE_PATH
    with configured_media(STATE_PATH) as run:
        rows = [build_media_record(sample(i), server[1]['watched'], tags=()) for i in range(5)]
        run.writer.outbox.stage({server[1]['watched']: rows}, message='original pending')
    return rows


def test_first_chunk_verified_second_unknown_retains_lower_bound_and_no_repost(server, monkeypatch):
    from fulcra_media.standalone_v1 import StandaloneMediaRun
    from fulcra_media.collect_twins import MediaTwinIndex
    original_import = StandaloneMediaRun.import_events
    monkeypatch.setattr(StandaloneMediaRun, 'import_events',
                        lambda self, events: original_import(self, events, chunk_size=2))
    record = MediaTwinIndex.record
    def record_then_unknown(self, *args, **kwargs):
        result = record(self, *args, **kwargs)
        server[5]['mode'] = 'unknown'
        return result
    monkeypatch.setattr(MediaTwinIndex, 'record', record_then_unknown)
    source(monkeypatch, 'netflix', [sample(i) for i in range(4)])
    body = invoke()
    assert counts(body) == (4, 0, 2, 2)
    assert len(server[3]) == 2
    before = pending_bytes(server[0])
    pending = json.loads(before)
    assert pending['dispatch'][server[1]['watched']]['phase'] == 'issued'
    source(monkeypatch, 'netflix', error=AssertionError('source before recovery'))
    assert counts(invoke()) == (0, 0, 0, 0)
    assert pending_bytes(server[0]) == before
    assert len(server[3]) == 2


def test_old_pending_recovers_then_source_failure_has_zero_current_counts(server, monkeypatch):
    stage_original(server)
    settings = (server[0] / 'cli-media-v1.json').read_bytes()
    source(monkeypatch, 'netflix', error=RuntimeError('new source failed'))
    body = invoke()
    assert counts(body) == (0, 0, 0, 0) and body['would_post'] is None
    assert body['errors'][0]['message'] == 'new source failed'
    assert len(server[3]) == 1 and len(server[3][0][1]) == 5
    assert json.loads(pending_bytes(server[0])) == {'groups': []}
    assert (server[0] / 'cli-media-v1.json').read_bytes() == settings


def test_preview_planning_error_uses_snapshot_without_write_or_pending_change(server, monkeypatch):
    from fulcra_media.standalone_v1 import StandaloneMediaRun
    from fulcra_media.collect_twins import MediaTwinIndex
    stage_original(server)
    before = pending_bytes(server[0])
    settings = (server[0] / 'cli-media-v1.json').read_bytes()
    import sqlite3
    with sqlite3.connect(server[0] / 'state.db') as conn:
        cursors = conn.execute('SELECT * FROM plugin_state').fetchall()
        kv = conn.execute('SELECT * FROM plugin_kv').fetchall()
    original = StandaloneMediaRun.import_events
    def plan_then_fail(self, events):
        original(self, events)
        fail()
    monkeypatch.setattr(StandaloneMediaRun, 'import_events', plan_then_fail)
    monkeypatch.setattr(MediaTwinIndex, 'record', lambda *_: pytest.fail('preview index write'))
    source(monkeypatch, 'netflix', [sample(20), sample(20), sample(21)])
    body = invoke(preview=True)
    assert counts(body) == (3, 1, 2, 0) and body['would_post'] == 2
    assert not server[3]
    assert pending_bytes(server[0]) == before
    assert (server[0] / 'cli-media-v1.json').read_bytes() == settings
    with sqlite3.connect(server[0] / 'state.db') as conn:
        assert conn.execute('SELECT * FROM plugin_state').fetchall() == cursors
        assert conn.execute('SELECT * FROM plugin_kv').fetchall() == kv


def test_preview_source_failure_has_no_plan(server, monkeypatch):
    source(monkeypatch, 'netflix', error=RuntimeError('source failed'))
    body = invoke(preview=True)
    assert counts(body) == (0, 0, 0, 0) and body['would_post'] is None
    assert not server[3]


def test_helper_empty_and_preview_validation():
    from fulcra_media.cli_common import media_failure_envelope
    from dataclasses import asdict
    for run in (None, SimpleNamespace(progress=None)):
        body = media_failure_envelope('x', stage='fetch', message='failed', run=run, check_only=True)
        assert counts(asdict(body)) == (0, 0, 0, 0) and body.would_post is None
    with pytest.raises(ValueError, match='preview'):
        media_failure_envelope('x', stage='import', message='failed', check_only=True,
            run=SimpleNamespace(progress=SimpleNamespace(total=1, skipped_existing=0, posted=1, verified=1)))
    with pytest.raises(AttributeError):
        media_failure_envelope('x', stage='import', message='failed', run=SimpleNamespace(progress=object()))


def test_baseexception_still_propagates_and_releases_context(server, monkeypatch):
    source(monkeypatch, 'netflix', error=KeyboardInterrupt('synthetic interruption'))
    result = CliRunner().invoke(cli, args_for('netflix', ['synthetic.csv']))
    assert result.exit_code == 1 and isinstance(result.exception, SystemExit)
    assert '"ok"' not in result.stdout
    source(monkeypatch, 'netflix', [sample(1)])
    result = CliRunner().invoke(cli, args_for('netflix', ['synthetic.csv']))
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)['verified'] == 1
