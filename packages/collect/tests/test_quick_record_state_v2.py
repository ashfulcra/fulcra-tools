"""Account-scoped pins and timer stops survive lost replies and app restart."""
from datetime import datetime, timedelta, timezone
import json
import sqlite3
from uuid import UUID

import pytest

from fulcra_collect.record_actions import ActionJournal

ACCOUNT = 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee'
OTHER = 'aaaaaaaa-bbbb-4ccc-8ddd-ffffffffffff'
CHOICE = str(UUID(int=1))
HIDDEN = str(UUID(int=2))
TIMER = str(UUID(int=3))
DESTINATION = 'Event/11111111-2222-4333-8444-555555555555'
NOW = datetime(2026, 1, 2, tzinfo=timezone.utc)


@pytest.fixture
def state(tmp_path):
    from fulcra_collect.quick_record_state import QuickRecordState
    return QuickRecordState(ActionJournal(tmp_path / 'actions.sqlite'))


def test_pin_diff_keeps_hidden_pins_and_never_adopts_legacy_unscoped_file(state, tmp_path):
    original = tmp_path / 'quick_record_favorites.json'
    original.write_text(json.dumps({'version': 1, 'favorites': [CHOICE]}))
    first = state.favorites(ACCOUNT)
    assert first == {'revision': 0, 'favorites': []}
    hidden = state.change_favorites(ACCOUNT, expected_revision=0, add=[HIDDEN], remove=[])
    updated = state.change_favorites(ACCOUNT, expected_revision=hidden['revision'], add=[CHOICE], remove=[])
    assert set(updated['favorites']) == {CHOICE, HIDDEN}
    assert state.favorites(OTHER) == first
    assert json.loads(original.read_text())['favorites'] == [CHOICE]


def test_stale_favorite_editor_cannot_replace_or_unpin_other_edits(state):
    first = state.change_favorites(ACCOUNT, expected_revision=0, add=[CHOICE], remove=[])
    with pytest.raises(ValueError, match='revision'):
        state.change_favorites(ACCOUNT, expected_revision=0, add=[HIDDEN], remove=[CHOICE])
    assert state.favorites(ACCOUNT) == first
    removed = state.change_favorites(ACCOUNT, expected_revision=first['revision'], add=[], remove=[CHOICE])
    assert removed['favorites'] == [] and removed['revision'] > first['revision']


@pytest.mark.parametrize('changes', [
    {'expected_revision': True}, {'add': ['bad']}, {'add': [CHOICE, CHOICE]},
    {'add': [CHOICE], 'remove': [CHOICE]}, {'remove': 'not-an-array'},
])
def test_malformed_pin_diff_does_not_write(state, changes):
    args = {'expected_revision': 0, 'add': [], 'remove': []} | changes
    with pytest.raises(ValueError):
        state.change_favorites(ACCOUNT, **args)
    assert state.favorites(ACCOUNT)['revision'] == 0


def start(state, **changes):
    args = dict(timer_id=TIMER, account_id=ACCOUNT, epoch='captured-epoch',
        choice={'id': CHOICE, 'name': 'Synthetic duration', 'kind': 'duration', 'digest': 'a' * 64,
                'data_type': DESTINATION, 'tags': []},
        started_at=NOW)
    return state.start_timer(**(args | changes))


@pytest.mark.parametrize('missing', ['data_type', 'tags'])
def test_timer_start_requires_a_prepared_target_and_tag_snapshot(state, missing):
    choice = {'id': CHOICE, 'name': 'Synthetic duration', 'kind': 'duration', 'digest': 'a' * 64,
              'data_type': DESTINATION, 'tags': []}
    choice.pop(missing)
    with pytest.raises(ValueError, match='choice'):
        start(state, choice=choice)


def test_timer_start_is_durable_and_retry_cannot_shift_its_first_timestamp(state):
    first = start(state)
    fresh = type(state)(ActionJournal(state.journal.path))
    assert fresh.timers(ACCOUNT) == [first]
    assert start(fresh, started_at=NOW + timedelta(minutes=5)) == first
    for changes in ({'account_id': OTHER}, {'epoch': 'different-epoch'},
                    {'choice': {'id': HIDDEN, 'name': 'Changed', 'kind': 'duration'}}):
        with pytest.raises(ValueError, match='timer'):
            start(fresh, **changes)
    assert fresh.timers(OTHER) == []


