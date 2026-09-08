"""Hub config + config directory."""
from __future__ import annotations

import os
import stat
from pathlib import Path

from fulcra_collect import config


def test_config_dir_honours_the_env_override(collect_home: Path):
    assert config.config_dir() == collect_home


def test_config_dir_is_owner_only(collect_home: Path):
    """M2: the config dir holds the control socket and state — 0700 only."""
    d = config.config_dir()
    mode = stat.S_IMODE(os.stat(d).st_mode)
    assert mode == 0o700, f"expected 0700, got {oct(mode)}"


def test_load_returns_empty_config_when_no_file(collect_home: Path):
    cfg = config.load()
    assert cfg.enabled == set()
    assert cfg.interval_overrides == {}
    assert cfg.plugin_settings == {}


def test_enable_disable_round_trip(collect_home: Path):
    cfg = config.load()
    cfg.enable("lastfm")
    cfg.enable("dayone")
    cfg.disable("dayone")
    config.save(cfg)
    reloaded = config.load()
    assert reloaded.enabled == {"lastfm"}


def test_interval_override_round_trip(collect_home: Path):
    cfg = config.load()
    cfg.set_interval("lastfm", 1800)
    config.save(cfg)
    assert config.load().interval_overrides == {"lastfm": 1800}


def test_plugin_settings_round_trip(collect_home: Path):
    cfg = config.load()
    cfg.plugin_settings["dayone"] = {"local_db": True}
    config.save(cfg)
    assert config.load().plugin_settings["dayone"] == {"local_db": True}


def test_save_preserves_comments(collect_home: Path):
    """config.save() must not strip comments the user added by hand."""
    toml_path = config.config_dir() / "config.toml"
    toml_path.write_text(
        "# this is a comment\nenabled = [\"lastfm\"]\n",
        encoding="utf-8",
    )
    cfg = config.load()
    assert cfg.enabled == {"lastfm"}
    cfg.enable("dayone")
    config.save(cfg)
    raw = toml_path.read_text(encoding="utf-8")
    assert "# this is a comment" in raw, (
        "tomlkit round-trip should preserve the hand-written comment"
    )


# ---------------------------------------------------------------------------
# Daemon-wide settings: web_port
# ---------------------------------------------------------------------------

def test_default_web_port_is_9292(collect_home: Path):
    """Empty config → web_port defaults to 9292 (the stable daemon port)."""
    cfg = config.load()
    assert cfg.web_port == 9292


def test_web_port_override_via_daemon_table(collect_home: Path):
    """A user-set `[daemon] web_port = N` in config.toml is honored on load."""
    toml_path = config.config_dir() / "config.toml"
    toml_path.write_text(
        "[daemon]\nweb_port = 9595\n",
        encoding="utf-8",
    )
    cfg = config.load()
    assert cfg.web_port == 9595


def test_web_port_round_trip(collect_home: Path):
    """save → load preserves a non-default web_port."""
    cfg = config.load()
    cfg.web_port = 9999
    config.save(cfg)
    assert config.load().web_port == 9999


def test_default_web_port_not_persisted(collect_home: Path):
    """Saving the default port should not write a `[daemon]` table — keeps
    the default config minimal and means the value is read from the
    code's default constant."""
    cfg = config.load()
    config.save(cfg)
    toml_path = config.config_dir() / "config.toml"
    raw = toml_path.read_text(encoding="utf-8")
    assert "[daemon]" not in raw and "web_port" not in raw


def test_plugin_epoch_survives_noop_and_rotates_on_every_intent_transition(collect_home):
    cfg = config.load()
    cfg.plugin_settings['tasks'] = {'selected_lists': ['one'], 'dry_run': False}
    cfg.enable('tasks')
    config.save(cfg)
    epochs = [config.load().plugin_epochs['tasks']]
    config.save(config.load())
    assert config.load().plugin_epochs['tasks'] == epochs[-1]
    for selection in ([], ['one'], ['two'], ['one']):
        cfg = config.load()
        cfg.plugin_settings['tasks']['selected_lists'] = selection
        config.save(cfg)
        epochs.append(config.load().plugin_epochs['tasks'])
    for enabled in (False, True):
        cfg = config.load()
        (cfg.enable if enabled else cfg.disable)('tasks')
        config.save(cfg)
        epochs.append(config.load().plugin_epochs['tasks'])
    cfg = config.load()
    cfg.plugin_settings['tasks']['dry_run'] = True
    config.save(cfg)
    epochs.append(config.load().plugin_epochs['tasks'])
    assert len(set(epochs)) == len(epochs)


