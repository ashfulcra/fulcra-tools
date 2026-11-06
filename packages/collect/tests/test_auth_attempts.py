"""Durable sign-in ordering without device codes or token storage."""
import concurrent.futures
import sqlite3

import pytest

from fulcra_collect.auth_attempts import AuthAttempts, AuthAttemptError


def test_superseded_start_cannot_bind_or_commit(tmp_path):
    journal = AuthAttempts(tmp_path / 'auth.sqlite3', clock=lambda: 1000)
    old = journal.begin('device')
    current = journal.begin('device')
    with pytest.raises(AuthAttemptError):
        journal.bind(old, 'synthetic-device', 900, 5)
    journal.bind(current, 'synthetic-device', 900, 5)
    assert journal.claim(current, 'synthetic-device') is None
    effects = []
    with pytest.raises(AuthAttemptError):
        journal.finish(old, lambda: effects.append('stale'))
    journal.finish(current, lambda: effects.append('current'))
    assert effects == ['current']
    assert journal.committed() == (current, 'device')


def test_reopen_unknown_consumed_grant_cannot_poll_again(tmp_path):
    path = tmp_path / 'auth.sqlite3'
    journal = AuthAttempts(path, clock=lambda: 1000)
    attempt = journal.begin('device')
    journal.bind(attempt, 'synthetic-device', 900, 5)
    journal.claim(attempt, 'synthetic-device')
    with pytest.raises(AuthAttemptError):
        AuthAttempts(path, clock=lambda: 1001).claim(attempt, 'synthetic-device')
    with pytest.raises(AuthAttemptError):
        journal.claim(attempt, 'other-device')
    assert b'synthetic-device' not in path.read_bytes()


def test_poll_spacing_and_expiry_are_enforced(tmp_path):
    now = [1000]
    journal = AuthAttempts(tmp_path / 'auth.sqlite3', clock=lambda: now[0])
    attempt = journal.begin('device')
    journal.bind(attempt, 'synthetic-device', 20, 5)
    journal.claim(attempt, 'synthetic-device')
    assert journal.pending(attempt) == 5
    assert journal.claim(attempt, 'synthetic-device') == 5
    now[0] += 5
    assert journal.claim(attempt, 'synthetic-device') is None
    assert journal.pending(attempt, slow=True) == 10
    now[0] = 1020
    with pytest.raises(AuthAttemptError):
        journal.claim(attempt, 'synthetic-device')


def test_clock_rollback_refuses_attempt(tmp_path):
    now = [1000]
    journal = AuthAttempts(tmp_path / 'auth.sqlite3', clock=lambda: now[0])
    attempt = journal.begin('device')
    journal.bind(attempt, 'synthetic-device', 20, 5)
    now[0] = 999
    with pytest.raises(AuthAttemptError):
        journal.claim(attempt, 'synthetic-device')


def test_failed_commit_cannot_be_replayed_or_replace_committed_revision(tmp_path):
    journal = AuthAttempts(tmp_path / 'auth.sqlite3', clock=lambda: 1000)
    first = journal.begin('token')
    journal.finish(first, lambda: None)
    second = journal.begin('token')
    def broken():
        raise RuntimeError('synthetic keychain failure')
    with pytest.raises(RuntimeError):
        journal.finish(second, broken)
    assert journal.committed() == (first, 'token')
    with pytest.raises(AuthAttemptError):
        journal.finish(second, lambda: pytest.fail('must not retry ambiguous commit'))


def test_concurrent_claim_only_one_owns_grant(tmp_path):
    path = tmp_path / 'auth.sqlite3'
    journal = AuthAttempts(path, clock=lambda: 1000)
    attempt = journal.begin('device')
    journal.bind(attempt, 'synthetic-device', 900, 5)
    def claim():
        try:
            return AuthAttempts(path, clock=lambda: 1000).claim(attempt, 'synthetic-device')
        except AuthAttemptError:
            return 'refused'
    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        assert sorted(list(pool.map(lambda _: claim(), range(2))), key=str) == [None, 'refused']


@pytest.mark.parametrize('change', ['replace', 'clear'])
def test_new_attempt_during_poll_blocks_late_commit(tmp_path, change):
    journal = AuthAttempts(tmp_path / 'auth.sqlite3', clock=lambda: 1000)
    old = journal.begin('device')
    journal.bind(old, 'synthetic-device', 900, 5)
    journal.claim(old, 'synthetic-device')
    current = journal.begin('clear' if change == 'clear' else 'token')
    journal.finish(current, lambda: None)
    with pytest.raises(AuthAttemptError):
        journal.finish(old, lambda: pytest.fail('late credential mutation'))
    assert journal.committed()[0] == current


@pytest.mark.parametrize('value', [0, -1, float('nan'), float('inf'), True, '900'])
def test_invalid_expiry_never_binds(tmp_path, value):
    journal = AuthAttempts(tmp_path / 'auth.sqlite3', clock=lambda: 1000)
    attempt = journal.begin('device')
    with pytest.raises(AuthAttemptError):
        journal.bind(attempt, 'synthetic-device', value, 5)


def test_symlink_or_nonprivate_journal_refuses(tmp_path):
    path = tmp_path / 'auth.sqlite3'
    journal = AuthAttempts(path)
    assert journal.committed() == (None, None)
    path.chmod(0o644)
    with pytest.raises(AuthAttemptError):
        AuthAttempts(path).begin('token')
    path.chmod(0o600)
    link = tmp_path / 'alias.sqlite3'
    link.symlink_to(path)
    with pytest.raises((AuthAttemptError, OSError)):
        AuthAttempts(link).begin('token')


def test_corrupt_version_fails_closed(tmp_path):
    path = tmp_path / 'auth.sqlite3'
    assert AuthAttempts(path).committed() == (None, None)
    with sqlite3.connect(path) as db:
        db.execute('UPDATE auth_state SET version=99')
    with pytest.raises(AuthAttemptError):
        AuthAttempts(path).begin('token')
