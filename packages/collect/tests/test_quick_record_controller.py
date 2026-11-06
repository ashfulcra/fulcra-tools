"""Protocol2 ties account scope, prepared choices and durable action identity."""
from datetime import timedelta
from uuid import uuid4

import pytest

from test_quick_record_actor import runtime as runtime  # shared synthetic HTTP fixture
from test_quick_record_actor import ACCOUNT, DESTINATION, NOW

TRACK = '00000000-0000-0000-0000-000000000021'
DIGEST = 'a' * 64


@pytest.fixture
def controller(runtime, monkeypatch):
    from fulcra_collect.quick_record_controller import QuickRecordController
    from fulcra_collect.quick_record_choices import QuickRecordChoices
    session, journal, _ = runtime
    prepared = {'id': TRACK, 'name': 'Synthetic duration', 'kind': 'duration', 'tags': [],
                'digest': DIGEST, 'annotation_type': 'duration', 'data_type': DESTINATION}

    def list_choices(self):
        self.session.guard()
        return [dict(prepared)]

    def prepare(self, identity, *, expected_digest):
        self.session.guard(mutating=True)
        if identity != TRACK or expected_digest != DIGEST:
            raise ValueError('track changed')
        return dict(prepared)

    monkeypatch.setattr(QuickRecordChoices, 'list', list_choices)
    monkeypatch.setattr(QuickRecordChoices, 'prepare', prepare)
    return QuickRecordController(session, journal)


def scope(controller):
    return {'protocol_version': 2, 'account_id': ACCOUNT, 'config_epoch': controller.session.epoch}


def request(controller, **changes):
    return {'cmd': 'record_annotation', **scope(controller), 'action_id': str(uuid4()),
            'definition_id': TRACK, 'choice_digest': DIGEST, 'comment': 'Synthetic note',
            'start_time': NOW.isoformat(), 'end_time': (NOW + timedelta(hours=1)).isoformat(), **changes}


def test_handshake_returns_one_account_epoch_choices_and_favorite_revision(controller):
    reply = controller.dispatch({'cmd': 'quick_record_list'})
    assert reply['ok'] and all(reply[key] == value for key, value in scope(controller).items())
    assert reply['favorites_revision'] == 0 and reply['definitions'][0]['id'] == TRACK
    assert reply['definitions'][0]['pinned'] is False


@pytest.mark.parametrize('change', [
    {'protocol_version': None}, {'protocol_version': True}, {'account_id': 'bad'},
    {'account_id': 'aaaaaaaa-bbbb-4ccc-8ddd-ffffffffffff'}, {'config_epoch': 'old'},
])
def test_obsolete_or_changed_scope_never_captures_or_uploads(controller, runtime, change):
    _, journal, state = runtime
    with pytest.raises(ValueError, match='refresh|scope|protocol'):
        controller.dispatch(request(controller, **change))
    assert journal.list_actions(account_id=ACCOUNT).actions == () and not state['posts']


def test_retry_uses_frozen_action_without_resolving_a_mutable_choice_again(controller, runtime, monkeypatch):
    _, journal, state = runtime
    intent = request(controller)
    state['action_id'] = intent['action_id']
    first = controller.dispatch(intent)
    assert first['ok'] and first['status'] == 'verified' and first['action_id'] == intent['action_id']
    assert first['data_type'] == DESTINATION and first['record_id']
    monkeypatch.setattr(controller.choices, 'prepare', lambda *a, **k: pytest.fail('retry re-resolved mutable choice'))
    assert controller.dispatch(intent) == first
    assert len(state['posts']) == 1
    saved = journal.inspect(intent['action_id'], account_id=ACCOUNT)
    assert saved.payload['record']['track_id'] == TRACK and saved.payload['record']['note'] == 'Synthetic note'


def test_delayed_index_is_not_success_and_status_reconciliation_does_not_create_again(controller, runtime):
    _, _, state = runtime
    intent = request(controller)
    state.update(action_id=intent['action_id'], delay=True)
    pending = controller.dispatch(intent)
    assert pending['accepted'] and not pending['ok'] and pending['status'] == 'submitted'
    status = controller.dispatch({'cmd': 'quick_record_action', **scope(controller), 'action_id': intent['action_id']})
    assert status['status'] == 'submitted'
    state['rows'][(DESTINATION, pending['record_id'])] = state['delayed'][0]
    reconciled = controller.dispatch({'cmd': 'quick_record_reconcile', **scope(controller), 'action_id': intent['action_id']})
    assert reconciled['ok'] and reconciled['status'] == 'verified'
    assert len(state['posts']) == 1


def test_native_undo_references_original_action_id_and_new_undo_action_id(controller, runtime):
    _, _, state = runtime
    intent = request(controller)
    state['action_id'] = intent['action_id']
    original = controller.dispatch(intent)
    undo_id = str(uuid4())
    undone = controller.dispatch({'cmd': 'delete_annotation', **scope(controller),
        'action_id': undo_id, 'record_action_id': original['action_id']})
    assert undone['ok'] and undone['action_id'] == undo_id and undone['operation'] == 'delete'
    assert (DESTINATION, original['record_id']) not in state['rows']


def test_pin_toggle_uses_captured_scope_revision_and_single_choice_diff(controller):
    added = controller.dispatch({'cmd': 'set_quick_record_favorites', **scope(controller),
        'choice_id': TRACK, 'choice_digest': DIGEST, 'pinned': True, 'expected_revision': 0})
    assert added['ok'] and added['favorites'] == [TRACK] and added['revision'] == 1
    with pytest.raises(ValueError, match='revision'):
        controller.dispatch({'cmd': 'set_quick_record_favorites', **scope(controller),
            'choice_id': TRACK, 'choice_digest': DIGEST, 'pinned': False, 'expected_revision': 0})
    assert controller.dispatch({'cmd': 'quick_record_list'})['definitions'][0]['pinned'] is True


