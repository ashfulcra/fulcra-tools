"""Recovery reads span old epochs but never grant their mutation authority."""
import sqlite3
from uuid import UUID

import pytest

from fulcra_collect.record_actions import ActionJournal

ACCOUNT = 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee'
OTHER = 'aaaaaaaa-bbbb-4ccc-8ddd-ffffffffffff'
DATA_TYPE = 'Event/11111111-2222-4333-8444-555555555555'


def capture(journal, number, *, account=ACCOUNT, epoch='old-epoch', note='Synthetic'):
    identity = str(UUID(int=number))
    return journal.capture(action_id=identity, account_id=account, epoch=epoch,
        operation='record', request={'note': note}, build_payload=lambda request:{
            'data_type':DATA_TYPE, 'record':{'id':identity, 'note':request['note']}})


def test_read_only_inspection_preserves_old_epoch_and_cannot_advance_it(tmp_path):
    journal = ActionJournal(tmp_path / 'actions.sqlite')
    old = capture(journal, 1)
    assert journal.inspect(old.action_id, account_id=ACCOUNT) == old
    assert journal.inspect(old.action_id, account_id=OTHER) is None
    with pytest.raises(ValueError, match='epoch'):
        journal.advance(old.action_id, account_id=ACCOUNT, epoch='current-epoch',
            expected_status='pending', status='cancelled', evidence={})
    assert journal.get(old.action_id, account_id=ACCOUNT, epoch='old-epoch') == old


def test_account_scoped_pages_keep_stable_creation_order_across_epochs(tmp_path):
    journal = ActionJournal(tmp_path / 'actions.sqlite')
    actions = [capture(journal, i, epoch='old' if i % 2 else 'current') for i in range(1, 8)]
    capture(journal, 8, account=OTHER)
    seen, cursor = [], None
    while True:
        page = journal.list_actions(account_id=ACCOUNT, limit=2, cursor=cursor)
        assert all(row.account_id == ACCOUNT for row in page.actions)
        seen.extend(row.action_id for row in page.actions)
        cursor = page.next_cursor
        if cursor is None:
            break
    assert seen == [row.action_id for row in reversed(actions)]
    assert journal.list_actions(account_id=OTHER).actions[0].action_id == str(UUID(int=8))


def test_filters_and_cursor_cannot_be_reused_for_another_scope(tmp_path):
    journal = ActionJournal(tmp_path / 'actions.sqlite')
    first = capture(journal, 1, epoch='old')
    second = capture(journal, 2, epoch='old')
    capture(journal, 3, epoch='new')
    journal.advance(first.action_id, account_id=ACCOUNT, epoch='old',
        expected_status='pending', status='verified', evidence={'visible':True})
    page = journal.list_actions(account_id=ACCOUNT, statuses=['pending'], limit=1)
    assert page.next_cursor
    for kwargs in ({'account_id':OTHER, 'statuses':['pending']},
                   {'account_id':ACCOUNT, 'statuses':['verified']},
                   {'account_id':ACCOUNT, 'statuses':['pending'], 'epoch':'old'}):
        with pytest.raises(ValueError, match='cursor'):
            journal.list_actions(**kwargs, cursor=page.next_cursor)
    assert journal.list_actions(account_id=ACCOUNT, epoch='old', statuses=['pending']).actions == (second,)


def test_cursor_anchor_status_change_does_not_strand_the_remaining_page(tmp_path):
    journal = ActionJournal(tmp_path / 'actions.sqlite')
    older, newer = capture(journal, 1), capture(journal, 2)
    page = journal.list_actions(account_id=ACCOUNT, statuses=['pending'], limit=1)
    journal.advance(newer.action_id, account_id=ACCOUNT, epoch='old-epoch',
        expected_status='pending', status='verified', evidence={'visible':True})
    remaining = journal.list_actions(account_id=ACCOUNT, statuses=['pending'], cursor=page.next_cursor)
    assert remaining.actions == (older,) and remaining.next_cursor is None


@pytest.mark.parametrize('options', [
    {'limit':True}, {'limit':0}, {'limit':101}, {'statuses':['unknown']},
    {'statuses':[]}, {'statuses':['pending','pending']}, {'epoch':''},
    {'cursor':'not-a-cursor'}, {'operation':'unsupported'},
])
def test_invalid_recovery_read_is_loud(tmp_path, options):
    journal = ActionJournal(tmp_path / 'actions.sqlite')
    with pytest.raises(ValueError):
        journal.list_actions(account_id=ACCOUNT, **options)


def test_corrupt_selected_action_refuses_the_whole_page(tmp_path):
    journal = ActionJournal(tmp_path / 'actions.sqlite')
    capture(journal, 1)
    capture(journal, 2)
    with sqlite3.connect(journal.path) as db:
        db.execute('UPDATE actions SET payload_hash=? WHERE action_id=?', ('bad-hash', str(UUID(int=1))))
    with pytest.raises(ValueError, match='corrupt'):
        journal.list_actions(account_id=ACCOUNT)


def test_unknown_stored_status_cannot_turn_default_recovery_read_into_clean_empty(tmp_path):
    journal = ActionJournal(tmp_path / 'actions.sqlite')
    action = capture(journal, 1)
    with sqlite3.connect(journal.path) as db:
        db.execute('UPDATE actions SET status=? WHERE action_id=?', ('unreadable-status', action.action_id))
    with pytest.raises(ValueError, match='corrupt'):
        journal.list_actions(account_id=ACCOUNT)


@pytest.mark.parametrize('field,value,options', [
    ('status', 'unknown-status', {'statuses': ['pending', 'submitted']}),
    ('operation', 'unknown-operation', {'operation': 'record'}),
])
def test_unknown_classification_cannot_hide_behind_a_recovery_filter(tmp_path, field, value, options):
    journal = ActionJournal(tmp_path / 'actions.sqlite')
    action = capture(journal, 1)
    with sqlite3.connect(journal.path) as db:
        # Field is one of two test constants, never caller input.
        db.execute(f'UPDATE actions SET {field}=? WHERE action_id=?', (value, action.action_id))
    with pytest.raises(ValueError, match='corrupt'):
        journal.list_actions(account_id=ACCOUNT, **options)


def test_page_payload_budget_retains_a_cursor_instead_of_dropping_actions(tmp_path):
    journal = ActionJournal(tmp_path / 'actions.sqlite')
    for number in range(1, 7):
        capture(journal, number, note='x' * (512 * 1024))
    page = journal.list_actions(account_id=ACCOUNT, limit=100)
    assert 0 < len(page.actions) < 6 and page.next_cursor
    seen = [row.action_id for row in page.actions]
    while page.next_cursor:
        page = journal.list_actions(account_id=ACCOUNT, limit=100, cursor=page.next_cursor)
        seen.extend(row.action_id for row in page.actions)
    assert len(seen) == len(set(seen)) == 6
