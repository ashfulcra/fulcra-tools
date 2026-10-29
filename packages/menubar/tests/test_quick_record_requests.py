"""A local timeout/restart never generates a replacement capture identity."""
from uuid import UUID

import pytest

from fulcra_collect.record_actions import ActionJournal

ACCOUNT = 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee'
OTHER = 'aaaaaaaa-bbbb-4ccc-8ddd-ffffffffffff'
IDENTITY = str(UUID(int=1))
TRACK = str(UUID(int=2))


def body(**changes):
    return {'operation': 'record_annotation', 'protocol_version': 2, 'account_id': ACCOUNT,
            'config_epoch': 'captured', 'action_id': IDENTITY, 'definition_id': TRACK,
            'choice_digest': 'a' * 64, 'start_time': '2026-01-02T12:00:00Z',
            'comment': 'Synthetic note 🐈', **changes}


@pytest.fixture
def journal(tmp_path):
    from fulcra_menubar.quick_record_requests import ClientRequestJournal
    return ClientRequestJournal(ActionJournal(tmp_path / 'client.sqlite'))


def capture(journal, **changes):
    return journal.capture(request_id=IDENTITY, body=body(**changes))


def reply(**changes):
    return {'protocol_version': 2, 'account_id': ACCOUNT, 'config_epoch': 'captured',
            'ok': True, 'accepted': True, 'status': 'verified', 'action_id': IDENTITY,
            'operation': 'record', 'captured_epoch': 'captured', 'record_id': str(UUID(int=3)),
            'data_type': 'Event/11111111-2222-4333-8444-555555555555', **changes}


def test_capture_and_attempt_survive_restart_with_the_exact_original_request(journal):
    original = capture(journal)
    assert original.status == 'queued'
    attempted = journal.mark_attempt(IDENTITY, account_id=ACCOUNT, epoch='captured')
    assert attempted.status == 'pending' and attempted.body == body()
    fresh = type(journal)(ActionJournal(journal.journal.path))
    restored = fresh.get(IDENTITY, account_id=ACCOUNT)
    assert restored.body == body() and restored.status == 'pending'
    restored.body['comment'] = 'Changed returned snapshot'
    assert fresh.get(IDENTITY, account_id=ACCOUNT).body == body()


@pytest.mark.parametrize('change', [
    {'account_id': OTHER}, {'config_epoch': 'new'}, {'comment': 'Changed note'},
    {'start_time': '2026-01-02T13:00:00Z'},
])
def test_reused_identity_cannot_change_scope_note_or_first_time(journal, change):
    capture(journal)
    with pytest.raises(ValueError, match='changed|belongs'):
        capture(journal, **change)
    assert journal.get(IDENTITY, account_id=ACCOUNT).body == body()


def test_account_or_epoch_switch_cannot_dispatch_the_old_request(journal):
    capture(journal)
    for account, epoch in [(OTHER, 'captured'), (ACCOUNT, 'new')]:
        with pytest.raises(ValueError, match='scope|account|epoch'):
            journal.mark_attempt(IDENTITY, account_id=account, epoch=epoch)
    assert journal.get(IDENTITY, account_id=OTHER) is None
    assert journal.get(IDENTITY, account_id=ACCOUNT).status == 'queued'


@pytest.mark.parametrize('change', [
    {'account_id': OTHER}, {'config_epoch': 'new'}, {'protocol_version': True},
    {'action_id': str(UUID(int=99))}, {'captured_epoch': 'old'}, {'operation': 'delete'},
    {'status': 'submitted'}, {'ok': False},
])
def test_unbound_or_contradictory_response_cannot_settle_the_saved_request(journal, change):
    capture(journal)
    journal.mark_attempt(IDENTITY, account_id=ACCOUNT, epoch='captured')
    with pytest.raises(ValueError, match='response|proof|scope'):
        journal.accept_reply(IDENTITY, account_id=ACCOUNT, epoch='captured', reply=reply(**change))
    assert journal.get(IDENTITY, account_id=ACCOUNT).status == 'pending'


def test_submitted_receipt_retains_pending_then_exact_bound_completion_is_immutable(journal):
    capture(journal)
    journal.mark_attempt(IDENTITY, account_id=ACCOUNT, epoch='captured')
    pending = journal.accept_reply(IDENTITY, account_id=ACCOUNT, epoch='captured',
        reply=reply(ok=False, status='submitted'))
    assert pending.status == 'pending' and pending.body == body()
    done = journal.accept_reply(IDENTITY, account_id=ACCOUNT, epoch='captured', reply=reply())
    assert done.status == 'verified'
    assert journal.accept_reply(IDENTITY, account_id=ACCOUNT, epoch='captured', reply=reply()) == done
    with pytest.raises(ValueError, match='settled|terminal|proof'):
        journal.accept_reply(IDENTITY, account_id=ACCOUNT, epoch='captured', reply=reply(record_id=str(UUID(int=4))))
    assert journal.pending(account_id=ACCOUNT)['requests'] == ()