def test_timer_failed_stop_retains_first_window_and_action_for_retry(controller, runtime):
    session, journal, state = runtime
    timer_id = str(uuid4())
    started = controller.dispatch({'cmd': 'quick_record_timer_start', **scope(controller),
        'timer_id': timer_id, 'definition_id': TRACK, 'choice_digest': DIGEST, 'start_time': NOW.isoformat()})
    assert started['timer']['status'] == 'running'
    # The fixture asserts the target POST already has a submitted intent.
    # Freeze the stop first so its server-assigned action UUID is observable.
    frozen = controller.state.stop_timer(timer_id, account_id=ACCOUNT, epoch=session.epoch,
        ended_at=NOW + timedelta(hours=1), comment='Synthetic note')
    state.update(action_id=frozen['action_id'], delay=True)
    stop_request = {'cmd': 'quick_record_timer_stop', **scope(controller), 'timer_id': timer_id,
                    'end_time': (NOW + timedelta(hours=2)).isoformat(), 'comment': 'Synthetic note'}
    pending = controller.dispatch(stop_request)
    assert pending['status'] == 'submitted' and pending['timer']['status'] == 'stopping'
    saved = journal.inspect(frozen['action_id'], account_id=ACCOUNT)
    assert saved.payload['record']['end_time'] == (NOW + timedelta(hours=1)).isoformat().replace('+00:00', 'Z')
    state['rows'][(DESTINATION, pending['record_id'])] = state['delayed'][0]
    finished = controller.dispatch(stop_request)
    assert finished['ok'] and finished['timer']['status'] == 'verified'
    assert controller.state.timers(ACCOUNT) == [] and len(state['posts']) == 1


@pytest.mark.parametrize('captured', [False, True])
def test_timer_pending_recovery_uses_saved_note_even_across_the_pre_action_crash_gap(controller, runtime, captured):
    session, journal, state = runtime
    timer_id = str(uuid4())
    controller.dispatch({'cmd': 'quick_record_timer_start', **scope(controller), 'timer_id': timer_id,
        'definition_id': TRACK, 'choice_digest': DIGEST, 'start_time': NOW.isoformat()})
    frozen = controller.state.stop_timer(timer_id, account_id=ACCOUNT, epoch=session.epoch,
        ended_at=NOW + timedelta(hours=1), comment='Saved before crash 🐈')
    state.update(action_id=frozen['action_id'], delay=captured)
    if captured:
        controller.dispatch({'cmd': 'quick_record_timer_reconcile', **scope(controller), 'timer_id': timer_id})
        state['rows'][(DESTINATION, state['delayed'][0]['id'])] = state['delayed'][0]
    recovered = controller.dispatch({'cmd': 'quick_record_timer_reconcile', **scope(controller), 'timer_id': timer_id})
    assert recovered['ok'] and recovered['timer']['status'] == 'verified'
    assert controller.state.timers(ACCOUNT) == [] and len(state['posts']) == 1
    action = journal.inspect(frozen['action_id'], account_id=ACCOUNT)
    assert action.payload['record']['note'] == 'Saved before crash 🐈'
    assert action.payload['record']['end_time'] == frozen['ended_at'].replace('+00:00', 'Z')


def test_generic_action_reconciliation_finishes_its_matching_timer(controller, runtime):
    session, _, state = runtime
    timer_id = str(uuid4())
    controller.dispatch({'cmd': 'quick_record_timer_start', **scope(controller), 'timer_id': timer_id,
        'definition_id': TRACK, 'choice_digest': DIGEST, 'start_time': NOW.isoformat()})
    frozen = controller.state.stop_timer(timer_id, account_id=ACCOUNT, epoch=session.epoch,
        ended_at=NOW + timedelta(hours=1), comment='Saved note')
    state.update(action_id=frozen['action_id'], delay=True)
    controller.dispatch({'cmd': 'quick_record_timer_reconcile', **scope(controller), 'timer_id': timer_id})
    state['rows'][(DESTINATION, state['delayed'][0]['id'])] = state['delayed'][0]
    result = controller.dispatch({'cmd': 'quick_record_reconcile', **scope(controller), 'action_id': frozen['action_id']})
    assert result['timer']['status'] == 'verified' and controller.state.timers(ACCOUNT) == []


def test_older_actions_are_discoverable_with_account_bound_recovery_pagination(controller, runtime):
    _, _, state = runtime
    identities = []
    for _ in range(3):
        intent = request(controller)
        state['action_id'] = intent['action_id']
        controller.dispatch(intent)
        identities.append(intent['action_id'])
    seen, cursor = [], None
    while True:
        result = controller.dispatch({'cmd': 'quick_record_actions', **scope(controller),
                                      'limit': 1, 'cursor': cursor, 'action_kind': 'record'})
        seen.extend(action['action_id'] for action in result['actions'])
        assert all('note' not in action and 'request' not in action and 'payload' not in action
                   for action in result['actions'])
        cursor = result['next_cursor']
        if cursor is None:
            break
    assert seen == list(reversed(identities))


@pytest.mark.parametrize('change', [{'end_time': NOW.isoformat()}, {'comment': {'private': 'bad'}},
    {'unexpected': True}, {'action_id': 'bad'}, {'choice_digest': 'bad'}])
def test_invalid_request_does_not_create_an_action(controller, runtime, change):
    _, journal, state = runtime
    with pytest.raises(ValueError):
        controller.dispatch(request(controller, **change))
    assert journal.list_actions(account_id=ACCOUNT).actions == () and not state['posts']
