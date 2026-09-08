from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from fulcra_apple_reminders import collect_plugin as plugin


@pytest.fixture(autouse=True)
def isolated_home(monkeypatch, tmp_path):
    monkeypatch.setenv("FULCRA_COLLECT_HOME", str(tmp_path / "collect"))


def context(settings):
    return SimpleNamespace(
        config=settings,
        kv_get=Mock(),
        kv_set=Mock(),
        fulcra_token=Mock(return_value="synthetic-token"),
        progress=Mock(),
        log=Mock(),
    )


def test_empty_selection_does_not_construct_provider_or_vault(monkeypatch):
    factory = Mock()
    monkeypatch.setattr(plugin, "AppleRemindersProvider", factory)
    with pytest.raises(ValueError, match="list"):
        plugin.run(context({"selected_lists": []}))
    factory.assert_not_called()


@pytest.mark.parametrize("value", ["list-one", [""], ["one", "one"], [1], None])
def test_invalid_selection_is_rejected(value):
    with pytest.raises(ValueError):
        plugin.selected_lists(value)


def test_read_only_permission_probe_never_requests(monkeypatch):
    provider = Mock()
    monkeypatch.setattr(plugin, "AppleRemindersProvider", lambda: provider)
    plugin.permission_check(context({}))
    provider.permission_check.assert_called_once_with(request=False)


def test_native_permission_requires_explicit_callback(monkeypatch):
    provider = Mock()
    monkeypatch.setattr(plugin, "AppleRemindersProvider", lambda: provider)
    plugin.permission_request(context({}))
    provider.permission_check.assert_called_once_with(request=True)


def test_options_discovery_does_not_run_sync(monkeypatch):
    provider = Mock()
    provider.collections.return_value = [
        SimpleNamespace(id="one", name="Example list", writable=True),
        SimpleNamespace(id="two", name="Read only", writable=False),
    ]
    monkeypatch.setattr(plugin, "AppleRemindersProvider", lambda: provider)
    sync = Mock()
    monkeypatch.setattr(plugin, "run_sync", sync)
    assert plugin.setting_options(context({}), "selected_lists") == [
        {"value": "one", "label": "Example list", "disabled": False},
        {"value": "two", "label": "Read only", "disabled": True},
    ]
    sync.assert_not_called()


def test_live_disable_or_preview_cancels_writes(monkeypatch):
    cfg = SimpleNamespace(
        enabled={"apple-reminders"},
        plugin_settings={
            "apple-reminders": {"selected_lists": ["one"], "dry_run": False}
        },
    )
    monkeypatch.setattr(plugin.collect_config, "load", lambda: cfg)
    assert plugin.current_selection() == {"one"}
    cfg.plugin_settings["apple-reminders"]["dry_run"] = True
    assert plugin.current_selection() == set()
    cfg.plugin_settings["apple-reminders"]["dry_run"] = False
    cfg.enabled.clear()
    assert plugin.current_selection() == set()


def test_run_forwards_preview_selection_and_state(monkeypatch):
    provider = Mock()
    vault = Mock()
    monkeypatch.setattr(plugin, "AppleRemindersProvider", lambda: provider)
    monkeypatch.setattr(plugin, "FulcraVault", lambda token: vault)
    result = SimpleNamespace(
        counts={"imported": 2}, errors=[], remaining=0, partial=False
    )
    sync = Mock(return_value=result)
    monkeypatch.setattr(plugin, "run_sync", sync)
    ctx = context({"selected_lists": ["one"], "dry_run": True})
    plugin.run(ctx)
    args = sync.call_args
    assert args.kwargs["selected_ids"] == {"one"}
    assert args.kwargs["dry_run"] is True
    assert args.kwargs["load_state"] == ctx.kv_get
    assert args.kwargs["save_state"] == ctx.kv_set
    assert args.kwargs["still_selected"].func is plugin.current_selection
    vault.close.assert_called_once()


def test_run_failure_is_reported_without_private_error(monkeypatch):
    monkeypatch.setattr(plugin, "AppleRemindersProvider", Mock())
    vault = Mock()
    monkeypatch.setattr(plugin, "FulcraVault", lambda token: vault)
    monkeypatch.setattr(
        plugin,
        "run_sync",
        Mock(
            return_value=SimpleNamespace(
                counts={}, errors=["private task title"], remaining=1, partial=False
            )
        ),
    )
    ctx = context({"selected_lists": ["one"], "dry_run": False})
    with pytest.raises(RuntimeError, match="1 task") as error:
        plugin.run(ctx)
    assert "private" not in str(error.value)
    vault.close.assert_called_once()


def test_setup_has_empty_selection_and_explicit_permission():
    settings = {s.key: s for s in plugin.PLUGIN.required_settings}
    assert settings["selected_lists"].default == []
    assert settings["dry_run"].default is True
    assert plugin.PLUGIN.permission_request is plugin.permission_request
    assert plugin.PLUGIN.setup_steps[-1].kind == "done"
