from fulcra_gmail import rules_derive
from fulcra_gmail.rules_derive import Chip


def _rec(mid, frm, subject, list_id=None, attach=False):
    return {"message_id": mid, "from": frm, "subject": subject,
            "list_id": list_id, "has_attachment": attach}


def test_shared_sender_becomes_from_match_chip():
    pos = [_rec("1", "Shop <r@shop.example>", "Your receipt #1"),
           _rec("2", "Shop <r@shop.example>", "Your receipt #2")]
    res = rules_derive.derive(pos, [])
    senders = [c for c in res.chips if c.kind == "sender" and c.on]
    assert senders and senders[0].value == "from:r@shop.example"
    assert "from:r@shop.example" in res.draft_rule["match"]


def test_domain_fallback_when_senders_differ():
    pos = [_rec("1", "a@shop.example", "receipt"),
           _rec("2", "b@shop.example", "receipt")]
    res = rules_derive.derive(pos, [])
    domain = [c for c in res.chips if c.kind == "domain" and c.on]
    assert domain and domain[0].value == "from:shop.example"


def test_shared_subject_keyword_becomes_subject_regex_chip():
    pos = [_rec("1", "a@x.example", "Receipt for order"),
           _rec("2", "b@y.example", "receipt attached")]
    res = rules_derive.derive(pos, [])
    subj = [c for c in res.chips if c.kind == "subject_kw" and c.on]
    assert subj and subj[0].field == "subject_regex"
    assert "receipt" in subj[0].value.lower()


def test_all_attachments_becomes_has_attachment_match_chip():
    pos = [_rec("1", "a@x.example", "s1", attach=True),
           _rec("2", "a@x.example", "s2", attach=True)]
    res = rules_derive.derive(pos, [])
    att = [c for c in res.chips if c.kind == "attachment" and c.on]
    assert att and att[0].value == "has:attachment"


def test_negative_subtracts_shared_trait():
    # Domain is shared by BOTH positives and a negative → dropped, not offered on.
    pos = [_rec("1", "a@shop.example", "receipt"),
           _rec("2", "b@shop.example", "receipt")]
    neg = [_rec("9", "noise@shop.example", "newsletter")]
    res = rules_derive.derive(pos, neg)
    on_domain = [c for c in res.chips if c.kind == "domain" and c.on]
    assert not on_domain  # the domain no longer separates pos from neg


def test_unseparable_sets_flag_refinement():
    pos = [_rec("1", "a@x.example", "hello")]
    neg = [_rec("2", "a@x.example", "hello")]  # identical traits
    res = rules_derive.derive(pos, neg)
    assert res.needs_refinement is True


def test_draft_from_chips_only_uses_on():
    chips = [
        Chip(kind="sender", field="match", value="from:a@x.example", label="", on=True),
        Chip(kind="attachment", field="match", value="has:attachment", label="", on=False),
        Chip(kind="subject_kw", field="subject_regex", value="(?i)receipt", label="", on=True),
    ]
    draft = rules_derive.draft_from_chips(chips)
    assert draft["match"] == "from:a@x.example"
    assert draft["subject_regex"] == "(?i)receipt"
    assert "has:attachment" not in draft["match"]
