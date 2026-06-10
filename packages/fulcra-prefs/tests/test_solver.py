import pytest
from fulcra_prefs.solver import solve, VETO_THRESHOLD_DEFAULT

def doc(**keys):
    return {"v": 1, "compiled_at": "2026-06-10T12:00:00+00:00",
            "keys": {k: {"value": True, "weight": w, "confidence": 1.0,
                         "observed_at": "2026-06-01T00:00:00+00:00",
                         "n_signals": 1, "sources": ["test"]}
                     for k, w in keys.items()}}

OPTIONS = [
    {"id": "thai-spot",  "keys": ["dining.cuisine.thai", "dining.noise.quiet"]},
    {"id": "bbq-barn",   "keys": ["dining.cuisine.bbq"]},
    {"id": "pizza-place","keys": ["dining.cuisine.pizza"]},
]

def test_weighted_sum_ranks_by_total_weight():
    alice = {"alice": doc(**{"dining.cuisine.thai": 0.9, "dining.cuisine.bbq": 0.2})}
    res = solve(OPTIONS, alice, policy="weighted-sum")
    assert [o["id"] for o in res["ranked"]] == ["thai-spot", "bbq-barn", "pizza-place"]
    assert res["ranked"][0]["score"] == 0.9

def test_multi_participant_scores_sum():
    docs = {"alice": doc(**{"dining.cuisine.thai": 0.9}),
            "bob":   doc(**{"dining.cuisine.thai": -0.3, "dining.cuisine.bbq": 0.8})}
    res = solve(OPTIONS, docs, policy="weighted-sum")
    thai = next(o for o in res["ranked"] if o["id"] == "thai-spot")
    assert abs(thai["score"] - 0.6) < 1e-9

def test_hard_veto_removes_option_and_traces_it():
    docs = {"alice": doc(**{"dining.cuisine.bbq": 0.9}),
            "bob":   doc(**{"dining.cuisine.bbq": -0.8})}
    res = solve(OPTIONS, docs, policy="hard-veto")
    assert "bbq-barn" not in [o["id"] for o in res["ranked"]]
    assert any("veto" in line and "bob" in line for line in res["trace"])

def test_tie_breaks_lexicographically_by_option_id():
    res = solve(OPTIONS, {"alice": doc()}, policy="weighted-sum")
    assert [o["id"] for o in res["ranked"]] == ["bbq-barn", "pizza-place", "thai-spot"]

def test_trace_explains_every_option():
    res = solve(OPTIONS, {"alice": doc(**{"dining.cuisine.thai": 0.9})},
                policy="weighted-sum")
    for opt in OPTIONS:
        assert any(opt["id"] in line for line in res["trace"])

def test_unknown_policy_rejected():
    with pytest.raises(ValueError):
        solve(OPTIONS, {"alice": doc()}, policy="vibes")

def test_default_veto_threshold():
    assert VETO_THRESHOLD_DEFAULT == -0.5
