from fhd.image import build_image_commands

def test_image_installs_required_stack():
    joined = "\n".join(build_image_commands())
    assert "astral.sh/uv/install.sh" in joined
    assert "uv tool install fulcra-api" in joined
    assert "hermes-agent/main/scripts/install.sh" in joined
    assert "--skip-browser --skip-setup" in joined
    assert "caddy" in joined
    assert "agent-skills/skills/fulcra-onboarding" in joined
    assert "npm run build" in joined
    assert "git" in joined and "procps" in joined

def test_image_presets_openrouter():
    joined = "\n".join(build_image_commands())
    assert "model.provider openrouter" in joined
    assert "model.default anthropic/claude-sonnet-4.5" in joined
