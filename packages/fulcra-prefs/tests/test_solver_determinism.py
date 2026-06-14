"""solve() must be independent of the caller's opt["keys"] ordering.

Float addition is not associative, so summing weights in the order the caller
happened to list keys makes both the score and the trace (which the spec calls
the product, not a debug aid) depend on input ordering. The module docstring
promises a reproducible ranking; that has to hold regardless of key order.
"""
from fulcra_prefs.solver import solve


def _doc(weights):
    return {"alice": {"v": 1, "compiled_at": "x", "keys": {
        k: {"value": True, "weight": w, "confidence": 1.0,
            "observed_at": "2026-06-01T00:00:00+00:00",
            "n_signals": 1, "sources": ["t"]}
        for k, w in weights.items()}}}


def test_score_independent_of_key_order():
    docs = _doc({"a": 1e16, "b": 1.0, "c": -1e16})
    s1 = solve([{"id": "o", "keys": ["a", "b", "c"]}], docs)["ranked"][0]["score"]
    s2 = solve([{"id": "o", "keys": ["a", "c", "b"]}], docs)["ranked"][0]["score"]
    assert s1 == s2


def test_trace_independent_of_key_order():
    docs = _doc({"a": 0.2, "b": 0.3, "c": 0.5})
    t1 = solve([{"id": "o", "keys": ["a", "b", "c"]}], docs)["trace"]
    t2 = solve([{"id": "o", "keys": ["c", "b", "a"]}], docs)["trace"]
    assert t1 == t2
