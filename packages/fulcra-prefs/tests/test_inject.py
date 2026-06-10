from fulcra_prefs.inject import render_block

DOC = {"v": 1, "compiled_at": "2026-06-10T12:00:00+00:00",
       "keys": {"dining.cuisine.thai": {"value": True, "weight": 0.8,
                                        "confidence": 0.9,
                                        "observed_at": "2026-06-01T00:00:00+00:00",
                                        "n_signals": 3, "sources": ["claude-code"]},
                "schedule.no-meetings-before": {"value": "10:00", "weight": 1.0,
                                                "confidence": 1.0,
                                                "observed_at": "2026-05-01T00:00:00+00:00",
                                                "n_signals": 1, "sources": ["codex"],
                                                "stale": True}}}

def test_render_contains_keys_weights_and_header():
    out = render_block(DOC, platform="claude-code")
    assert "# User preferences (fulcra-prefs)" in out
    assert "dining.cuisine.thai" in out and "+0.80" in out
    assert "compiled 2026-06-10" in out

def test_stale_entries_marked():
    out = render_block(DOC, platform="claude-code")
    assert "schedule.no-meetings-before" in out and "(stale)" in out

def test_empty_doc_renders_nothing():
    assert render_block({"v": 1, "compiled_at": "x", "keys": {}},
                        platform="claude-code") == ""

def test_none_doc_renders_nothing():
    assert render_block(None, platform="claude-code") == ""
