import json
from fulcra_coord import routing_ops as ro

def test_no_config_yields_empty_seeds(monkeypatch):
    monkeypatch.delenv("FULCRA_COORD_REVIEW_SEED", raising=False)
    monkeypatch.setattr(ro, "_review_routing_config_path", lambda: None)
    assert ro._review_seeds("anyone:h:r") == []

def test_env_seed_ordered(monkeypatch):
    monkeypatch.setenv("FULCRA_COORD_REVIEW_SEED", "a:h:r, b:h:r")
    monkeypatch.setattr(ro, "_review_routing_config_path", lambda: None)
    assert ro._review_seeds("x:y:z") == ["a:h:r", "b:h:r"]

def test_file_author_override_beats_top_level(tmp_path, monkeypatch):
    cfg = tmp_path / "review-routing.json"
    cfg.write_text(json.dumps({
        "seed": ["default:h:r"],
        "author_overrides": [{"author_prefix": "claude-code:ArcBot:", "seed": ["arc:rev:bot"]}],
    }))
    monkeypatch.delenv("FULCRA_COORD_REVIEW_SEED", raising=False)
    monkeypatch.setattr(ro, "_review_routing_config_path", lambda: str(cfg))
    assert ro._review_seeds("claude-code:ArcBot:x") == ["arc:rev:bot"]
    assert ro._review_seeds("codex:h:r") == ["default:h:r"]

def test_malformed_config_is_safe(tmp_path, monkeypatch):
    cfg = tmp_path / "review-routing.json"
    cfg.write_text("{ not json")
    monkeypatch.delenv("FULCRA_COORD_REVIEW_SEED", raising=False)
    monkeypatch.setattr(ro, "_review_routing_config_path", lambda: str(cfg))
    assert ro._review_seeds("x:y:z") == []

def test_malformed_nested_config_is_safe(tmp_path, monkeypatch):
    cfg = tmp_path / "review-routing.json"
    cfg.write_text(json.dumps({
        "seed": "not-a-list",
        "author_overrides": [
            "not-a-dict",
            {"author_prefix": ["not", "a", "string"], "seed": ["bad:h:r"]},
            {"author_prefix": "x:y:", "seed": "not-a-list"},
        ],
    }))
    monkeypatch.delenv("FULCRA_COORD_REVIEW_SEED", raising=False)
    monkeypatch.setattr(ro, "_review_routing_config_path", lambda: str(cfg))
    assert ro._review_seeds("x:y:z") == []

def test_malformed_author_overrides_fall_back_to_top_level_seed(tmp_path, monkeypatch):
    cfg = tmp_path / "review-routing.json"
    cfg.write_text(json.dumps({
        "seed": ["default:h:r"],
        "author_overrides": {"author_prefix": "x:y:", "seed": ["bad:h:r"]},
    }))
    monkeypatch.delenv("FULCRA_COORD_REVIEW_SEED", raising=False)
    monkeypatch.setattr(ro, "_review_routing_config_path", lambda: str(cfg))
    assert ro._review_seeds("x:y:z") == ["default:h:r"]

def test_env_takes_precedence_over_file(tmp_path, monkeypatch):
    cfg = tmp_path / "review-routing.json"
    cfg.write_text(json.dumps({"seed": ["file:h:r"]}))
    monkeypatch.setenv("FULCRA_COORD_REVIEW_SEED", "env:h:r")
    monkeypatch.setattr(ro, "_review_routing_config_path", lambda: str(cfg))
    assert ro._review_seeds("x:y:z") == ["env:h:r"]