def test_pending_pages_are_account_bound_and_keep_old_epoch_requests_visible(journal):
    capture(journal)
    for number in range(4, 7):
        identity = str(UUID(int=number))
        journal.capture(request_id=identity, body=body(action_id=identity, config_epoch='old'))
    first = journal.pending(account_id=ACCOUNT, limit=2)
    assert len(first['requests']) == 2 and first['next_cursor']
    second = journal.pending(account_id=ACCOUNT, limit=2, cursor=first['next_cursor'])
    assert len(second['requests']) == 2 and second['next_cursor'] is None
    assert any(intent.epoch == 'old' for intent in first['requests'])
    assert journal.pending(account_id=OTHER)['requests'] == ()
    with pytest.raises(ValueError, match='cursor'):
        journal.pending(account_id=OTHER, cursor=first['next_cursor'])


def timer_body(operation='quick_record_timer_start', **changes):
    fields = {'timer_id': IDENTITY, 'definition_id': TRACK, 'choice_digest': 'a' * 64,
              'start_time': '2026-01-02T12:00:00Z'} if operation.endswith('_start') else {
              'timer_id': IDENTITY, 'end_time': '2026-01-02T13:00:00Z', 'comment': 'Frozen stop note'}
    return {'operation': operation, 'protocol_version': 2, 'account_id': ACCOUNT,
            'config_epoch': 'captured', **fields, **changes}


def timer_reply(**changes):
    return {'protocol_version': 2, 'account_id': ACCOUNT, 'config_epoch': 'captured', 'ok': True,
            'timer': {'timer_id': IDENTITY, 'captured_epoch': 'captured', 'definition_id': TRACK,
                      'name': 'Synthetic timer', 'status': 'running',
                      'start_time': '2026-01-02T12:00:00+00:00', 'end_time': None, 'action_id': None,
                      **changes}}


def test_timer_start_is_durable_and_stores_only_projected_metadata(journal):
    journal.capture(request_id=IDENTITY, body=timer_body())
    journal.mark_attempt(IDENTITY, account_id=ACCOUNT, epoch='captured')
    result = journal.accept_reply(IDENTITY, account_id=ACCOUNT, epoch='captured',
                                 reply=timer_reply(comment='Unexpected private server field'))
    assert result.status == 'started' and 'comment' not in result.reply['timer']
    restored = type(journal)(ActionJournal(journal.journal.path)).get(IDENTITY, account_id=ACCOUNT)
    assert restored == result and restored.body == timer_body()


@pytest.mark.parametrize('change', [
    {'timer_id': str(UUID(int=99))}, {'captured_epoch': 'other'}, {'definition_id': str(UUID(int=99))},
    {'start_time': '2026-01-02T12:01:00Z'},
])
def test_timer_start_cannot_acknowledge_a_different_capture(journal, change):
    journal.capture(request_id=IDENTITY, body=timer_body())
    journal.mark_attempt(IDENTITY, account_id=ACCOUNT, epoch='captured')
    with pytest.raises(ValueError, match='proof'):
        journal.accept_reply(IDENTITY, account_id=ACCOUNT, epoch='captured', reply=timer_reply(**change))
    assert journal.get(IDENTITY, account_id=ACCOUNT).status == 'pending'


def stop_reply(**timer_changes):
    action = str(UUID(int=7))
    return reply(action_id=action) | {'timer': timer_reply(status='verified', action_id=action,
        end_time='2026-01-02T13:00:00+00:00', **timer_changes)['timer']}


@pytest.mark.parametrize('change', [
    {'end_time': '2026-01-02T13:01:00Z'}, {'status': 'stopping'},
    {'start_time': '2026-01-02T14:00:00Z'},
])
def test_timer_stop_completion_must_match_its_first_end_and_consistent_state(journal, change):
    stop_id = str(UUID(int=8))
    journal.capture(request_id=stop_id, body=timer_body('quick_record_timer_stop'))
    journal.mark_attempt(stop_id, account_id=ACCOUNT, epoch='captured')
    proof = stop_reply()
    proof['timer'].update(change)
    with pytest.raises(ValueError, match='proof'):
        journal.accept_reply(stop_id, account_id=ACCOUNT, epoch='captured', reply=proof)
    assert journal.get(stop_id, account_id=ACCOUNT).status == 'pending'


