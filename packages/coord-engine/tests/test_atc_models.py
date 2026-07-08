"""ATC v2 — packaged default model map + overlay merge.

The default map is packaged data (a coord release artifact): invalid content
there is a packaging bug, so it raises. Overlay content comes from the
operator's accounts.json and must never crash the fold — bad entries are
skipped or sanitized and reported back as strings.
"""
import json
import pytest

from coord_engine.atc import TAXONOMY, load_default_models, merge_models


def test_taxonomy_is_frozen_set():
    assert isinstance(TAXONOMY, frozenset)
    assert TAXONOMY == frozenset({
        "code", "architecture", "writing", "long-context",
        "vision", "fast", "tool-use",
    })


def test_packaged_map_loads_and_validates():
    m = load_default_models()
    assert isinstance(m["models"], dict) and m["models"]
    for mid, entry in m["models"].items():
        assert set(entry["tags"]) <= TAXONOMY, mid
        assert isinstance(entry["cost_rank"], int) and 1 <= entry["cost_rank"] <= 9, mid
        assert isinstance(entry["harnesses"], list), mid
        assert all(isinstance(h, str) for h in entry["harnesses"]), mid


def test_map_version_string_present():
    m = load_default_models()
    assert isinstance(m["map_version"], str) and m["map_version"]


def test_unknown_top_level_keys_ignored():
    m = load_default_models()
    assert set(m.keys()) == {"map_version", "models"}
    assert "_comment" not in m
    assert "_watch_items" not in m
    assert "claude-fable-5" in m["models"]


def test_overlay_override_wins_per_id():
    defaults = load_default_models()
    overlay = {"claude-fable-5": {
        "tags": ["code"], "cost_rank": 4, "harnesses": ["claude-code"]}}
    merged, reports = merge_models(defaults, overlay)
    assert merged["models"]["claude-fable-5"]["cost_rank"] == 4
    assert merged["models"]["claude-fable-5"]["tags"] == ["code"]
    assert reports == []
    # defaults untouched (no mutation of the input map)
    assert defaults["models"]["claude-fable-5"]["cost_rank"] == 1


def test_overlay_adds_new_id():
    defaults = load_default_models()
    n = len(defaults["models"])
    overlay = {"my-local-model": {
        "tags": ["code", "fast"], "cost_rank": 9, "harnesses": ["ollama"]}}
    merged, reports = merge_models(defaults, overlay)
    assert "my-local-model" in merged["models"]
    assert merged["models"]["my-local-model"]["cost_rank"] == 9
    assert len(merged["models"]) == n + 1
    assert reports == []


def test_overlay_unknown_tag_dropped_and_reported():
    defaults = load_default_models()
    overlay = {"weird": {
        "tags": ["code", "telepathy"], "cost_rank": 5, "harnesses": ["x"]}}
    merged, reports = merge_models(defaults, overlay)
    assert merged["models"]["weird"]["tags"] == ["code"]
    assert reports == ["model weird: unknown tag 'telepathy' dropped"]


def test_overlay_malformed_entries_skipped_and_reported():
    defaults = load_default_models()
    overlay = {
        "not-a-dict": "just a string",
        "no-tags": {"cost_rank": 5, "harnesses": ["x"]},
    }
    merged, reports = merge_models(defaults, overlay)
    assert "not-a-dict" not in merged["models"]
    assert "no-tags" not in merged["models"]
    assert len(reports) == 2
    assert any("not-a-dict" in r for r in reports)
    assert any("no-tags" in r for r in reports)


def test_overlay_none_returns_defaults_unchanged():
    defaults = load_default_models()
    merged, reports = merge_models(defaults, None)
    assert merged["models"] == defaults["models"]
    assert merged["map_version"] == defaults["map_version"]
    assert reports == []


def test_broken_default_map_bad_tag_raises(monkeypatch):
    import coord_engine.atc as atc
    broken = json.dumps({"map_version": "x", "models": {
        "bad": {"tags": ["code", "not-a-real-tag"],
                "cost_rank": 5, "harnesses": ["x"]}}})
    monkeypatch.setattr(atc, "_read_default_models_text", lambda: broken)
    with pytest.raises(ValueError):
        atc.load_default_models()


def test_broken_default_map_bad_cost_rank_raises(monkeypatch):
    import coord_engine.atc as atc
    broken = json.dumps({"map_version": "x", "models": {
        "bad": {"tags": ["code"], "cost_rank": 42, "harnesses": ["x"]}}})
    monkeypatch.setattr(atc, "_read_default_models_text", lambda: broken)
    with pytest.raises(ValueError):
        atc.load_default_models()


def test_broken_default_map_bad_harnesses_raises(monkeypatch):
    import coord_engine.atc as atc
    broken = json.dumps({"map_version": "x", "models": {
        "bad": {"tags": ["code"], "cost_rank": 5, "harnesses": "claude-code"}}})
    monkeypatch.setattr(atc, "_read_default_models_text", lambda: broken)
    with pytest.raises(ValueError):
        atc.load_default_models()


def test_broken_default_map_missing_version_raises(monkeypatch):
    import coord_engine.atc as atc
    broken = json.dumps({"models": {}})
    monkeypatch.setattr(atc, "_read_default_models_text", lambda: broken)
    with pytest.raises(ValueError):
        atc.load_default_models()
