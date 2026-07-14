import json

import pytest

from fulcra_gmail import rules_ai


def _rec(mid, frm, subject):
    return {"message_id": mid, "from": frm, "subject": subject, "snippet": "s"}


def test_prompt_contains_examples_only():
    p = rules_ai.build_prompt([_rec("1", "r@shop.example", "receipt")], [])
    assert "r@shop.example" in p and "receipt" in p


def test_suggest_parses_and_validates_model_output():
    def fake_model(prompt):
        return json.dumps({
            "draft_rule": {"id": "ai", "version": 1, "name": "AI",
                           "match": "from:shop.example", "actions": ["file"]},
            "explanation": "Matches the shop sender.",
        })
    out = rules_ai.suggest([_rec("1", "r@shop.example", "receipt")], [],
                           call_model=fake_model)
    assert out["draft_rule"]["match"] == "from:shop.example"
    assert "shop" in out["explanation"]


def test_suggest_rejects_invalid_model_rule():
    def bad_model(prompt):
        return json.dumps({"draft_rule": {"actions": ["relay"]}, "explanation": "x"})
    with pytest.raises(ValueError):
        rules_ai.suggest([_rec("1", "a@x.example", "s")], [], call_model=bad_model)
