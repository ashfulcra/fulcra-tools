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
        'changes': [dict(uuid=c.uuid, status=c.status.value) for c in changes]})
    monkeypatch.setattr(plugin, '_snapshot_for', lambda *args: ([], []))
    monkeypatch.setattr(writeback, 'probe', lambda: (True, 'available'))
    def plan(all_changes, **kwargs):
        assert len(all_changes) == 205
        return [(c, SimpleNamespace(pk=i)) for i, c in enumerate(all_changes)], []
    monkeypatch.setattr(writeback, 'plan_writeback', plan)
    monkeypatch.setattr(plugin, '_vault_body_for', lambda change: 'synthetic body')
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
