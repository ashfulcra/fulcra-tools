"""Guard tests for the macOS notification poster.

Regression: ``UNUserNotificationCenter.currentNotificationCenter()`` raises an
*uncatchable* NSException (it aborts via ``dispatch_once``) when the process
has no app-bundle identifier — i.e. when launched as ``python -m
fulcra_menubar`` from a venv. The poster must therefore SKIP the framework
entirely when unbundled and fall back to a stdout line. These tests pin that
behaviour and run on every platform (the predicate is monkeypatched, so no
real PyObjC call is made)."""
from __future__ import annotations

import fulcra_menubar._notify_macos as nm


def test_post_notification_prints_when_not_in_bundle(monkeypatch, capsys):
    monkeypatch.setattr(nm, "running_in_app_bundle", lambda: False)
    nm.post_notification("Daemon stopped", "It died.")
    out = capsys.readouterr().out
    assert "Daemon stopped" in out
    assert "It died." in out


def test_request_authorization_is_noop_when_not_in_bundle(monkeypatch):
    monkeypatch.setattr(nm, "running_in_app_bundle", lambda: False)
    # Must return without raising and without touching UserNotifications.
    nm.request_authorization()


def test_running_in_app_bundle_false_without_bundle_identifier():
    # Linux: Foundation import fails -> False. macOS dev / venv: mainBundle has
    # no bundle identifier -> False. Either way, the unbundled case is False,
    # which is exactly the condition that must gate the framework call.
    assert nm.running_in_app_bundle() is False
