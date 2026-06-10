"""The byte-identical contract from SPEC.md. If these tests ever flake,
determinism is broken — treat as P0, not as test noise."""
import random
from datetime import datetime, timezone
from fulcra_prefs.compileprefs import compile_signals
from fulcra_prefs.schema import canonical_json
from test_schema import make_signal

NOW = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)

def _fixture_signals():
    sigs = []
    for i in range(40):
        sigs.append(make_signal(
            id=f"rec-{i:03d}",
            key=f"k.{i % 7}",
            scope="global" if i % 3 else "platform:claude-code",
            strength=((i % 11) - 5) / 5.0,
            observed_at=f"2026-05-{(i % 28) + 1:02d}T08:00:00+00:00",
            half_life_days=None if i % 5 == 0 else 60.0,
            supersedes=f"rec-{i - 1:03d}" if i % 13 == 0 and i else None,
            value={"i": i},
        ))
    return sigs

def test_same_inputs_byte_identical_output():
    a = canonical_json(compile_signals(_fixture_signals(), NOW))
    b = canonical_json(compile_signals(_fixture_signals(), NOW))
    assert a == b

def test_input_order_does_not_change_output():
    base = canonical_json(compile_signals(_fixture_signals(), NOW))
    for seed in (1, 7, 42):
        shuffled = _fixture_signals()
        random.Random(seed).shuffle(shuffled)
        assert canonical_json(compile_signals(shuffled, NOW)) == base

def test_output_contains_no_unnormalized_floats():
    out = canonical_json(compile_signals(_fixture_signals(), NOW))
    for token in out.replace("{", ",").replace("}", ",").split(","):
        if "." in token and token.split(":")[-1].replace("-", "").replace(".", "").isdigit():
            frac = token.split(".")[-1].rstrip("}")
            assert len(frac) <= 6
