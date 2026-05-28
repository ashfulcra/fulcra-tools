from fhd.snapshot_params import build_spawn_kwargs


def test_build_spawn_kwargs_shape():
    kw = build_spawn_kwargs(
        snapshot="fhd-hermes-demo",
        openrouter_model="anthropic/claude-sonnet-4.5",
        label="alice",
    )
    assert kw["snapshot"] == "fhd-hermes-demo"
    assert kw["auto_stop_interval"] == 30
    assert kw["public"] is False
    assert kw["env_vars"]["OPENROUTER_MODEL"] == "anthropic/claude-sonnet-4.5"
    # the secret key must NOT be in sandbox env_vars (injected via ~/.hermes/.env)
    assert "OPENROUTER_API_KEY" not in kw["env_vars"]
    assert kw["labels"] == {"fhd": "guest", "guest": "alice"}
