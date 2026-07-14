from fulcra_gmail import rules_preview


def _msg(mid, frm, subject):
    return {"id": mid, "payload": {"headers": [
        {"name": "From", "value": frm}, {"name": "Subject", "value": subject},
    ]}}


def test_preview_counts_and_verifies_labels():
    rule = {"id": "r1", "version": 1, "name": "n",
            "match": "from:shop.example", "actions": ["file"],
            "subject_regex": "(?i)receipt"}
    candidates = [
        _msg("1", "r@shop.example", "Your receipt"),      # matches
        _msg("2", "r@shop.example", "newsletter"),        # subject rejects
        _msg("3", "x@other.example", "receipt here"),     # (server q wouldn't return; included to prove evaluate runs)
    ]
    res = rules_preview.preview(rule, candidates, "acct",
                                positives={"1"}, negatives={"2"})
    assert res.match_count == 1
    assert res.positives_caught == ["1"]
    assert res.negatives_caught == []      # "2" correctly excluded
    assert len(res.sample) == 1
    assert res.sample[0]["message_id"] == "1"


def test_preview_flags_caught_negative():
    rule = {"id": "r1", "version": 1, "name": "n",
            "match": "from:shop.example", "actions": ["file"]}
    candidates = [_msg("9", "r@shop.example", "anything")]
    res = rules_preview.preview(rule, candidates, "acct",
                                positives=set(), negatives={"9"})
    assert res.negatives_caught == ["9"]   # rule too loose — caught a ✗


def test_preview_rejects_invalid_rule():
    import pytest
    bad = {"id": "r1", "version": 1, "name": "n", "match": "x",
           "actions": ["file"], "subject_regex": "["}
    with pytest.raises(ValueError):
        rules_preview.preview(bad, [], "acct", set(), set())
