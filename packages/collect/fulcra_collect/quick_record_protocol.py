"""Versioned transport boundary; no stale-account or legacy-write fallback."""
import logging

import httpx
from fulcra_common.v1 import _canonical_uuid

from . import config
from .quick_record_controller import COMMANDS, QuickRecordController, action_view, timer_view
from .record_actions import ActionJournal

_BOOTSTRAP = {"quick_record_list", "get_quick_record_favorites"}


def _refresh():
    return {"ok": False, "code": "refresh_required", "protocol_version": 2,
            "error": "Refresh quick record or reconnect to Fulcra in Collect before continuing."}


def handle_quick_record(request):
    operation = request.get("operation")
    if not isinstance(operation, str) or operation not in COMMANDS:
        return _refresh()
    if operation not in _BOOTSTRAP:
        try:
            if type(request.get("protocol_version")) is not int or request["protocol_version"] != 2:
                return _refresh()
            _canonical_uuid(request.get("account_id"))
            epoch = request.get("config_epoch")
            if not isinstance(epoch, str) or not 1 <= len(epoch) <= 256:
                return _refresh()
        except ValueError:
            return _refresh()
    body = {key: value for key, value in request.items() if key not in {"cmd", "operation"}}
    body["cmd"] = operation
    # Lazy authority setup keeps daemon construction and obsolete requests free
    # of Keychain reads. Every actual call uses fresh Collect context, not a
    # daemon startup config or previously authenticated list cache.
    from .quick_record_v1 import QuickRecordSession, quick_record_context
    try:
        ctx, account = quick_record_context()
        with QuickRecordSession(ctx, expected_account=request.get("account_id", account),
                                expected_epoch=request.get("config_epoch", ctx.config_epoch)) as session:
            journal = ActionJournal(config.config_dir() / "quick-record-actions.sqlite")
            controller = QuickRecordController(session, journal)
            try:
                return controller.dispatch(body)
            except (httpx.HTTPError, RuntimeError, ValueError) as exc:
                # First distinguish actual consent revocation from a delivery
                # or receipt/readback failure under still-valid authority.
                session.guard()
                reply = {"ok": False, "code": "invalid_request" if isinstance(exc, ValueError) else "upstream_error",
                         **controller.scope(), "error": str(exc) if isinstance(exc, ValueError) else
                         "Could not verify this capture. Check pending captures before trying again."}
                action = None
                identity = request.get("action_id")
                if identity is not None:
                    try:
                        _canonical_uuid(identity)
                    except ValueError:
                        identity = None
                    if identity is not None:
                        reply["action_id"] = identity
                        action = journal.inspect(identity, account_id=session.account_id)
                timer_id = request.get("timer_id")
                if timer_id is not None:
                    try:
                        _canonical_uuid(timer_id)
                    except ValueError:
                        timer_id = None
                if timer_id is not None:
                    with journal._db() as db:
                        timer = controller.state._timer(db, timer_id, session.account_id)
                    if timer is not None:
                        reply["timer"] = timer_view(timer)
                        reply["accepted"], reply["code"] = True, "pending"
                        reply["needs_reauthorization"] = timer["epoch"] != session.epoch
                        if timer.get("action_id"):
                            action = journal.inspect(timer["action_id"], account_id=session.account_id)
                if action is not None:
                    reply.update(action_view(action))
                    reply["ok"] = False
                    reply["code"] = "pending" if action.status in {"pending", "submitted"} else "action_retained"
                    reply["needs_reauthorization"] = action.epoch != session.epoch and action.status in {"pending", "submitted"}
                return reply
    except (ValueError, RuntimeError, TimeoutError):
        return _refresh()
    except Exception as exc:
        # No exception payload/traceback: source text and credentials are never
        # copied into activity/logs by the transport error boundary.
        logging.getLogger(__name__).error("quick-record-v2 failed type=%s", type(exc).__name__)
        return {"ok": False, "code": "internal_error", "protocol_version": 2,
                "error": "Collect could not verify this capture. Its saved state has been retained."}