def test_first_stop_freezes_action_end_and_comment_before_any_record_effect(state):
    first = start(state)
    stop = state.stop_timer(TIMER, account_id=ACCOUNT, epoch='captured-epoch',
        ended_at=NOW + timedelta(hours=1), comment='Synthetic note')
    assert stop['status'] == 'stopping' and stop['started_at'] == first['started_at']
    assert str(UUID(stop['action_id'])) == stop['action_id']
    assert stop['ended_at'] == (NOW + timedelta(hours=1)).isoformat()
    fresh = type(state)(ActionJournal(state.journal.path))
    retry = fresh.stop_timer(TIMER, account_id=ACCOUNT, epoch='captured-epoch',
        ended_at=NOW + timedelta(hours=2), comment='Synthetic note')
    assert retry == stop and fresh.timers(ACCOUNT) == [stop]
    assert fresh.journal.inspect(stop['action_id'], account_id=ACCOUNT) is None
    with pytest.raises(ValueError, match='comment'):
        fresh.stop_timer(TIMER, account_id=ACCOUNT, epoch='captured-epoch',
            ended_at=NOW + timedelta(hours=2), comment='Changed after timeout')


@pytest.mark.parametrize('account,epoch', [(OTHER, 'captured-epoch'), (ACCOUNT, 'new-epoch')])
def test_timer_cannot_be_stopped_in_another_account_or_new_epoch(state, account, epoch):
    start(state)
    with pytest.raises(ValueError, match='timer'):
        state.stop_timer(TIMER, account_id=account, epoch=epoch,
            ended_at=NOW + timedelta(hours=1), comment=None)
    assert state.timers(ACCOUNT)[0]['status'] == 'running'


@pytest.mark.parametrize('end', [NOW, NOW - timedelta(seconds=1), NOW.replace(tzinfo=None)])
def test_bad_duration_end_keeps_original_timer_running(state, end):
    start(state)
    with pytest.raises(ValueError, match='time|end'):
        state.stop_timer(TIMER, account_id=ACCOUNT, epoch='captured-epoch', ended_at=end, comment=None)
    assert state.timers(ACCOUNT)[0]['status'] == 'running'


def test_timer_completion_requires_its_verified_or_excluded_record_action(state):
    from fulcra_common.v1 import build_event_record
    start(state)
    timer = state.stop_timer(TIMER, account_id=ACCOUNT, epoch='captured-epoch',
        ended_at=NOW + timedelta(hours=1), comment=None)
    with pytest.raises(ValueError, match='verified|excluded'):
        state.finish_timer(TIMER, account_id=ACCOUNT, epoch='captured-epoch')
    destination = 'Event/11111111-2222-4333-8444-555555555555'
    action = state.journal.capture(action_id=timer['action_id'], account_id=ACCOUNT, epoch='captured-epoch',
        operation='record', request={'timer_id': TIMER}, build_payload=lambda _: {
            'data_type': destination, 'record': build_event_record(data_type=destination,
                source_id='synthetic.timer.' + TIMER, start_time=NOW, end_time=NOW + timedelta(hours=1))})
    state.journal.advance(action.action_id, account_id=ACCOUNT, epoch='captured-epoch',
        expected_status='pending', status='verified', evidence={'readback': 'exact_fields'})
    done = state.finish_timer(TIMER, account_id=ACCOUNT, epoch='captured-epoch')
    assert done['status'] == 'verified' and done['action_id'] == action.action_id
    assert state.timers(ACCOUNT) == []
    assert state.timers(ACCOUNT, include_finished=True) == [done]


def test_completed_history_does_not_block_active_timer_discovery_and_is_paginated(state):
    from fulcra_collect.record_actions import _hash, _json
    start(state)
    stopped = state.stop_timer(TIMER, account_id=ACCOUNT, epoch='captured-epoch',
        ended_at=NOW + timedelta(hours=1), comment=None)
    # Populate well-shaped synthetic completed history in one transaction.
    # This test exercises storage paging, not remote completion authority.
    with sqlite3.connect(state.journal.path) as db:
        template = db.execute('SELECT * FROM quick_record_timers WHERE timer_id=?', (TIMER,)).fetchone()
        for number in range(1001, 2002):
            values = list(template)
            values[0] = str(UUID(int=number))
            frozen = _json({'action_id': str(UUID(int=number + 10000)),
                           'ended_at': stopped['ended_at'], 'comment': None})
            values[6], values[7], values[8] = frozen, _hash(frozen), 'verified'
            db.execute('INSERT INTO quick_record_timers VALUES (?,?,?,?,?,?,?,?,?)', values)
    assert state.timers(ACCOUNT) == [stopped]
    seen, cursor = set(), None
    while True:
        page = state.timer_page(ACCOUNT, include_finished=True, limit=100, cursor=cursor)
        seen.update(row['timer_id'] for row in page['timers'])
        cursor = page['next_cursor']
        if cursor is None:
            break
    assert len(seen) == 1002 and TIMER in seen
