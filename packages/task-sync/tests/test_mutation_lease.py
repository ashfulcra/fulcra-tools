"""Real config locks serialize revocation against external task mutations."""
import threading

import pytest

from fulcra_collect import config


@pytest.mark.parametrize('transition', ['settings', 'account'])
def test_task_mutation_lease_blocks_configuration_transitions(tmp_path, monkeypatch, transition):
    from fulcra_collect.config_leases import task_mutation_scope

    monkeypatch.setenv('FULCRA_COLLECT_HOME', str(tmp_path / 'synthetic-config'))
    cfg = config.load()
    cfg.enable('tasks')
    config.save(cfg)
    attempted, finished = threading.Event(), threading.Event()
    errors = []
    def revoke():
        try:
            attempted.set()
            if transition == 'settings':
                changed = config.load()
                changed.disable('tasks')
                config.save(changed)
            else:
                with config.fulcra_account_transition():
                    pass
            finished.set()
        except Exception as exc:
            errors.append(exc)
    worker = threading.Thread(target=revoke)
    try:
        with task_mutation_scope():
            worker.start()
            assert attempted.wait(5)
            assert not finished.wait(0.2)
        assert finished.wait(5)
        assert not errors
    finally:
        if worker.ident is not None:
            worker.join(5)
    assert not worker.is_alive()


def test_mutation_lease_wait_is_bounded_and_releases_first_lock(tmp_path, monkeypatch):
    from fulcra_collect.config_leases import task_mutation_scope

    monkeypatch.setenv('FULCRA_COLLECT_HOME', str(tmp_path / 'synthetic-config'))
    with config._save_lock(config._config_path()):
        with pytest.raises(TimeoutError, match='lease unavailable'):
            with task_mutation_scope(timeout_s=0.02):
                pytest.fail('shared lease must not enter under exclusive config lock')
    # The failed second-lock acquisition must release the first shared lock.
    with config.fulcra_account_transition():
        pass
    assert config.load().account_transition is False
