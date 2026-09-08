"""Exercise the wizard contract and permission probe with synthetic stores."""
import sqlite3
from types import SimpleNamespace

from fulcra_apple_notes import collect_plugin as plugin


def test_wizard_can_verify_a_real_notes_store(notes_db):
    assert plugin.PLUGIN.permission_check is not None
    result = plugin.PLUGIN.permission_check(SimpleNamespace(config={'container': str(notes_db)}))
    assert result['granted'] is True
    assert 'permission_request' in [s.kind for s in plugin.PLUGIN.setup_steps]


def test_empty_database_is_not_reported_as_notes_access(tmp_path):
    sqlite3.connect(tmp_path / 'NoteStore.sqlite').close()
    result = plugin.permission_check(SimpleNamespace(config={'container': str(tmp_path)}))
    assert result['granted'] is False
    assert result['hint']


def test_missing_store_explains_how_to_get_started(tmp_path):
    result = plugin.permission_check(SimpleNamespace(config={'container': str(tmp_path)}))
    assert result['granted'] is False
    assert 'Notes' in result['hint']


def test_preview_setting_is_usable_without_editing_toml():
    settings = {s.key: s for s in plugin.PLUGIN.required_settings}
    assert settings['dry_run'].kind == 'toggle'
    assert settings['dry_run'].default is False
    assert any('dry_run' in s.settings_keys for s in plugin.PLUGIN.setup_steps)


def test_shared_dry_run_false_does_not_enable_experimental_writes():
    import pytest
    with pytest.raises(RuntimeError, match='separate explicit opt-in'):
        plugin._run_writeback(SimpleNamespace(config={'dry_run': False}))


def test_reconcile_caps_only_the_local_report(monkeypatch):
    from unittest.mock import Mock
    changes = [{'uuid': f'note-{i}', 'status': 'conflict'} for i in range(205)]
    result = {'summary': {'conflict': 205}, 'changes': changes}
    monkeypatch.setattr(plugin, 'run_reconcile', lambda **kwargs: result)
    write = Mock()
    monkeypatch.setattr(plugin.report, 'write', write)
    plugin._run_reconcile(SimpleNamespace(config={}, log=Mock()))
    payload = write.call_args.args[0]
    assert len(payload['changes']) == 200
    assert payload['changes_total'] == 205
    assert payload['changes_truncated'] is True
    assert payload['summary']['conflict'] == 205
    assert len(result['changes']) == 205


def test_writeback_reports_partial_failure_before_raising(monkeypatch):
    import pytest
    from unittest.mock import Mock
    from fulcra_apple_notes import writeback
    from fulcra_apple_notes.reconcile import Change, Status

    changes = [Change(uuid=f'note-{i}', status=Status.VAULT_EDITED) for i in range(205)]
    monkeypatch.setattr(plugin, 'run_reconcile', lambda **kwargs: {
        'complete': True,
        'changes': [dict(uuid=c.uuid, status=c.status.value) for c in changes]})
    monkeypatch.setattr(plugin, '_snapshot_for', lambda *args: ([], []))
    monkeypatch.setattr(writeback, 'probe', lambda: (True, 'available'))
    def plan(all_changes, **kwargs):
        assert len(all_changes) == 205
        return [(c, SimpleNamespace(pk=i)) for i, c in enumerate(all_changes)], []
    monkeypatch.setattr(writeback, 'plan_writeback', plan)
    monkeypatch.setattr(plugin, '_revalidated_body_for', lambda *args, **kwargs: 'synthetic body')
    monkeypatch.setattr(plugin, 'load_state', lambda **kwargs: {})
    responses = iter([SimpleNamespace(ok=True, skipped=False)] * 204 +
                     [SimpleNamespace(ok=False, skipped=False, error='synthetic failure')])
    monkeypatch.setattr(writeback, 'write_note_body', lambda *args, **kwargs: next(responses))
    write = Mock()
    monkeypatch.setattr(plugin.report, 'write', write)
    ctx = SimpleNamespace(config={'writeback_enabled': True, 'dry_run': False}, log=Mock())
    with pytest.raises(RuntimeError, match='writeback.*errors'):
        plugin._run_writeback(ctx)
    write.assert_called_once()
    payload = write.call_args.args[0]
    assert payload['ok'] is False
    assert payload['written'] == 204
    assert payload['candidates'] == 205
    assert len(payload['failures']) == 1
    assert write.call_args.kwargs['path'] == plugin.report.WRITEBACK_PATH


