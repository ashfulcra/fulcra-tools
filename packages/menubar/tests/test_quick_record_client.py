"""Native capture is durable before IO and repeats its frozen request only."""
from pathlib import Path
from uuid import UUID

import pytest

from fulcra_collect.record_actions import ActionJournal
from fulcra_menubar.quick_record_requests import ClientRequestJournal

ACCOUNT = 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee'
OTHER = 'aaaaaaaa-bbbb-4ccc-8ddd-ffffffffffff'
IDENTITY = str(UUID(int=1))


def body(**changes):
    return {'operation': 'record_annotation', 'protocol_version': 2, 'account_id': ACCOUNT,
            'config_epoch': 'captured', 'action_id': IDENTITY, 'definition_id': str(UUID(int=2)),
            'choice_digest': 'a' * 64, 'start_time': '2026-01-02T12:00:00Z',
            'comment': 'Synthetic note 🐈', **changes}


def reply(**changes):
    return {'protocol_version': 2, 'account_id': ACCOUNT, 'config_epoch': 'captured',
            'ok': True, 'accepted': True, 'status': 'verified', 'action_id': IDENTITY,
            'operation': 'record', 'captured_epoch': 'captured', 'record_id': str(UUID(int=3)),
            'data_type': 'Event/11111111-2222-4333-8444-555555555555', **changes}


def scope(**changes):
    return {'protocol_version': 2, 'account_id': ACCOUNT, 'config_epoch': 'captured', **changes}


class SyntheticDaemon:
    def __init__(self, journal):
        self.journal, self.calls, self.fail = journal, [], True

    def quick_record_v2(self, operation, **fields):
        saved = self.journal.get(IDENTITY, account_id=ACCOUNT)
        assert saved.status == 'pending' and saved.body == body()
        self.calls.append({'operation': operation, **fields})
        if self.fail:
            raise TimeoutError('synthetic socket response lost')
        return reply()


@pytest.fixture
def runtime(tmp_path: Path):
    from fulcra_menubar.quick_record_client import QuickRecordClient
    journal = ClientRequestJournal(ActionJournal(tmp_path / 'native-client.sqlite'))
    daemon = SyntheticDaemon(journal)
    return QuickRecordClient(daemon, journal), journal, daemon


def test_socket_failure_after_durable_capture_survives_app_restart_and_reuses_original(runtime):
    client, journal, daemon = runtime
    with pytest.raises(TimeoutError):
        client.submit(request_id=IDENTITY, body=body(), current_scope=scope())
    assert journal.get(IDENTITY, account_id=ACCOUNT).status == 'pending'
    fresh = type(client)(daemon, ClientRequestJournal(ActionJournal(journal.journal.path)))
    daemon.fail = False
    assert fresh.retry(IDENTITY, current_scope=scope()) == reply()
    assert daemon.calls == [body(), body()]
    assert journal.get(IDENTITY, account_id=ACCOUNT).status == 'verified'
    assert fresh.retry(IDENTITY, current_scope=scope()) == reply()
    assert len(daemon.calls) == 2


@pytest.mark.parametrize('current', [scope(account_id=OTHER), scope(config_epoch='new'), scope(protocol_version=True)])
def test_changed_live_view_never_rescopes_or_sends_saved_request(runtime, current):
    client, journal, daemon = runtime
    journal.capture(request_id=IDENTITY, body=body())
    with pytest.raises(ValueError, match='scope|account|epoch|protocol'):
        client.retry(IDENTITY, current_scope=current)
    assert daemon.calls == [] and journal.get(IDENTITY, account_id=ACCOUNT).body == body()


def test_changed_ui_body_cannot_replace_pending_capture_or_emit_another_request(runtime):
    client, journal, daemon = runtime
    journal.capture(request_id=IDENTITY, body=body())
    with pytest.raises(ValueError, match='changed'):
        client.submit(request_id=IDENTITY, body=body(comment='Changed UI note'), current_scope=scope())
    assert daemon.calls == [] and journal.get(IDENTITY, account_id=ACCOUNT).body == body()


def view(**changes):
    return scope(ok=True, definitions=[{'id': str(UUID(int=2)), 'name': 'Synthetic choice',
        'annotation_type': 'moment', 'digest': 'a' * 64, 'pinned': False}],
        favorites=[], favorites_revision=0, recent=[], timers=[]) | changes


def test_bootstrap_supplies_a_complete_account_scope_without_creating_an_intent(runtime):
    client, journal, daemon = runtime
    daemon.quick_record_v2 = lambda operation, **fields: view()
    assert client.bootstrap() == view()
    assert journal.pending(account_id=ACCOUNT)['requests'] == ()


@pytest.mark.parametrize('change', [{'ok': False}, {'protocol_version': True},
    {'favorites_revision': True}, {'definitions': None}, {'favorites': None}])
def test_bad_handshake_is_never_a_clean_empty_picker(runtime, change):
    client, _, daemon = runtime
    daemon.quick_record_v2 = lambda operation, **fields: view(**change)
    with pytest.raises(ValueError, match='scope|protocol|handshake|favorite|choice'):
        client.bootstrap()


