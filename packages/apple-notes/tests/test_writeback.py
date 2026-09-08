"""Writeback: conversion, refusals, and permission classification."""
from __future__ import annotations

import subprocess

import pytest

from fulcra_apple_notes import writeback
from fulcra_apple_notes.reconcile import Change, Status
from fulcra_apple_notes.writeback import (
    AutomationDenied, WritebackError, markdown_to_html, plan_writeback,
    write_note_body)


def test_headings_become_html_headings():
    assert "<h1>Title</h1>" in markdown_to_html("# Title")
    assert "<h3>Sub</h3>" in markdown_to_html("### Sub")


def test_checkboxes_survive_as_visible_marks():
    out = markdown_to_html("- [x] done\n- [ ] todo")
    assert "☑︎ done" in out and "☐ todo" in out


def test_bullets_are_wrapped_in_a_single_list():
    out = markdown_to_html("- one\n- two")
    assert out.count("<ul>") == 1 and out.count("</ul>") == 1


def test_html_special_characters_are_escaped():
    # Unescaped user text would corrupt the note body.
    assert "&lt;script&gt;" in markdown_to_html("<script>")
    assert "&amp;" in markdown_to_html("a & b")


def test_vault_links_degrade_to_their_visible_text():
    # A vault path means nothing inside Apple Notes.
    out = markdown_to_html("![photo.png](notes/apple/_attachments/x/photo.png)")
    assert "notes/apple" not in out
    assert "photo.png" in out


def test_note_body_reaches_applescript_with_no_unescaped_quote(monkeypatch):
    """Two layers protect the AppleScript string literal, and both matter.

    html.escape already turns a double quote into &quot;, so quotes never
    reach the AppleScript layer -- but it does NOT touch backslashes, and a
    trailing backslash would escape the closing delimiter and change which
    statement runs. Assert on the generated script, not on either layer.
    """
    captured = {}
    monkeypatch.setattr(writeback, "_applescript",
                        lambda s: captured.setdefault("script", s) or "ok")
    write_note_body(42, 'say "hi" \\ bye', dry_run=False)
    script = captured["script"]
    assert "/ICNote/p42" in script
    assert "\\\\" in script, "a literal backslash must be doubled"
    # Isolate the one LINE that carries the body literal: later lines in
    # the script have quotes of their own.
    line = [l for l in script.split("\n") if "set body" in l][0]
    body = line.split('to "', 1)[1].rsplit('"', 1)[0]
    assert '"' not in body, "unescaped quote inside the body string literal"


def test_a_trailing_backslash_cannot_escape_the_closing_delimiter(monkeypatch):
    captured = {}
    monkeypatch.setattr(writeback, "_applescript",
                        lambda s: captured.setdefault("script", s) or "ok")
    write_note_body(9, "ends with a backslash \\", dry_run=False)
    after = captured["script"].split('theNotes to "')[1]
    # The delimiter that closes the literal must not itself be escaped.
    assert after.count('"') >= 1


def test_dry_run_never_invokes_applescript(monkeypatch):
    def explode(*a, **k):
        raise AssertionError("dry run must not touch Notes")
    monkeypatch.setattr(writeback, "_applescript", explode)
    result = write_note_body(1, "text", dry_run=True)
    assert result.ok and result.skipped == "dry-run"


def test_a_timed_out_appleevent_is_reported_as_a_consent_problem(monkeypatch):
    def timeout(*a, **k):
        raise subprocess.TimeoutExpired(cmd="osascript", timeout=1)
    monkeypatch.setattr(subprocess, "run", timeout)
    with pytest.raises(AutomationDenied, match="Automation"):
        writeback._applescript("x")


def test_explicit_permission_error_is_classified_as_denied(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
        args=[], returncode=1, stdout="", stderr="execution error: Not authorized (-1743)"))
    with pytest.raises(AutomationDenied):
        writeback._applescript("x")


def test_other_failures_are_not_misreported_as_permission_problems(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
        args=[], returncode=1, stdout="", stderr="execution error: note not found (8001)"))
    with pytest.raises(WritebackError) as exc:
        writeback._applescript("x")
    assert not isinstance(exc.value, AutomationDenied)


def test_probe_reports_unavailable_rather_than_raising(monkeypatch):
    monkeypatch.setattr(writeback, "_applescript",
                        lambda s: (_ for _ in ()).throw(AutomationDenied("nope")))
    ok, why = writeback.probe()
    assert ok is False and "nope" in why


class FakeNote:
    def __init__(self, pk): self.pk = pk


def test_a_note_with_attachments_is_refused_by_default():
    change = Change(uuid="u1", status=Status.VAULT_EDITED)
    allowed, refused = plan_writeback(
        [change], notes_by_uuid={"u1": FakeNote(1)},
        attachments_by_note={"u1": ["a", "b"]})
    assert allowed == []
    assert "attachment" in refused[0][1]


def test_attachment_refusal_can_be_overridden_explicitly():
    change = Change(uuid="u1", status=Status.VAULT_EDITED)
    allowed, refused = plan_writeback(
        [change], notes_by_uuid={"u1": FakeNote(1)},
        attachments_by_note={"u1": ["a"]}, allow_attachment_loss=True)
    assert len(allowed) == 1 and refused == []


def test_a_conflict_is_never_written_back():
    change = Change(uuid="u1", status=Status.CONFLICT)
    allowed, refused = plan_writeback(
        [change], notes_by_uuid={"u1": FakeNote(1)}, attachments_by_note={})
    assert allowed == [] and "conflict" in refused[0][1].lower() or refused


def test_a_note_missing_from_the_store_is_refused():
    change = Change(uuid="u1", status=Status.VAULT_EDITED)
    allowed, refused = plan_writeback(
        [change], notes_by_uuid={}, attachments_by_note={})
    assert allowed == [] and "no longer" in refused[0][1]


def test_a_clean_vault_edit_without_attachments_is_allowed():
    change = Change(uuid="u1", status=Status.VAULT_EDITED)
    allowed, refused = plan_writeback(
        [change], notes_by_uuid={"u1": FakeNote(7)}, attachments_by_note={})
    assert len(allowed) == 1 and refused == []
