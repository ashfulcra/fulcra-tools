from datetime import datetime, timezone
from fulcra_prefs.compileprefs import compile_signals
from test_schema import make_signal

NOW = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)

def test_single_signal_lands_in_global_doc():
    docs = compile_signals([make_signal()], NOW)
    entry = docs["global"]["keys"]["dining.cuisine.thai"]
    assert entry["value"] == {"liked": True}
    assert entry["n_signals"] == 1
    assert docs["global"]["compiled_at"] == "2026-06-10T12:00:00+00:00"

def test_conflict_resolves_to_highest_abs_effective_weight():
    a = make_signal(id="rec-a", strength=0.3, value={"liked": True},
                    observed_at="2026-06-09T12:00:00+00:00")
    b = make_signal(id="rec-b", strength=-0.9, value={"liked": False},
                    observed_at="2026-06-08T12:00:00+00:00")
    docs = compile_signals([a, b], NOW)
    assert docs["global"]["keys"]["dining.cuisine.thai"]["value"] == {"liked": False}
    assert docs["global"]["keys"]["dining.cuisine.thai"]["n_signals"] == 2

def test_tie_resolves_to_newer_observed_at():
    a = make_signal(id="rec-a", half_life_days=None, strength=0.5,
                    value={"v": "old"}, observed_at="2026-06-01T00:00:00+00:00")
    b = make_signal(id="rec-b", half_life_days=None, strength=0.5,
                    value={"v": "new"}, observed_at="2026-06-09T00:00:00+00:00")
    docs = compile_signals([a, b], NOW)
    assert docs["global"]["keys"]["dining.cuisine.thai"]["value"] == {"v": "new"}

def test_superseded_signals_dropped_including_chains():
    a = make_signal(id="rec-a", value={"gen": 1})
    b = make_signal(id="rec-b", value={"gen": 2}, supersedes="rec-a")
    c = make_signal(id="rec-c", value={"gen": 3}, supersedes="rec-b")
    docs = compile_signals([a, b, c], NOW)
    entry = docs["global"]["keys"]["dining.cuisine.thai"]
    assert entry["value"] == {"gen": 3}
    assert entry["n_signals"] == 1   # superseded signals are gone, not merged

def test_supersedes_temp_id_still_drops_persisted_record():
    # Spec contract: `supersedes` may reference either the local temp id or the
    # persisted Fulcra record id. Once a record is persisted, its temp id still
    # appears in metadata.source and must remain a valid alias.
    old = make_signal(id="rec-a", value={"gen": 1},
                      source_ids=("com.fulcra-prefs.sig.temp-a",))
    new = make_signal(id="rec-b", value={"gen": 2},
                      supersedes="com.fulcra-prefs.sig.temp-a")
    docs = compile_signals([old, new], NOW)
    assert docs["global"]["keys"]["dining.cuisine.thai"]["value"] == {"gen": 2}

def test_supersedes_dangling_ref_does_not_drop_replacement():
    sig = make_signal(id="rec-b", value={"gen": 2}, supersedes="missing-id")
    docs = compile_signals([sig], NOW)
    assert docs["global"]["keys"]["dining.cuisine.thai"]["value"] == {"gen": 2}

def test_supersedes_cycle_drops_all_cycle_members():
    a = make_signal(id="rec-a", value={"gen": 1}, supersedes="rec-b")
    b = make_signal(id="rec-b", value={"gen": 2}, supersedes="rec-a")
    assert compile_signals([a, b], NOW)["global"]["keys"] == {}

def test_confidence_weights_selection_so_inferred_does_not_override_explicit():
    """Feature 3 — auto-capture safety. confidence was stored but UNUSED in
    conflict resolution, so a low-confidence INFERRED signal could override a
    high-confidence EXPLICIT one of similar weight. Selection is now weighted by
    confidence: |0.6|*1.0 = 0.60 beats |0.8|*0.5 = 0.40 → the explicit pref wins,
    even though the inferred one has the larger raw strength."""
    explicit = make_signal(id="rec-explicit", strength=0.6, confidence=1.0,
                           value={"liked": True}, half_life_days=None,
                           observed_at="2026-06-09T12:00:00+00:00")
    inferred = make_signal(id="rec-inferred", strength=0.8, confidence=0.5,
                           value={"liked": False}, half_life_days=None,
                           observed_at="2026-06-09T12:00:00+00:00")
    docs = compile_signals([explicit, inferred], NOW)
    assert docs["global"]["keys"]["dining.cuisine.thai"]["value"] == {"liked": True}


def test_platform_scope_overlays_global():
    g = make_signal(id="rec-g", value={"v": "global"})
    p = make_signal(id="rec-p", scope="platform:claude-code", value={"v": "cc"})
    docs = compile_signals([g, p], NOW)
    assert docs["global"]["keys"]["dining.cuisine.thai"]["value"] == {"v": "global"}
    assert docs["platforms"]["claude-code"]["keys"]["dining.cuisine.thai"]["value"] == {"v": "cc"}

def test_consent_kind_signals_excluded_from_pref_docs():
    docs = compile_signals([make_signal(kind="consent", key="consent.disclosure.x")], NOW)
    assert docs["global"]["keys"] == {}

def test_stale_fact_carries_flag():
    f = make_signal(kind="fact", half_life_days=None,
                    observed_at="2020-01-01T00:00:00+00:00")
    docs = compile_signals([f], NOW)
    assert docs["global"]["keys"]["dining.cuisine.thai"]["stale"] is True
