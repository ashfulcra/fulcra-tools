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
