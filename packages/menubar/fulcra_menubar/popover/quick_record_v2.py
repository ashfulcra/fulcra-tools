"""Native protocol2 view: durable capture on click, socket IO off the UI thread."""
from datetime import datetime, timezone, timedelta
from threading import Thread
from uuid import uuid4

from .._dispatch import on_main_thread
from .._humanize import parse_duration_seconds
from .._objc_targets import attach, detach_view
from ..quick_record_client import native_client, _scope
from .._refresh_view import refresh_view


def make_view(*, client, model, on_view_status, width, height=360.0, active_timers=None,
              recent=None, on_timer_changed=None, on_preferences=None):
    from AppKit import NSButton, NSBezelStyleRounded, NSScrollView, NSTextField, NSView, NSMakeRect
    from ..theme import colors, typography
    from .quick_record import (_make_moment_row, _make_duration_row, _make_section_header,
                               _group_label, quick_record_view_state)
    active_timers = active_timers if active_timers is not None else {}
    recent = recent if recent is not None else []
    root = refresh_view(NSMakeRect(0, 0, width, height))
    root.setWantsLayer_(True)
    root.layer().setBackgroundColor_(colors.bg().CGColor())
    scroll = NSScrollView.alloc().initWithFrame_(NSMakeRect(0, 36, width, height - 76))
    scroll.setHasVerticalScroller_(True)
    scroll.setDrawsBackground_(False)
    from ..preferences.plugins_tab import _FlippedView
    content = _FlippedView.alloc().initWithFrame_(NSMakeRect(0, 0, width, 0))
    scroll.setDocumentView_(content)
    root.addSubview_(scroll)
    state = {'snapshot': None, 'pending': (), 'cursor': None, 'busy': False,
             'message': 'Loading quick record…', 'workflow': None, 'generation': 0, 'refresh_pending': False, 'actions': [], 'actions_cursor': None}
    drafts, draft_widgets = {}, {}
    message_label = NSTextField.labelWithString_(state['message'])
    message_label.setFont_(typography.small())
    message_label.setLineBreakMode_(0)
    message_label.setFrame_(NSMakeRect(12, height - 38, width - 24, 34))
    root.addSubview_(message_label)

    def message(value):
        state['message'] = value
        message_label.setStringValue_(value)

    def button(parent, title, frame, callback, enabled=True):
        widget = NSButton.alloc().initWithFrame_(NSMakeRect(*frame))
        widget.setTitle_(title)
        widget.setBezelStyle_(NSBezelStyleRounded)
        widget.setEnabled_(enabled)
        attach(widget, lambda _sender: callback())
        parent.addSubview_(widget)
        return widget

    button(root, 'View Status →', (width - 132, 5, 120, 25), on_view_status)
    button(root, 'Refresh', (12, 5, 76, 25), lambda: refresh())

    def worker(operation, finished):
        if state['busy']:
            return False
        state['busy'] = True
        generation = state['generation']
        def run():
            try:
                result, error = operation(), False
            except Exception:
                # Never display/log exception bodies or frozen note content.
                result, error = None, True
            @on_main_thread
            def deliver():
                state['busy'] = False
                if generation != state['generation'] or state['refresh_pending']:
                    state['refresh_pending'] = False
                    worker(load, loaded)
                    return
                finished(result, error)
            deliver()
        Thread(target=run, daemon=True, name='collect-quick-record').start()
        render()
        return True

    def load():
        if state['workflow'] is None:
            state['workflow'] = native_client(client)
        view = state['workflow'].bootstrap()
        page = state['workflow'].journal.pending(account_id=view['account_id'])
        return view, page

    def loaded(result, error):
        active_timers.clear()
        recent.clear()
        state.update(actions=[], actions_cursor=None)
        if error:
            state.update(snapshot=None, pending=(), cursor=None,
                         message='Quick record unavailable. Refresh or reconnect in Collect.')
        else:
            view, page = result
            state.update(snapshot=view, pending=page['requests'], cursor=page['next_cursor'])
            active_timers.update({timer['timer_id']: timer for timer in view['timers']})
            recent.extend(view['recent'])
        if on_timer_changed:
            on_timer_changed()
        render()

    def refresh():
        state['generation'] += 1
        if state['busy']:
            state['refresh_pending'] = True
            return
        state['message'] = ''
        worker(load, loaded)

    def finished_request(result, error):
        state['message'] = ('Request retained. Retry the saved request below; refresh if your sign-in changed.'
                            if error else ('Verified.' if result.get('status') == 'verified'
                            else 'Excluded from delivery.' if result.get('status') == 'excluded'
                            else 'Timer saved.' if result.get('timer') and result.get('ok')
                            else 'Pending verification. The original request is saved.'))
        worker(load, loaded)

    def matches(view):
        return state['snapshot'] is not None and _scope(view) == _scope(state['snapshot'])

    def draft_id(view, key):
        return (view['account_id'], view['config_epoch'], *key)

    def preserve_draft(view, key, row):
        identity = draft_id(view, key)
        fields = [widget for widget in row.subviews() if isinstance(widget, NSTextField) and widget.isEditable()]
        prior = drafts.get(identity)
        if prior is not None:
            for widget, value in zip(fields, prior):
                widget.setStringValue_(value)
        draft_widgets[identity] = fields

    def clear_draft(view, key):
        if key is not None:
            identity = draft_id(view, key)
            drafts.pop(identity, None)
            for widget in draft_widgets.get(identity, ()):
                widget.setStringValue_('')

    def send(view, operation, *, draft_key=None, **fields):
        if state['busy'] or not matches(view):
            return False
        workflow = state['workflow']
        identity = fields.get('action_id') or fields.get('timer_id')
        if operation == 'quick_record_timer_stop':
            prior = workflow.journal.timer_stop(fields['timer_id'], account_id=view['account_id'])
            if prior is not None:
                message('A stop is already saved. Retry its original request below.')
                return False
            identity = str(uuid4())
        body = {'operation': operation, **_scope(view), **fields}
        try:
            # Commit before starting the worker or acknowledging the click.
            workflow.capture(request_id=identity, body=body, current_scope=view)
        except Exception:
            message('Could not save this request. Your entry has not been sent.')
            return False
        clear_draft(view, draft_key)
        worker(lambda: workflow.retry(identity, current_scope=view), finished_request)
        return True

    def retry(request, view):
        if request.epoch != view['config_epoch']:
            state['message'] = 'This saved request needs explicit consent recovery after the settings or sign-in change.'
            render()
            return
        worker(lambda: state['workflow'].retry(request.request_id, current_scope=view), finished_request)

    def choose(view, identity):
        return next(choice for choice in view['definitions'] if choice['id'] == identity)

    def record(view, identity, name, note, duration=None):
        now = datetime.now(timezone.utc)
        fields = {'start_time': now.isoformat()}
        if duration is not None:
            try:
                seconds = parse_duration_seconds(duration)
                if seconds is None or seconds <= 0:
                    raise ValueError('invalid duration')
                fields = {'start_time': (now - timedelta(seconds=seconds)).isoformat(), 'end_time': now.isoformat()}
            except (ValueError, OverflowError):
                message('Enter a duration such as 90m or 1h 30m.')
                return False
        return send(view, 'record_annotation', draft_key=('choice', identity), action_id=str(uuid4()), definition_id=identity,
                    choice_digest=choose(view, identity)['digest'], comment=note or None, **fields)

    def pin(view, identity, pinned):
        if not matches(view):
            return
        worker(lambda: state['workflow'].change_pin(identity, pinned=not pinned, view=view),
               lambda result, error: pin_done(error))

    def pin_done(error):
        state['message'] = 'Pin changed; refresh before retrying.' if error else ''
        worker(load, loaded)

    def reconcile(view, *, action=None, timer=None):
        worker(lambda: state['workflow'].reconcile(view=view, action=action, timer=timer), finished_request)

    def next_page(view):
        cursor = state['cursor']
        def done(page, error):
            if error or not matches(view):
                state['message'] = 'Could not read the next pending page. Refresh before retrying.'
            else:
                state.update(pending=page['requests'], cursor=page['next_cursor'])
            render()
        worker(lambda: state['workflow'].journal.pending(account_id=view['account_id'], cursor=cursor), done)

    def pending_actions(view, cursor=None):
        def done(page, error):
            if error:
                state['message'] = 'Could not read pending recordings. Refresh before retrying.'
            else:
                state.update(actions=page['actions'], actions_cursor=page['next_cursor'])
                if not page['actions']:
                    state['message'] = 'No pending server actions on this page.'
            render()
        worker(lambda: state['workflow'].pending_actions(view=view, cursor=cursor), done)

    def render():
        for identity, fields in draft_widgets.items():
            drafts[identity] = tuple(str(widget.stringValue() or '') for widget in fields)
        draft_widgets.clear()
        for item in list(content.subviews()):
            detach_view(item)
            item.removeFromSuperview()
        y = 0.0
        def text(value, size=38):
            nonlocal y
            label = NSTextField.labelWithString_(value)
            label.setFont_(typography.small())
            label.setTextColor_(colors.text_secondary())
            label.setLineBreakMode_(0)
            label.setFrame_(NSMakeRect(12, y + 4, width - 24, size - 4))
            content.addSubview_(label)
            y += size
        def heading(value):
            nonlocal y
            item = _make_section_header(width, 24, value)
            item.setFrame_(NSMakeRect(0, y, width, 24))
            content.addSubview_(item)
            y += 24
        def action(label, caption, callback, enabled=True, with_note=False, draft_key=None):
            nonlocal y
            row_height = 78 if with_note else 52
            row = NSView.alloc().initWithFrame_(NSMakeRect(0, y, width, row_height))
            field = NSTextField.labelWithString_(label)
            field.setFont_(typography.small())
            field.setLineBreakMode_(0)
            field.setFrame_(NSMakeRect(12, 34 if with_note else 6, width - 110, 40))
            row.addSubview_(field)
            if with_note:
                note = NSTextField.alloc().initWithFrame_(NSMakeRect(12, 8, width - 120, 23))
                note.setPlaceholderString_('Comment (optional)')
                note.setFont_(typography.small())
                note.setEnabled_(enabled and not state['busy'])
                row.addSubview_(note)
                def clicked():
                    if callback(str(note.stringValue() or '')):
                        note.setStringValue_('')
            else:
                clicked = callback
            button(row, caption, (width - 96, 12, 84, 25), clicked, enabled and not state['busy'])
            if draft_key is not None:
                preserve_draft(view, draft_key, row)
            content.addSubview_(row)
            y += row_height
        message_label.setStringValue_(state['message'] or 'Quick Record')
        view = state['snapshot']
        if view is None:
            content.setFrame_(NSMakeRect(0, 0, width, max(y, height - 76)))
            return
        if state['busy']:
            text('Working… Your saved requests remain available.', 34)
        definitions, empty = quick_record_view_state(view['definitions'])
        if not definitions:
            text('No tracks yet. Create a track in Fulcra.' if empty == 'no_defs'
                 else 'Pin tracks in Preferences to record them here.')
            if on_preferences:
                action('Choose tracks', 'Preferences', lambda: on_preferences('annotations'))
        last_group = None
        for choice in definitions:
            kind = choice['annotation_type']
            if kind != last_group:
                heading(_group_label(kind))
                last_group = kind
            common = {'on_toggle_favorite': lambda identity, pinned, v=view: pin(v, identity, pinned)}
            if kind == 'duration':
                row = _make_duration_row(choice, width, 84,
                    on_record_inline=lambda identity, name, duration, note, v=view: record(v, identity, name, note, duration),
                    on_start_timer=lambda identity, name, v=view: send(v, 'quick_record_timer_start',
                        timer_id=str(uuid4()), definition_id=identity, choice_digest=choose(v, identity)['digest'],
                        start_time=datetime.now(timezone.utc).isoformat()),
                    **common)
                row_height = 84
            else:
                row = _make_moment_row(choice, width, 44,
                    on_record=lambda identity, name, note, v=view: record(v, identity, name, note), **common)
                row_height = 44
            row.setFrame_(NSMakeRect(0, y, width, row_height))
            preserve_draft(view, ('choice', choice['id']), row)
            # The worker owns IO; disable stale controls while it runs.
            for widget in row.subviews():
                if isinstance(widget, NSButton):
                    widget.setEnabled_(not state['busy'])
            content.addSubview_(row)
            y += row_height
        if view['timers']:
            heading('Saved timers')
        for timer in view['timers']:
            old = timer['captured_epoch'] != view['config_epoch']
            stopping = timer['status'] == 'stopping'
            started = datetime.fromisoformat(timer['start_time'].replace('Z', '+00:00')).astimezone().strftime('%b %d, %H:%M')
            action(f"{timer['name']} — {'needs consent recovery' if old else timer['status']}\nStarted {started}",
                'Retry stop' if stopping else 'Stop',
                (lambda t=timer, v=view: reconcile(v, timer=t))
                if stopping else (lambda note, t=timer, v=view: send(v, 'quick_record_timer_stop', draft_key=('timer', t['timer_id']), timer_id=t['timer_id'],
                    end_time=datetime.now(timezone.utc).isoformat(), comment=note or None)), not old, with_note=not stopping, draft_key=('timer', timer['timer_id']))
        if state['pending']:
            heading('Pending requests')
        for request in state['pending']:
            old = request.epoch != view['config_epoch']
            action(f"{request.operation.replace('quick_record_', '').replace('_', ' ')} — "
                   + ('needs consent recovery' if old else request.status), 'Retry',
                   lambda request=request, v=view: retry(request, v), not old)
        if state['cursor']:
            action('More saved requests', 'Next page', lambda v=view: next_page(v))
        action('Find pending recordings and Undo', 'Find', lambda v=view: pending_actions(v))
        if state['actions']:
            heading('Pending server actions')
        for entry in state['actions']:
            action(f"{entry['name']} — {entry['operation']} {entry['status']}"
                + ('; needs consent recovery' if entry['needs_reauthorization'] else ''), 'Check',
                lambda entry=entry, v=view: reconcile(v, action=entry), not entry['needs_reauthorization'])
        if state['actions_cursor']:
            action('More pending server actions', 'Next page',
                   lambda v=view: pending_actions(v, state['actions_cursor']))
        if recent:
            heading('Recent recordings')
        for entry in recent:
            status = 'undone' if entry.get('undone') else entry['status']
            undo_pending = entry.get('undo_status') in {'pending', 'submitted'}
            if undo_pending:
                status = 'Undo pending'
            action(f"{entry['name']} — {status}", 'Undo' if entry['status'] == 'verified' else 'Check',
                lambda e=entry, v=view: send(v, 'delete_annotation', action_id=str(uuid4()), record_action_id=e['action_id'])
                if e['status'] == 'verified' else reconcile(v, action=e),
                not entry.get('undone') and not entry.get('needs_reauthorization') and not undo_pending
                and entry['status'] in {'verified', 'pending', 'submitted'})
        content.setFrame_(NSMakeRect(0, 0, width, max(y, height - 76)))

    root._refresh_callback = refresh
    refresh()
    return root