def test_pin_sends_one_scoped_diff_using_the_displayed_revision_and_digest(runtime):
    client, _, daemon = runtime
    calls = []
    def transport(operation, **fields):
        calls.append((operation, fields))
        return scope(ok=True, revision=1, favorites=[str(UUID(int=2))])
    daemon.quick_record_v2 = transport
    result = client.change_pin(str(UUID(int=2)), pinned=True, view=view())
    assert result['revision'] == 1
    assert calls == [('set_quick_record_favorites', {**scope(), 'choice_id': str(UUID(int=2)),
        'choice_digest': 'a' * 64, 'pinned': True, 'expected_revision': 0})]


@pytest.mark.parametrize('response', [scope(ok=False), scope(ok=True, account_id=OTHER, revision=1, favorites=[]),
                                    scope(ok=True, revision=True, favorites=[]), scope(ok=True, revision=1, favorites=None)])
def test_failed_or_wrong_scope_pin_reply_never_acknowledges_a_checkbox(runtime, response):
    client, _, daemon = runtime
    daemon.quick_record_v2 = lambda operation, **fields: response
    with pytest.raises(ValueError, match='response|scope|favorite'):
        client.change_pin(str(UUID(int=2)), pinned=True, view=view())


@pytest.mark.parametrize('change', [{'ok': False}, {'action_id': str(UUID(int=99))},
    {'record_id': str(UUID(int=99))}, {'data_type': 'Event/aaaaaaaa-2222-4333-8444-555555555555'}])
def test_reconcile_display_cannot_claim_a_contradictory_or_retargeted_completion(runtime, change):
    client, _, daemon = runtime
    action = reply(ok=False, status='submitted')
    daemon.quick_record_v2 = lambda *a, **k: reply(**change)
    with pytest.raises(ValueError, match='proof|response'):
        client.reconcile(view=view(), action=action)


def test_reconcile_display_accepts_bound_verified_action_without_capturing_a_new_request(runtime):
    client, journal, daemon = runtime
    calls = []
    def transport(operation, **fields):
        calls.append((operation, fields))
        return reply()
    daemon.quick_record_v2 = transport
    assert client.reconcile(view=view(), action=reply(ok=False, status='submitted')) == reply()
    assert calls == [('quick_record_reconcile', {**scope(), 'action_id': IDENTITY})]
    assert journal.pending(account_id=ACCOUNT)['requests'] == ()


def test_pending_action_pages_keep_old_epoch_visible_without_granting_replay(runtime):
    client, _, daemon = runtime
    calls = []
    observed = reply(ok=False, status='submitted', captured_epoch='old', name='Synthetic older action')
    observed.pop('account_id')
    observed.pop('config_epoch')
    observed.pop('protocol_version')
    def transport(operation, **fields):
        calls.append((operation, fields))
        return scope(ok=True, actions=[observed], next_cursor='opaque-synthetic')
    daemon.quick_record_v2 = transport
    page = client.pending_actions(view=view(), cursor='prior-synthetic')
    assert page['actions'][0]['needs_reauthorization'] is True
    assert page['next_cursor'] == 'opaque-synthetic'
    assert calls == [('quick_record_actions', {**scope(), 'statuses': ['pending', 'submitted'],
        'limit': 50, 'cursor': 'prior-synthetic'})]
    with pytest.raises(ValueError, match='old-epoch'):
        client.reconcile(view=view(), action=page['actions'][0])
    assert len(calls) == 1


@pytest.mark.parametrize('change', [{'account_id': OTHER}, {'ok': False}, {'actions': None}])
def test_pending_action_page_unknown_is_visible_not_clean_empty(runtime, change):
    client, _, daemon = runtime
    daemon.quick_record_v2 = lambda *a, **k: scope(ok=True, actions=[], next_cursor=None) | change
    with pytest.raises(ValueError, match='response|scope|page'):
        client.pending_actions(view=view())


@pytest.mark.parametrize('history', [
    {'recent': [reply(ok=False)]}, {'recent': [{'status': 'verified'}]},
    {'timers': [{'status': 'running'}]},
    {'timers': [{'timer_id': str(UUID(int=8)), 'definition_id': str(UUID(int=2)), 'name': 'Synthetic',
        'captured_epoch': 'captured', 'status': 'running', 'start_time': 'not-a-time', 'end_time': None, 'action_id': None}]},
])
def test_incomplete_or_contradictory_history_handshake_fails_before_gui_render(runtime, history):
    client, _, daemon = runtime
    daemon.quick_record_v2 = lambda *a, **k: view(**history)
    with pytest.raises(ValueError, match='proof|history|timer|response|UUID|epoch|time'):
        client.bootstrap()


@pytest.mark.parametrize('row', [None, {}, reply(ok=False, status='submitted'),
    reply(ok=False, status='submitted', name='x' * 257)])
def test_pending_page_malformed_display_row_fails_before_main_thread_render(runtime, row):
    client, _, daemon = runtime
    daemon.quick_record_v2 = lambda *a, **k: scope(ok=True, actions=[row], next_cursor=None)
    with pytest.raises(ValueError, match='response|page|name'):
        client.pending_actions(view=view())
