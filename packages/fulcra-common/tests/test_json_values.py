"""JSON snapshots must reject values whose identity/content would be lost."""
import math

import pytest

from fulcra_common.json_values import snapshot_json


@pytest.mark.parametrize("value", [{1: "lost"}, {"nested": {False: 0}}, {"items": [{None: 1}]},
    {"key": "\ud800"}, {"\udfff": 1}, {"nested": [math.nan]}, {"nested": [math.inf]}, {"nested": {-1.0}},
    {"tuple": ({1: "lost"},)}, b"not JSON", object()])
def test_non_json_keys_unicode_and_values_are_rejected(value):
    with pytest.raises(ValueError, match="JSON"):
        snapshot_json(value)


def test_snapshot_preserves_falsy_nested_unicode_and_independent_mutability():
    original = {"key": [0, False, None, "Café 日本語\n", {"1": "text"}], "tuple": (1, 2)}
    captured = snapshot_json(original)
    assert captured == {"key": [0, False, None, "Café 日本語\n", {"1": "text"}], "tuple": [1, 2]}
    original["key"][-1]["1"] = "changed"
    assert captured["key"][-1]["1"] == "text"


def test_shared_non_cyclic_container_is_allowed_but_cycles_fail():
    shared = [0]
    assert snapshot_json([shared, shared]) == [[0], [0]]
    shared.append(shared)
    with pytest.raises(ValueError, match="JSON"):
        snapshot_json(shared)
