from fhd.snapshot_params import build_spawn_kwargs


def test_build_spawn_kwargs_shape():
    kw = build_spawn_kwargs(
        snapshot="fhd-hermes-demo",
        openrouter_model="anthropic/claude-sonnet-4.5",
        label="alice",
        skill_branch="my-branch",
    )
    assert kw["snapshot"] == "fhd-hermes-demo"
    assert kw["auto_stop_interval"] == 240
    assert kw["public"] is False
    assert kw["env_vars"]["OPENROUTER_MODEL"] == "anthropic/claude-sonnet-4.5"
    assert kw["env_vars"]["FULCRA_SKILL_BRANCH"] == "my-branch"
    # the secret key must NOT be in sandbox env_vars (injected via ~/.hermes/.env)
    assert "OPENROUTER_API_KEY" not in kw["env_vars"]
    assert kw["labels"] == {"fhd": "guest", "guest": "alice"}


def test_build_spawn_kwargs_defaults_to_main_branch():
    kw = build_spawn_kwargs(
        snapshot="s", openrouter_model="m", label="l",
    )
    assert kw["env_vars"]["FULCRA_SKILL_BRANCH"] == "main"
