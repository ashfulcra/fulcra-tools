"""Rules engine: parse, server-q builder, local post-filters + reason codes."""
from __future__ import annotations

import pytest

from fulcra_gmail import rules
from fulcra_gmail.rules import MatchReason

from .conftest import attachment_part, header, make_message

# A fixed instant for deterministic query-window math.
_CURSOR_EPOCH = 1_700_000_000
_OVERLAP = 24 * 3600


# -- parsing ----------------------------------------------------------------


def test_parse_full_rule():
    (rule,) = rules.parse_rules([{
        "id": "receipts",
        "version": 3,
        "name": "Receipts",
        "match": "subject:receipt",
        "from_regex": r".*@shop\.example\.com",
        "subject_regex": r"(?i)receipt",
        "has_attachment": True,
        "actions": ["file", "relay"],
        "relay_to": "agent-x",
        "relay_priority": "high",
        "accounts": ["acct-1", "person@example.com"],
        "backfill": 90,
    }])
    assert rule.id == "receipts"
    assert rule.version == 3
    assert rule.match == "subject:receipt"
    assert rule.from_regex == r".*@shop\.example\.com"
    assert rule.has_attachment is True
    assert rule.actions == ["file", "relay"]
    assert rule.relay_to == "agent-x"
    assert rule.accounts == ["acct-1", "person@example.com"]
    assert rule.backfill == 90


def test_parse_minimal_rule_defaults():
    (rule,) = rules.parse_rules([{
        "id": "r1",
        "version": 1,
        "name": "All inbox",
        "match": "in:inbox",
        "actions": ["file"],
    }])
    assert rule.from_regex is None
    assert rule.subject_regex is None
    assert rule.has_attachment is None
    assert rule.relay_to is None
    assert rule.accounts is None  # omitted == all authorized accounts
    assert rule.backfill is None


def test_parse_rejects_unknown_action():
    with pytest.raises(ValueError, match="action"):
        rules.parse_rules([{
            "id": "r1", "version": 1, "name": "n", "match": "in:inbox",
            "actions": ["file", "delete"],
        }])


def test_parse_rejects_relay_action_without_relay_to():
    # P1-b: a relay action with no recipient is rejected at parse time (naming
    # the rule id + the missing field), so a misconfigured relay rule can never
    # silently complete a message without relaying it.
    with pytest.raises(ValueError, match=r"r1.*relay_to"):
        rules.parse_rules([{
            "id": "r1", "version": 1, "name": "n", "match": "in:inbox",
            "actions": ["file", "relay"],  # no relay_to
        }])


def test_parse_accepts_relay_action_with_relay_to():
    (rule,) = rules.parse_rules([{
        "id": "r1", "version": 1, "name": "n", "match": "in:inbox",
        "actions": ["file", "relay"], "relay_to": "agent:claude",
    }])
    assert rule.relay_to == "agent:claude"


def test_parse_rejects_missing_required_field():
    with pytest.raises(ValueError):
        rules.parse_rules([{"id": "r1", "name": "n", "match": "x",
                            "actions": ["file"]}])  # no version


def test_parse_rejects_malformed_from_regex_naming_rule_and_field():
    with pytest.raises(ValueError, match=r"r1.*from_regex"):
        rules.parse_rules([{
            "id": "r1", "version": 1, "name": "n", "match": "in:inbox",
            "actions": ["file"], "from_regex": "[",  # unbalanced → re.error
        }])


def test_parse_rejects_malformed_subject_regex_naming_rule_and_field():
    with pytest.raises(ValueError, match=r"r1.*subject_regex"):
        rules.parse_rules([{
            "id": "r1", "version": 1, "name": "n", "match": "in:inbox",
            "actions": ["file"], "subject_regex": "(",  # unterminated group
        }])


def test_malformed_regex_never_reaches_evaluate():
    # parse raises BEFORE any Rule is produced, so a bad pattern can never
    # blow up inside evaluate() on the first candidate.
    with pytest.raises(ValueError):
        rules.parse_rules([{
            "id": "r1", "version": 1, "name": "n", "match": "in:inbox",
            "actions": ["file"], "from_regex": "[",
        }])
    # A valid rule with the same shape still parses + evaluates fine.
    (rule,) = rules.parse_rules([{
        "id": "r1", "version": 1, "name": "n", "match": "in:inbox",
        "actions": ["file"], "from_regex": r".*@ok\.example\.com",
    }])
    msg = make_message(headers=[header("From", "a@ok.example.com")])
    assert rules.evaluate(rule, msg, account_id="a").matched


# -- rule identity ----------------------------------------------------------


def test_rule_identity_is_id_version_tuple():
    (rule,) = rules.parse_rules([{
        "id": "r1", "version": 7, "name": "n", "match": "x", "actions": ["file"],
    }])
    assert rules.rule_identity(rule) == ("r1", 7)


# -- accounts targeting -----------------------------------------------------


def test_accounts_omitted_applies_to_all():
    (rule,) = rules.parse_rules([{
        "id": "r1", "version": 1, "name": "n", "match": "x", "actions": ["file"],
    }])
    assert rule.applies_to_account("acct-anything", "whoever@example.com")