def test_epoch_isolated_from_other_plugin_and_can_rotate_credentials(collect_home):
    cfg = config.load()
    cfg.enable('tasks')
    config.save(cfg)
    first = config.load().plugin_epochs['tasks']
    cfg = config.load()
    cfg.enable('other')
    config.save(cfg)
    assert config.load().plugin_epochs['tasks'] == first
    cfg = config.load()
    cfg.rotate_plugin_epoch('tasks')
    config.save(cfg)
    assert config.load().plugin_epochs['tasks'] != first


def test_stale_config_cannot_restore_epoch_after_credential_rotation(collect_home):
    cfg = config.load()
    cfg.enable('tasks')
    config.save(cfg)
    stale = config.load()
    config.invalidate_plugin_work('tasks')
    current = config.load().plugin_epochs['tasks']
    stale.set_interval('tasks', 600)
    config.save(stale)
    assert config.load().plugin_epochs['tasks'] == current


def test_overlapping_noop_save_cannot_restore_credential_epoch(collect_home):
    """Pause a separate saver after its read while another process invalidates work."""
    import subprocess
    import sys
    import time

    cfg = config.load()
    cfg.enable('tasks')
    config.save(cfg)
    original = config.load().plugin_epochs['tasks']
    paused, release, invalidating = [collect_home / name for name in
                                     ('save-paused', 'save-release', 'invalidation-started')]
    env = dict(os.environ)
    env['PYTHONPATH'] = str(Path(config.__file__).parents[1])
    saver_code = '''
import time
from pathlib import Path
from fulcra_collect import config
home = config.config_dir()
original_dumps = config.tomlkit.dumps
def paused_dumps(doc):
    text = original_dumps(doc)
    (home / "save-paused").touch()
    limit = time.monotonic() + 10
    while not (home / "save-release").exists():
        if time.monotonic() >= limit:
            raise TimeoutError("synthetic paused saver timeout")
        time.sleep(0.01)
    return text
config.tomlkit.dumps = paused_dumps
config.save(config.load())
'''
    invalidator_code = '''
from fulcra_collect import config
(config.config_dir() / "invalidation-started").touch()
config.invalidate_plugin_work("tasks")
'''
    def await_marker(path):
        deadline = time.monotonic() + 10
        while not path.exists():
            assert time.monotonic() < deadline, 'child process failed to reach barrier'
            time.sleep(0.01)
    saver = subprocess.Popen([sys.executable, '-c', saver_code], env=env)
    invalidator = None
    try:
        await_marker(paused)
        invalidator = subprocess.Popen([sys.executable, '-c', invalidator_code], env=env)
        await_marker(invalidating)
        try:
            invalidator.wait(timeout=0.3)
            invalidator_waited_for_lock = False
        except subprocess.TimeoutExpired:
            invalidator_waited_for_lock = True
        release.touch()
        assert saver.wait(timeout=10) == 0
        assert invalidator.wait(timeout=10) == 0
        assert invalidator_waited_for_lock
        assert config.load().plugin_epochs['tasks'] != original
    finally:
        release.touch()
        for process in (saver, invalidator):
            if process is not None and process.poll() is None:
                process.kill()
                process.wait(timeout=5)


def test_failed_atomic_replace_preserves_old_configuration_and_removes_temp(collect_home, monkeypatch):
    import pytest

    cfg = config.load()
    cfg.enable('tasks')
    config.save(cfg)
    path = collect_home / 'config.toml'
    old_bytes = path.read_bytes()
    cfg.disable('tasks')
    def fail_replace(source, destination):
        assert Path(source).parent == collect_home
        assert stat.S_IMODE(os.stat(source).st_mode) == 0o600
        assert config.load().enabled == {'tasks'}
        raise OSError('synthetic failed replacement')
    monkeypatch.setattr(config.os, 'replace', fail_replace)
    with pytest.raises(OSError, match='synthetic failed replacement'):
        config.save(cfg)
    assert path.read_bytes() == old_bytes
    assert not list(collect_home.glob('.config-*.tmp'))


