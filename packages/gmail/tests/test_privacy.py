"""B2 — the #1 privacy gate.

A candidate that hits the server ``q`` but is REJECTED by the local
post-filters must leave ZERO residue and emit NO subject / from / body /
snippet text to any log sink at any level.
"""
from __future__ import annotations

import logging

from fulcra_gmail import convert, rules
from fulcra_gmail.ledger import Ledger, LedgerEntry

from .conftest import b64url, header, make_message

# Sensitive fixture strings that must NEVER reach a log sink.
_SUBJECT = "SENSITIVE_SUBJECT_your_bank_statement_2026"
_FROM = "victim.name@private-bank.example"
_BODY = "SENSITIVE_BODY_account_balance_1234567"
_SNIPPET = "SENSITIVE_SNIPPET_dear_customer"

_SENSITIVE = (_SUBJECT, _FROM, _BODY, _SNIPPET)


def _rejected_candidate():
    """A message that hit the server q but every local post-filter rejects."""
    (rule,) = rules.parse_rules([{
        "id": "r1", "version": 1, "name": "n", "match": "subject:statement",
        "actions": ["file", "relay"], "relay_to": "agent:claude",
        # None of these will match the message → guaranteed reject.
        "from_regex": r".*@allowed\.example\.com",
        "subject_regex": r"(?i)receipt",
        "has_attachment": True,
    }])
    payload = {
        "mimeType": "text/plain",
        "headers": [header("From", _FROM), header("Subject", _SUBJECT)],
        "body": {"data": b64url(_BODY)},
    }
    msg = {"id": "m-secret", "threadId": "t1", "snippet": _SNIPPET,
           "payload": payload}
    return rule, msg


def _run_pipeline(rule, msg, ledger, account_id, *, convert_spy, append_spy):
    """The intended Task-3 guard, in miniature: convert + ledger ONLY on an
    EFFECTIVE match. A rejected candidate short-circuits before any downstream
    call, so no File/ledger/bus residue is ever produced."""
    decision = rules.evaluate(rule, msg, account_id=account_id)
    if decision.matched:
        convert_spy(msg)
        ledger.append(LedgerEntry.file_done(
            account_id=account_id, message_id=msg["id"], rule_id=rule.id,
            rule_version=rule.version, sha256="h", destination="d",
        ))
        append_spy(msg["id"])
    return decision


def test_rejected_candidate_leaves_zero_residue(tmp_path, caplog, mocker):
    caplog.set_level(logging.DEBUG)  # capture EVERY level, root logger
    rule, msg = _rejected_candidate()
    account_id = "acct-secret-0000"

    convert_spy = mocker.spy(convert, "to_selected_email")
    ledger = Ledger(account_id, root=tmp_path)
    append_spy = mocker.spy(ledger, "append")

    decision = _run_pipeline(
        rule, msg, ledger, account_id,
        convert_spy=convert.to_selected_email, append_spy=lambda _id: None,
    )

    # (1) Effective-match rejected.
    assert decision.matched is False

    # (2) convert + ledger were NEVER invoked for the rejected candidate.
    assert convert_spy.call_count == 0
    assert append_spy.call_count == 0

    # (3) Zero ledger / state residue — the file is never even created.
    assert ledger.entries() == []
    assert not ledger.path.exists()

    # (4) NO PII in caplog at ANY level — scan message text AND record args.
    for record in caplog.records:
        rendered = record.getMessage()
        for secret in _SENSITIVE:
            assert secret not in rendered, f"PII in log message: {secret!r}"
        for arg in (record.args or ()):
            assert not (isinstance(arg, str) and any(s in arg for s in _SENSITIVE))

    # Full-blob backstop across everything caplog captured.
    blob = caplog.text
    for secret in _SENSITIVE:
        assert secret not in blob


def test_pipeline_guard_is_not_vacuous(tmp_path, mocker):
    """Sibling of the zero-residue test: prove the SAME guard DOES invoke
    convert + ledger on an effective match, so the zero-residue assertion is
    meaningful (it isn't just never calling anything)."""
    (rule,) = rules.parse_rules([{
        "id": "r1", "version": 1, "name": "n", "match": "in:inbox",
        "actions": ["file"],
    }])
    msg = make_message(headers=[header("Subject", "harmless")])
    account_id = "acct-ok"
    ledger = Ledger(account_id, root=tmp_path)

    calls = {"convert": 0, "append": 0}

    def convert_spy(_m):
        calls["convert"] += 1

    def append_spy(_id):
        calls["append"] += 1

    decision = _run_pipeline(
        rule, msg, ledger, account_id,
        convert_spy=convert_spy, append_spy=append_spy,
    )
    assert decision.matched is True
    assert calls == {"convert": 1, "append": 1}
    assert len(ledger.entries()) == 1


def test_evaluate_debug_log_contains_only_opaque_facts(caplog):
    """Directly assert the rules logger's DEBUG line carries account_id,
    message_id, rule_id, decision + reason — and NOTHING else sensitive."""
    caplog.set_level(logging.DEBUG, logger="fulcra_gmail.rules")
    rule, msg = _rejected_candidate()
    rules.evaluate(rule, msg, account_id="acct-secret-0000")

    rules_logs = [r for r in caplog.records if r.name == "fulcra_gmail.rules"]
    assert rules_logs, "expected a DEBUG decision log line"
    for record in rules_logs:
        rendered = record.getMessage()
        assert "m-secret" in rendered  # opaque message id is allowed
        assert "r1" in rendered  # rule id is allowed
        for secret in _SENSITIVE:
            assert secret not in rendered