def test_accounts_membership_by_id_or_email():
    (rule,) = rules.parse_rules([{
        "id": "r1", "version": 1, "name": "n", "match": "x", "actions": ["file"],
        "accounts": ["acct-1", "person@example.com"],
    }])
    assert rule.applies_to_account("acct-1", "ignored@example.com")
    assert rule.applies_to_account("acct-other", "person@example.com")
    assert not rule.applies_to_account("acct-other", "nobody@example.com")


# -- server-q builder -------------------------------------------------------


def _rule(**over):
    base = {"id": "r1", "version": 1, "name": "n", "match": "subject:receipt",
            "actions": ["file"]}
    base.update(over)
    return rules.parse_rules([base])[0]


def test_query_with_cursor_subtracts_24h_overlap():
    q = rules.build_query(_rule(), cursor_epoch=_CURSOR_EPOCH)
    assert q == f"subject:receipt after:{_CURSOR_EPOCH - _OVERLAP}"


def test_query_first_run_no_cursor_bounds_7d():
    q = rules.build_query(_rule(), cursor_epoch=None)
    assert q == "subject:receipt newer_than:7d"


def test_query_first_run_backfill_widens_window():
    q = rules.build_query(_rule(backfill=90), cursor_epoch=None)
    assert q == "subject:receipt newer_than:90d"


def test_query_backfill_ignored_once_cursor_exists():
    # An established cursor always uses the overlap window, never backfill.
    q = rules.build_query(_rule(backfill=90), cursor_epoch=_CURSOR_EPOCH)
    assert q == f"subject:receipt after:{_CURSOR_EPOCH - _OVERLAP}"


# -- local post-filters (effective match) -----------------------------------


def test_no_postfilters_matches():
    rule = _rule()
    msg = make_message(headers=[header("From", "a@example.com"),
                                header("Subject", "Receipt")])
    d = rules.evaluate(rule, msg, account_id="acct-1")
    assert d.matched is True
    assert d.reason is MatchReason.MATCHED


def test_from_regex_accept():
    rule = _rule(from_regex=r".*@shop\.example\.com")
    msg = make_message(headers=[header("From", "sales@shop.example.com")])
    assert rules.evaluate(rule, msg, account_id="a").matched


def test_from_regex_reject_reason():
    rule = _rule(from_regex=r".*@shop\.example\.com")
    msg = make_message(headers=[header("From", "spam@other.example.net")])
    d = rules.evaluate(rule, msg, account_id="a")
    assert d.matched is False
    assert d.reason is MatchReason.REJECTED_FROM_REGEX


def test_subject_regex_reject_reason():
    rule = _rule(subject_regex=r"(?i)invoice")
    msg = make_message(headers=[header("Subject", "your receipt")])
    d = rules.evaluate(rule, msg, account_id="a")
    assert d.matched is False
    assert d.reason is MatchReason.REJECTED_SUBJECT_REGEX


def test_has_attachment_required_but_absent_reject():
    rule = _rule(has_attachment=True)
    msg = make_message(headers=[header("Subject", "no files")])
    d = rules.evaluate(rule, msg, account_id="a")
    assert d.matched is False
    assert d.reason is MatchReason.REJECTED_HAS_ATTACHMENT


def test_has_attachment_required_and_present_matches():
    rule = _rule(has_attachment=True)
    payload = {
        "mimeType": "multipart/mixed",
        "headers": [header("Subject", "with file")],
        "parts": [attachment_part("r.pdf", "application/pdf", "att-1", 10)],
    }
    msg = make_message(payload=payload)
    assert rules.evaluate(rule, msg, account_id="a").matched


def test_postfilter_order_from_checked_first():
    # Every post-filter would reject; evaluate short-circuits on the FIRST
    # (from_regex) and reports that reason.
    rule = _rule(from_regex=r"nomatch", subject_regex=r"nomatch",
                 has_attachment=True)
    msg = make_message(headers=[header("From", "x@example.com"),
                                header("Subject", "y")])
    assert rules.evaluate(rule, msg, account_id="a").reason \
        is MatchReason.REJECTED_FROM_REGEX


def test_enabled_defaults_true_and_parses():
    (r_default,) = rules.parse_rules([
        {"id": "r1", "version": 1, "name": "n", "match": "in:inbox", "actions": ["file"]},
    ])
    assert r_default.enabled is True
    (r_off,) = rules.parse_rules([
        {"id": "r2", "version": 1, "name": "n", "match": "in:inbox",
         "actions": ["file"], "enabled": False},
    ])
    assert r_off.enabled is False


def test_rule_to_config_dict_roundtrips():
    raw = {
        "id": "r1", "version": 2, "name": "Receipts", "match": "from:shop.example",
        "actions": ["file", "relay"], "relay_to": "amy", "relay_priority": "P2",
        "subject_regex": "(?i)receipt", "has_attachment": True, "enabled": False,
    }
    (rule,) = rules.parse_rules([raw])
    out = rules.rule_to_config_dict(rule)
    # Re-parsing the serialized dict yields an equal rule.
    (roundtripped,) = rules.parse_rules([out])
    assert roundtripped == rule
    assert "from_regex_re" not in out and "subject_regex_re" not in out


def test_rule_summary_is_human_and_pii_free():
    (rule,) = rules.parse_rules([
        {"id": "r1", "version": 1, "name": "n", "match": "from:a@b.example has:attachment",
         "actions": ["file", "relay"], "relay_to": "amy", "subject_regex": "(?i)receipt"},
    ])
    s = rules.rule_summary(rule)
    assert "from:a@b.example" in s and "relay" in s and "amy" in s