def test_config_file_is_owner_private_after_each_save(collect_home):
    cfg = config.load()
    config.save(cfg)
    path = collect_home / 'config.toml'
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    path.chmod(0o644)
    config.save(cfg)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_failed_temporary_write_keeps_epoch_rotation_retryable(collect_home, monkeypatch):
    import pytest

    cfg = config.load()
    cfg.enable('tasks')
    config.save(cfg)
    path = collect_home / 'config.toml'
    old_bytes = path.read_bytes()
    cfg.rotate_plugin_epoch('tasks')
    new_epoch = cfg.plugin_epochs['tasks']
    with monkeypatch.context() as patch:
        def fail_sync(_):
            raise OSError('synthetic temporary write failure')
        patch.setattr(config.os, 'fsync', fail_sync)
        with pytest.raises(OSError, match='synthetic temporary write failure'):
            config.save(cfg)
    assert path.read_bytes() == old_bytes
    assert not list(collect_home.glob('.config-*.tmp'))
    config.save(cfg)
    assert config.load().plugin_epochs['tasks'] == new_epoch


def test_fulcra_account_change_invalidates_every_known_plugin(collect_home):
    cfg = config.load()
    cfg.enable('tasks-enabled')
    cfg.plugin_settings['tasks-configured'] = {'selected_lists': ['synthetic-list']}
    cfg.rotate_plugin_epoch('tasks-previous')
    config.save(cfg)
    before = config.load()
    config.invalidate_all_plugin_work()
    after = config.load()
    assert set(after.plugin_epochs) == {'tasks-enabled', 'tasks-configured', 'tasks-previous'}
    assert all(after.plugin_epochs[plugin] != epoch for plugin, epoch in before.plugin_epochs.items())
    assert after.enabled == before.enabled
    assert after.plugin_settings == before.plugin_settings


def test_account_transition_blocks_stale_saves_and_rotates_again_on_completion(collect_home):
    cfg = config.load()
    cfg.enable('tasks')
    config.save(cfg)
    stale = config.load()
    before = dict(stale.plugin_epochs)
    with config.fulcra_account_transition():
        during = config.load()
        assert during.account_transition is True
        assert during.plugin_epochs['tasks'] != before['tasks']
        config.save(stale)
        assert config.load().account_transition is True
    after = config.load()
    assert after.account_transition is False
    assert after.plugin_epochs['tasks'] != during.plugin_epochs['tasks']


def test_account_transition_failure_stays_blocked_until_successful_retry(collect_home, monkeypatch):
    import pytest

    cfg = config.load()
    cfg.enable('tasks')
    config.save(cfg)
    with pytest.raises(RuntimeError, match='synthetic token mutation failed'):
        with config.fulcra_account_transition():
            raise RuntimeError('synthetic token mutation failed')
    assert config.load().account_transition is True
    with monkeypatch.context() as patch:
        with pytest.raises(OSError, match='synthetic final save failed'):
            with config.fulcra_account_transition():
                def fail_write(*args):
                    raise OSError('synthetic final save failed')
                patch.setattr(config, '_atomic_write', fail_write)
    assert config.load().account_transition is True
    blocked_epoch = config.load().plugin_epochs['tasks']
    with config.fulcra_account_transition():
        pass
    assert config.load().account_transition is False
    assert config.load().plugin_epochs['tasks'] != blocked_epoch


def test_overlapping_account_transitions_cannot_open_each_others_gate(collect_home):
    import threading

    cfg = config.load()
    cfg.enable('tasks')
    config.save(cfg)
    attempted, entered, finish = threading.Event(), threading.Event(), threading.Event()
    errors = []
    def second_transition():
        try:
            attempted.set()
            with config.fulcra_account_transition():
                entered.set()
                assert finish.wait(5)
        except Exception as exc:
            errors.append(exc)
    worker = threading.Thread(target=second_transition)
    try:
        with config.fulcra_account_transition():
            worker.start()
            assert attempted.wait(5)
            assert not entered.wait(0.2)
            assert config.load().account_transition is True
        assert entered.wait(5)
        assert config.load().account_transition is True
        finish.set()
        worker.join(5)
        assert not worker.is_alive() and not errors
        assert config.load().account_transition is False
    finally:
        finish.set()
        if worker.ident is not None:
            worker.join(5)
