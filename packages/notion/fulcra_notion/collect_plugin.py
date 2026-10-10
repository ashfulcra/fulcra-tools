"""Explicit page selection, read-only previews and account-bound uploads."""
from copy import deepcopy
from datetime import timedelta
from functools import partial
import hashlib
import hmac

from fulcra_collect import config, credentials
from fulcra_collect.config_leases import task_mutation_scope
from fulcra_collect.plugin import Credential, HealthResult, Plugin, Setting, SetupStep
from fulcra_local_sources.sync import SyncResult
from fulcra_local_sources.vault import VaultWriter

from .client import NotionClient, NotionError, normalize_page_ids
from .render import render_snapshot

PLUGIN_ID = 'notion-pages'


def _token(ctx):
    value = ctx.credentials.get('notion_token')
    if not isinstance(value, str) or not value.strip():
        raise NotionError('Connect Notion in Collect first.')
    return value


def _same(left, right):
    return isinstance(right, str) and hmac.compare_digest(left.encode(), right.encode())


def _consent(settings, epoch, notion_token, fulcra_token=None, *, fresh=False, require_enabled=True):
    current = config.load()
    saved = current.plugin_settings.get(PLUGIN_ID, {})
    if (current.account_transition or (require_enabled and PLUGIN_ID not in current.enabled)
            or current.plugin_epochs.get(PLUGIN_ID, '') != epoch
            or saved.get('page_links') != settings.get('page_links')
            or saved.get('dry_run', True) != settings.get('dry_run', True)
            or not _same(notion_token, credentials.get_secret(PLUGIN_ID, 'notion_token'))
            or (fresh and fulcra_token is not None
                and not _same(fulcra_token, credentials.get_user_secret_fresh('bearer-token')))):
        raise RuntimeError('Collect configuration changed; run the selected pages again.')


def run(ctx, *, client=None, writer=None):
    selected = normalize_page_ids(ctx.config.get('page_links'))
    preview = ctx.config.get('dry_run', True)
    if type(preview) is not bool:
        raise ValueError('Preview only must be on or off.')
    notion_token = _token(ctx)
    fulcra_token = None if preview else ctx.collect_token()
    if not preview and not fulcra_token:
        raise NotionError('Reconnect to Fulcra in Collect before uploading.')
    allowed = partial(_consent, deepcopy(ctx.config), ctx.config_epoch,
                      notion_token, fulcra_token)
    upload_allowed = partial(allowed, fresh=True)
    allowed()
    owns_client = client is None
    owns_writer = writer is None and not preview
    if owns_client:
        client = NotionClient(notion_token, still_allowed=allowed)
    if owns_writer:
        writer = VaultWriter(fulcra_token)
    uploaded = unchanged = previewed = 0
    try:
        for page_id in selected:
            allowed()
            snapshot = client.snapshot(page_id)
            allowed()
            content = render_snapshot(snapshot)
            if preview:
                previewed += 1
                continue
            with task_mutation_scope():
                upload_allowed()
                namespace = writer.namespace
                key = 'page:' + hashlib.sha256((namespace + '\0' + page_id).encode()).hexdigest()
                digest = hashlib.sha256(content.encode()).hexdigest()
                remote = '/vault/imports/notion/' + page_id + '.md'
                prior = ctx.kv_get(key, None)
                if prior == {'digest': digest, 'remote': remote}:
                    unchanged += 1
                    continue
                upload_allowed()
                writer.write_text(remote, content)
                ctx.kv_set(key, {'digest': digest, 'remote': remote})
                uploaded += 1
        ctx.progress(stage='done', uploaded=uploaded, unchanged=unchanged, previewed=previewed)
        ctx.log.info('Notion pages: uploaded=%d unchanged=%d previewed=%d',
                     uploaded, unchanged, previewed)
        return SyncResult(uploaded, unchanged, previewed)
    finally:
        if owns_client:
            client.close()
        if owns_writer:
            writer.close()


def health(ctx):
    try:
        selected = normalize_page_ids(ctx.config.get('page_links'))
        current = config.load()
        allowed = partial(_consent, deepcopy(ctx.config),
                          current.plugin_epochs.get(PLUGIN_ID, ''), _token(ctx),
                          require_enabled=False)
        allowed()
        with NotionClient(_token(ctx), still_allowed=allowed) as client:
            preview = []
            for page_id in selected:
                allowed()
                snapshot = client.snapshot(page_id)
                allowed()
                render_snapshot(snapshot)
                if len(preview) < 20:
                    preview.append({'title': snapshot.title,
                                    'detail': 'Page content is readable; unsupported items are marked.'})
        return HealthResult(True, f'{len(selected)} selected pages readable; nothing uploaded.', preview)
    except (NotionError, ValueError):
        return HealthResult(False, 'Could not read every selected page. Check the page links '
                                  'and share each page with the Notion connection.')
    except RuntimeError:
        return HealthResult(False, 'The page selection or connection changed; run the check again.')


PLUGIN = Plugin(
    id=PLUGIN_ID, name='Notion pages', kind='scheduled', collect_mode='live_polled',
    run=run, default_interval=timedelta(hours=6), requires_network=True,
    description='Copies only the Notion pages you choose into Fulcra Files. '
                'Leaves Notion unchanged; linked pages and attachments are omitted.',
    required_credentials=(Credential(key='notion_token', label='Notion connection token',
        help='Paste the token from your Notion internal connection. Collect stores it in Keychain.'),),
    required_settings=(
        Setting(key='page_links', label='Pages to copy', kind='long_text',
                help='Paste a Notion page link on each line. No pages are selected for you.'),
        Setting(key='dry_run', label='Preview only', kind='toggle', default=True, required=False,
                help='Read the selected pages without uploading anything to Fulcra.'),
    ),
    setup_steps=(
        SetupStep(kind='intro', title='Choose the Notion pages your agents can read', body_md=
            'Collect copies selected pages into Fulcra every six hours while this Mac is running. '
            'It reads ordinary page blocks, marks unsupported content, and leaves the originals alone. '
            'It does not search your workspace or follow child pages.'),
        SetupStep(kind='external_action', title='Create a read-only Notion connection',
            external_link='https://developers.notion.com/guides/get-started/authorization', body_md=
            'A Notion workspace owner must create an internal connection in the Notion Developer '
            'portal. Give it **Read content** access only. Copy its API token from **Configuration**. '
            'For each page you want to copy, open its **••• → Add connections** menu and choose '
            'this connection. If you are not a workspace owner, ask the owner to set this up.'),
        SetupStep(kind='input', title='Connect Notion and choose pages',
            settings_keys=('notion_token', 'page_links', 'dry_run'), body_md=
            'Paste the connection token, then a page link on each line. Keep **Preview only** on '
            'for the first run. Links and selections stay in your local Collect configuration; '
            'the token goes into Keychain.'),
        SetupStep(kind='test_connection', title='Check the selected pages', body_md=
            'Collect reads the chosen pages and shows their titles. This check uploads nothing.'),
        SetupStep(kind='done', title='Ready to collect selected pages', body_md=
            'Choose **Enable & run preview** to test without uploading. When you want copies in '
            'Fulcra, turn **Preview only** off and run again. Existing Fulcra copies remain when '
            'you remove a selection. Reconnect in Collect if its Fulcra sign-in expires.'),
    ),
    health_check=health,
)