def test_timer_stop_binds_first_observed_action_and_preserves_note_across_timeout(journal):
    stop_id = str(UUID(int=8))
    request = timer_body('quick_record_timer_stop')
    journal.capture(request_id=stop_id, body=request)
    journal.mark_attempt(stop_id, account_id=ACCOUNT, epoch='captured')
    proof = stop_reply()
    proof.update(ok=False, status='submitted')
    proof['timer']['status'] = 'stopping'
    assert journal.accept_reply(stop_id, account_id=ACCOUNT, epoch='captured', reply=proof).status == 'pending'
    fresh = type(journal)(ActionJournal(journal.journal.path))
    assert fresh.get(stop_id, account_id=ACCOUNT).body == request
    changed = stop_reply()
    changed['action_id'] = changed['timer']['action_id'] = str(UUID(int=9))
    with pytest.raises(ValueError, match='proof'):
        fresh.accept_reply(stop_id, account_id=ACCOUNT, epoch='captured', reply=changed)
    assert fresh.accept_reply(stop_id, account_id=ACCOUNT, epoch='captured', reply=stop_reply()).status == 'verified'


def test_oversized_projected_receipt_cannot_fill_private_pending_history(journal):
    capture(journal)
    journal.mark_attempt(IDENTITY, account_id=ACCOUNT, epoch='captured')
    with pytest.raises(ValueError, match='bound|large|limit'):
        journal.accept_reply(IDENTITY, account_id=ACCOUNT, epoch='captured', reply=reply(name='x' * 5000))
    assert journal.get(IDENTITY, account_id=ACCOUNT).reply == {}


def test_first_local_timer_stop_survives_new_click_with_another_request_id(journal):
    original = str(UUID(int=8))
    request = timer_body('quick_record_timer_stop')
    journal.capture(request_id=original, body=request)
    journal.mark_attempt(original, account_id=ACCOUNT, epoch='captured')
    fresh = type(journal)(ActionJournal(journal.journal.path))
    with pytest.raises(ValueError, match='original|first|saved stop'):
        fresh.capture(request_id=str(UUID(int=9)), body=timer_body('quick_record_timer_stop',
            end_time='2026-01-02T14:00:00Z', comment='Second click'))
    assert fresh.timer_stop(IDENTITY, account_id=ACCOUNT).request_id == original
    assert fresh.timer_stop(IDENTITY, account_id=ACCOUNT).body == request
    assert fresh.timer_stop(IDENTITY, account_id=OTHER) is None


@pytest.mark.parametrize('change', [
    {'record_id': str(UUID(int=99))}, {'data_type': 'Event/aaaaaaaa-2222-4333-8444-555555555555'},
])
def test_later_response_cannot_retarget_original_pending_record_proof(journal, change):
    capture(journal)
    journal.mark_attempt(IDENTITY, account_id=ACCOUNT, epoch='captured')
    journal.accept_reply(IDENTITY, account_id=ACCOUNT, epoch='captured', reply=reply(ok=False, status='submitted'))
    with pytest.raises(ValueError, match='proof|target'):
        journal.accept_reply(IDENTITY, account_id=ACCOUNT, epoch='captured', reply=reply(**change))
    assert journal.get(IDENTITY, account_id=ACCOUNT).reply['record_id'] == reply()['record_id']


@pytest.mark.parametrize('damage', ['missing-stop-table', 'future-version', 'missing-meta-table'])
def test_partial_or_unknown_client_state_schema_never_heals_to_clean_empty(journal, damage):
    capture(journal)
    with journal.journal._db(write=True) as db:
        db.execute('CREATE TABLE IF NOT EXISTS quick_record_client_meta(version INTEGER NOT NULL)')
        if not db.execute('SELECT 1 FROM quick_record_client_meta').fetchone():
            db.execute('INSERT INTO quick_record_client_meta VALUES (1)')
        if damage == 'missing-stop-table':
            db.execute('DROP TABLE quick_record_client_timer_stops')
        elif damage == 'missing-meta-table':
            db.execute('DROP TABLE quick_record_client_meta')
        else:
            db.execute('UPDATE quick_record_client_meta SET version=999')
    with pytest.raises(ValueError, match='schema|version'):
        type(journal)(ActionJournal(journal.journal.path))
    with journal.journal._db() as db:
        assert db.execute('SELECT COUNT(*) FROM quick_record_client_requests').fetchone()[0] == 1