def test_reconcile_failures_leave_mode_specific_failed_reports(monkeypatch):
    import pytest
    from unittest.mock import Mock
    def fail(**kwargs):
        raise RuntimeError('synthetic unreadable vault')
    monkeypatch.setattr(plugin, 'run_reconcile', fail)
    for mode, target in [('reconcile', plugin.report.RECONCILE_PATH),
                         ('writeback', plugin.report.WRITEBACK_PATH)]:
        write = Mock()
        monkeypatch.setattr(plugin.report, 'write', write)
        ctx = SimpleNamespace(config={'mode': mode, 'writeback_enabled': True}, log=Mock())
        with pytest.raises(RuntimeError, match='synthetic unreadable vault'):
            plugin.run(ctx)
        write.assert_called_once()
        assert write.call_args.args[0]['ok'] is False
        assert write.call_args.args[0]['mode'] == mode
        assert write.call_args.kwargs['path'] == target


def test_writeback_never_probes_or_writes_from_an_incomplete_scan(monkeypatch):
    import pytest
    from unittest.mock import Mock
    from fulcra_apple_notes import writeback

    probe = Mock()
    plan = Mock()
    write_note = Mock()
    monkeypatch.setattr(writeback, 'probe', probe)
    monkeypatch.setattr(writeback, 'plan_writeback', plan)
    monkeypatch.setattr(writeback, 'write_note_body', write_note)
    # Missing completeness evidence also fails closed (e.g. a stale result).
    for completeness in ({'complete': False}, {}):
        result = {**completeness, 'notes_remaining': 10,
                  'changes': [{'uuid': 'synthetic-note', 'status': 'vault_edited'}]}
        monkeypatch.setattr(plugin, 'run_reconcile', lambda **kwargs: result)
        write = Mock()
        monkeypatch.setattr(plugin.report, 'write', write)
        ctx = SimpleNamespace(config={'writeback_enabled': True, 'dry_run': False}, log=Mock())
        with pytest.raises(RuntimeError, match='incomplete.*run again'):
            plugin._run_writeback(ctx)
        payload = write.call_args.args[0]
        assert payload['ok'] is False
        assert payload['complete'] is False
        assert payload['notes_remaining'] == 10
        assert payload['written'] == 0
        assert payload['blocked'] == 'reconciliation_incomplete'
    probe.assert_not_called()
    plan.assert_not_called()
    write_note.assert_not_called()


def test_writeback_budget_stop_reports_remaining_without_writing(monkeypatch):
    import pytest
    from unittest.mock import Mock
    from fulcra_apple_notes import writeback
    from fulcra_apple_notes.reconcile import Change, Status
    change = Change(uuid='synthetic-note', status=Status.VAULT_EDITED)
    monkeypatch.setattr(plugin, 'run_reconcile', lambda **kwargs: {'complete': True, 'changes': []})
    monkeypatch.setattr(plugin, 'load_state', lambda **kwargs: {})
    monkeypatch.setattr(plugin, '_snapshot_for', lambda *args: ([], []))
    monkeypatch.setattr(writeback, 'plan_writeback', lambda *args, **kwargs:
                        ([(change, SimpleNamespace(pk=1))], []))
    monkeypatch.setattr(writeback, 'probe', lambda: (True, 'available'))
    now = iter([0, 601, 602])
    monkeypatch.setattr(plugin.time, 'monotonic', lambda: next(now))
    write_note = Mock()
    monkeypatch.setattr(writeback, 'write_note_body', write_note)
    report_write = Mock()
    monkeypatch.setattr(plugin.report, 'write', report_write)
    with pytest.raises(RuntimeError, match='time budget'):
        plugin._run_writeback(SimpleNamespace(
            config={'writeback_enabled': True, 'dry_run': False}, log=Mock()))
    write_note.assert_not_called()
    payload = report_write.call_args.args[0]
    assert payload['ok'] is False
    assert payload['stopped_early'] is True
    assert payload['candidates_remaining'] == 1
    assert payload['written'] == 0


