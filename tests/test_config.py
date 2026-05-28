import pytest
from fhd.config import load_settings

def test_load_settings_reads_required_env(monkeypatch):
    monkeypatch.setenv("DAYTONA_API_KEY", "dtn_x")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-y")
    monkeypatch.setenv("OPENROUTER_MODEL", "anthropic/claude-sonnet-4.5")
    s = load_settings(use_dotenv=False)
    assert s.daytona_api_key == "dtn_x"
    assert s.openrouter_api_key == "sk-or-y"
    assert s.openrouter_model == "anthropic/claude-sonnet-4.5"
    assert s.daytona_api_url == "https://app.daytona.io/api"
    assert s.daytona_target == "us"

def test_load_settings_missing_required_raises(monkeypatch):
    monkeypatch.delenv("DAYTONA_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(ValueError) as e:
        load_settings(use_dotenv=False)
    assert "DAYTONA_API_KEY" in str(e.value)

def test_openrouter_model_defaults(monkeypatch):
    monkeypatch.setenv("DAYTONA_API_KEY", "dtn_x")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-y")
    monkeypatch.delenv("OPENROUTER_MODEL", raising=False)
    s = load_settings(use_dotenv=False)
    assert s.openrouter_model == "anthropic/claude-sonnet-4.5"
