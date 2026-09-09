"""Native controls report conflicts without discarding concurrent edits."""
from unittest.mock import Mock

import pytest

from fulcra_collect import config
from fulcra_menubar import _config_edit


def test_conflicting_save_keeps_current_settings_and_shows_retry(temp_config_home, monkeypatch):
    seed = config.load()
    seed.set_interval('example', 60)
    config.save(seed)
    first, stale = config.load(), config.load()
    first.set_interval('example', 120)
    config.save(first)
    stale.set_interval('example', 180)
    alert = Mock()
    monkeypatch.setattr(_config_edit, '_show_conflict', alert)
    assert _config_edit.save(stale) is False
    assert config.load().interval_overrides['example'] == 120
    alert.assert_called_once_with()


def test_success_needs_no_alert_and_io_errors_are_distinct(temp_config_home, monkeypatch):
    alert = Mock()
    monkeypatch.setattr(_config_edit, '_show_conflict', alert)
    cfg = config.load()
    cfg.enable('example')
    assert _config_edit.save(cfg) is True
    alert.assert_not_called()
    monkeypatch.setattr(config, 'save', Mock(side_effect=OSError('disk unavailable')))
    with pytest.raises(OSError):
        _config_edit.save(cfg)