def test_reconcile_report_exposes_saved_partial_progress(monkeypatch):
    from unittest.mock import Mock
    result = {'complete': False, 'stopped_early': True, 'notes_remaining': 20,
              'notes_checked': 10, 'changes': [], 'summary': {'unchanged': 10}}
    monkeypatch.setattr(plugin, 'run_reconcile', lambda **kwargs: result)
    write = Mock()
    monkeypatch.setattr(plugin.report, 'write', write)
    plugin._run_reconcile(SimpleNamespace(config={}, log=Mock()))
    payload = write.call_args.args[0]
    assert payload['ok'] is True  # checkpoint saved successfully, scan incomplete
    assert payload['complete'] is False
    assert payload['notes_remaining'] == 20
    assert payload['stopped_early'] is True


def test_writeback_rechecks_apple_and_vault_after_reconciliation(monkeypatch):
    from datetime import datetime, timezone, timedelta
    from unittest.mock import Mock
    from apple_notes_test_helpers import make_body
    from fulcra_apple_notes import writeback, render, vaultio
    from fulcra_apple_notes.body import decode

    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    baseline_body = make_body('original body', [])
    baseline_hash = render.content_hash(decode(baseline_body).markdown)
    entry = {'path': 'notes/apple/synthetic.md', 'hash': baseline_hash,
             'modified': now.isoformat()}
    state = {'notes': {'synthetic-note': entry}, 'attachments': {}}
    monkeypatch.setattr(plugin, 'load_state', lambda **kwargs: state)
    monkeypatch.setattr(plugin, 'run_reconcile', lambda **kwargs: {
        'complete': True, 'changes': [{'uuid': 'synthetic-note',
                                     'path': entry['path'], 'status': 'vault_edited'}]})
    monkeypatch.setattr(writeback, 'probe', lambda: (True, 'available'))
    write_note = Mock()
    monkeypatch.setattr(writeback, 'write_note_body', write_note)
    fenced_edit = f'{render.OPEN_FENCE}\nvault edit\n{render.CLOSE_FENCE}'
    cases = [
        # Apple content changed during the long scan, even without a new date.
        (make_body('new Apple edit', []), now, fenced_edit),
        # Formatting-only Apple changes must also block replacement.
        (baseline_body, now + timedelta(seconds=1), fenced_edit),
        # A stale vault edit was reverted; do not replace the Apple note.
        (baseline_body, now, f'{render.OPEN_FENCE}\noriginal body\n{render.CLOSE_FENCE}'),
        # Duplicate ownership markers are ambiguous, never a write candidate.
        (baseline_body, now, fenced_edit + '\n' + fenced_edit),
    ]
    for body, modified, vault_text in cases:
        note = SimpleNamespace(uuid='synthetic-note', pk=1, body=body, modified=modified)
        monkeypatch.setattr(plugin, '_snapshot_for', lambda *args: ([note], []))
        monkeypatch.setattr(vaultio, 'read_text', lambda *args, **kwargs: vault_text)
        write = Mock()
        monkeypatch.setattr(plugin.report, 'write', write)
        plugin._run_writeback(SimpleNamespace(
            config={'writeback_enabled': True, 'dry_run': False}, log=Mock()))
        payload = write.call_args.args[0]
        assert payload['written'] == 0
        assert payload['refused_count'] == 1
    write_note.assert_not_called()
