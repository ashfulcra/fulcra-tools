from unittest.mock import Mock

import pytest
from fulcra_collect import config, credentials
from fulcra_notion import collect_plugin
from fulcra_notion.client import PageSnapshot

PAGE = '00000000-0000-4000-8000-000000000001'
TOKEN = 'synthetic-notion-token'


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv('FULCRA_COLLECT_HOME', str(tmp_path / 'config'))
    monkeypatch.setattr(credentials, 'get_secret', lambda plugin, key: TOKEN)
    monkeypatch.setattr(credentials, 'get_user_secret_fresh', lambda key: 'synthetic-fulcra-token')


class Context:
    plugin_id = 'notion-pages'
    def __init__(self, preview=False):
        self.config = {'page_links': PAGE, 'dry_run': preview}
        self.credentials = {'notion_token': TOKEN}
        self.log = Mock()
        self.state = {}
        saved = config.load()
        saved.enable(self.plugin_id)
        saved.update_plugin_settings(self.plugin_id, self.config)
        config.save(saved)
        self.config_epoch = saved.plugin_epochs[self.plugin_id]
    def collect_token(self):
        return 'synthetic-fulcra-token'
    def kv_get(self, key, default=None):
        return self.state.get(key, default)
    def kv_set(self, key, value):
        self.state[key] = value
    def progress(self, **values):
        pass


class Reader:
    def __init__(self, hook=lambda: None):
        self.hook = hook
        self.reads = []
    def snapshot(self, page_id):
        self.reads.append(page_id)
        self.hook()
        return PageSnapshot(page_id, 'Synthetic page', ())


class Writer:
    namespace = 'synthetic-account-a'
    def __init__(self, fail=False):
        self.writes = []
        self.fail = fail
    def write_text(self, path, text):
        if self.fail:
            raise RuntimeError('synthetic upload failure')
        self.writes.append((path, text))


def test_plugin_is_scheduled_read_only_with_private_credentials_and_preview_default():
    plugin = collect_plugin.PLUGIN
    assert plugin.id == 'notion-pages' and plugin.collect_mode == 'live_polled'
    settings = {s.key: s for s in plugin.required_settings}
    assert settings['dry_run'].default is True
    assert settings['page_links'].kind == 'long_text'
    assert plugin.required_credentials[0].key == 'notion_token'
    assert plugin.health_check and any(step.kind == 'test_connection' for step in plugin.setup_steps)


def test_preview_has_no_fulcra_lookup_writes_or_state(monkeypatch):
    ctx = Context(preview=True)
    ctx.collect_token = lambda: pytest.fail('preview must not need a Fulcra token')
    monkeypatch.setattr(collect_plugin, 'VaultWriter', lambda *a: pytest.fail('preview must not upload'))
    reader = Reader()
    result = collect_plugin.run(ctx, client=reader)
    assert result.previewed == 1 and reader.reads == [PAGE] and not ctx.state


def test_repeat_import_is_idempotent_and_destination_account_change_copies_again():
    ctx = Context()
    writer = Writer()
    assert collect_plugin.run(ctx, client=Reader(), writer=writer).uploaded == 1
    assert collect_plugin.run(ctx, client=Reader(), writer=writer).unchanged == 1
    writer.namespace = 'synthetic-account-b'
    assert collect_plugin.run(ctx, client=Reader(), writer=writer).uploaded == 1
    assert len(writer.writes) == 2
    assert writer.writes[0][0] == '/vault/imports/notion/' + PAGE + '.md'


def test_failed_upload_does_not_advance_hash():
    ctx = Context()
    with pytest.raises(RuntimeError, match='upload failure'):
        collect_plugin.run(ctx, client=Reader(), writer=Writer(fail=True))
    assert not ctx.state


@pytest.mark.parametrize('change', ['disable', 'selection', 'preview', 'epoch',
                                     'notion-token', 'fulcra-token', 'transition'])
def test_live_selection_or_account_changes_stop_pending_upload(change, monkeypatch):
    ctx = Context()
    writer = Writer()
    def change_settings():
        saved = config.load()
        if change == 'disable':
            saved.disable(ctx.plugin_id)
        elif change == 'selection':
            saved.update_plugin_settings(ctx.plugin_id, {'page_links': ''})
        elif change == 'preview':
            saved.update_plugin_settings(ctx.plugin_id, {'dry_run': True})
        elif change == 'epoch':
            config.invalidate_plugin_work(ctx.plugin_id)
            return
        elif change == 'transition':
            try:
                with config.fulcra_account_transition():
                    raise RuntimeError("synthetic failed transition")
            except RuntimeError:
                pass
            return
        elif change == 'notion-token':
            monkeypatch.setattr(credentials, 'get_secret', lambda *a: 'replacement-token')
        elif change == 'fulcra-token':
            monkeypatch.setattr(credentials, 'get_user_secret_fresh', lambda *a: 'replacement-token')
        config.save(saved)
    with pytest.raises(RuntimeError, match='configuration changed'):
        collect_plugin.run(ctx, client=Reader(change_settings), writer=writer)
    assert not writer.writes and not ctx.state


def test_bad_preview_and_empty_selection_fail_before_reads():
    for key, value in [('dry_run', 'false'), ('page_links', '')]:
        ctx = Context()
        ctx.config[key] = value
        reader = Reader()
        with pytest.raises((ValueError, RuntimeError)):
            collect_plugin.run(ctx, client=reader, writer=Writer())
        assert not reader.reads


@pytest.mark.parametrize('change', ['selection', 'credential', 'epoch'])
def test_health_probe_stops_after_live_configuration_changes(change, monkeypatch):
    ctx = Context(preview=True)
    ctx.config_epoch = ''  # Current setup route does not provide a worker epoch.
    class Probe:
        def __init__(self, guard):
            self.guard = guard
        def snapshot(self, page_id):
            saved = config.load()
            if change == 'selection':
                saved.update_plugin_settings(ctx.plugin_id, {'page_links': ''})
                config.save(saved)
            elif change == 'credential':
                monkeypatch.setattr(credentials, 'get_secret', lambda *a: 'replacement-token')
            else:
                config.invalidate_plugin_work(ctx.plugin_id)
            self.guard()
            return PageSnapshot(page_id, 'Synthetic page', ())
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
    monkeypatch.setattr(collect_plugin, 'NotionClient',
                        lambda token, **kwargs: Probe(kwargs.get('still_allowed', lambda: None)))
    result = collect_plugin.health(ctx)
    assert result.ok is False and not result.preview


def test_health_probe_is_allowed_before_enablement(monkeypatch):
    ctx = Context(preview=True)
    saved = config.load()
    saved.disable(ctx.plugin_id)
    config.save(saved)
    ctx.config_epoch = ''
    class Probe:
        def __init__(self, guard):
            self.guard = guard
        def snapshot(self, page_id):
            self.guard()
            return PageSnapshot(page_id, 'Synthetic page', ())
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
    monkeypatch.setattr(collect_plugin, 'NotionClient',
                        lambda token, **kwargs: Probe(kwargs.get('still_allowed', lambda: None)))
    assert collect_plugin.health(ctx).ok is True
