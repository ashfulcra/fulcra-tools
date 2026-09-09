"""Project selection and Keychain connection for Todoist completion sync."""
from datetime import timedelta
from functools import partial
import hmac

from fulcra_collect import config as collect_config, credentials as collect_credentials
from fulcra_collect.config_leases import task_mutation_scope
from fulcra_collect.plugin import Credential, Plugin, Setting, SetupStep
from fulcra_task_sync.engine import run_sync
from fulcra_task_sync.vault import FulcraVault

from .provider import TodoistProvider

PLUGIN_ID = 'todoist'


def selected_projects(value):
    if (not isinstance(value, list) or any(not isinstance(v, str) or not v.strip() for v in value)
            or len(value) != len(set(value))):
        raise ValueError('Choose valid Todoist projects in Collect settings.')
    return set(value)


def current_selection(token, epoch=None):
    config = collect_config.load()
    settings = config.plugin_settings.get(PLUGIN_ID, {})
    if (getattr(config, 'account_transition', False)
            or PLUGIN_ID not in config.enabled or settings.get('dry_run', True) is not False
            or (epoch is not None and config.plugin_epochs.get(PLUGIN_ID, '') != epoch)):
        return set()
    current = collect_credentials.get_secret(PLUGIN_ID, 'api_token')
    if not current or not hmac.compare_digest(current, token):
        return set()
    return selected_projects(settings.get('selected_projects', []))


def _token(ctx):
    token = ctx.credentials.get('api_token')
    if not isinstance(token, str) or not token.strip():
        raise ValueError('Connect Todoist with your API token first.')
    return token


def setting_options(ctx, key):
    if key != 'selected_projects':
        raise ValueError('Unknown Todoist setting.')
    provider = TodoistProvider(_token(ctx), dry_run=True)
    try:
        return [{'value': item.id, 'label': item.name, 'disabled': not item.writable}
                for item in provider.collections()]
    finally:
        provider.close()


def run(ctx):
    selected = selected_projects(ctx.config.get('selected_projects', []))
    if not selected:
        raise ValueError('Choose at least one Todoist project before syncing.')
    preview = ctx.config.get('dry_run', True)
    if type(preview) is not bool:
        raise ValueError('Preview only must be on or off.')
    token = _token(ctx)
    epoch = getattr(ctx, 'config_epoch', '')
    provider = TodoistProvider(token, load_state=ctx.kv_get, save_state=ctx.kv_set, dry_run=preview)
    try:
        vault = FulcraVault(ctx.fulcra_token())
        try:
            result = run_sync(provider=provider, selected_ids=selected, vault=vault,
                load_state=ctx.kv_get, save_state=ctx.kv_set, dry_run=preview,
                still_selected=partial(current_selection, token, epoch=epoch),
                selection_epoch=epoch, deadline_s=600, mutation_scope=task_mutation_scope)
        finally:
            vault.close()
    finally:
        provider.close()
    ctx.progress(stage='partial' if result.partial else 'done',
                 remaining=result.remaining, errors=len(result.errors), **result.counts)
    if result.errors:
        raise RuntimeError(f'{len(result.errors)} task sync issue(s); progress is saved. '
                           'Check the Todoist connection, selected projects, and task ownership. '
                           'Recurring tasks must be completed in Todoist.')


PLUGIN = Plugin(
    id=PLUGIN_ID, name='Todoist', kind='scheduled', collect_mode='live_polled',
    run=run, category='other', default_interval=timedelta(minutes=5),
    description='Copy selected Todoist projects to Fulcra and sync ordinary task completion '
                'in both directions. Recurring tasks must be completed in Todoist.',
    required_credentials=(Credential(key='api_token', label='Todoist API token',
        help='In Todoist on the web: Settings → Integrations → Developer → Copy API token. '
             'Paste it here. Collect stores it in your OS Keychain.'),),
    setting_options=setting_options,
    required_settings=(
        Setting(key='selected_projects', label='Projects to sync', kind='multiselect',
                help='Only checked projects sync. Removing a selection leaves existing Fulcra copies alone.'),
        Setting(key='dry_run', label='Preview only', kind='toggle', default=True, required=False,
                help='Read and check tasks without uploading or changing Todoist.'),
    ),
    setup_steps=(
        SetupStep(kind='intro', title='Bring selected Todoist projects into Fulcra', body_md=
            'Collect checks selected projects every five minutes while this Mac is running. '
            'Complete an ordinary task in Todoist and its Fulcra copy becomes resolved. '
            'Resolve its Fulcra copy and Collect completes it in Todoist. '
            'Dates, titles, descriptions, and project membership stay managed in Todoist.'),
        SetupStep(kind='external_action', title='Copy your Todoist API token',
            external_link='https://www.todoist.com/help/todoist/integrations/find-your-api-token-Jpzx9IIlB',
            body_md='Open Todoist on the web, click your avatar, then **Settings → Integrations → '
                    'Developer → Copy API token**. Return here and paste it in the next step. '
                    'You do not need a developer account or an OAuth application.'),
        SetupStep(kind='input', title='Connect Todoist', settings_keys=('api_token',)),
        SetupStep(kind='input', title='Choose projects and preview mode',
            settings_keys=('selected_projects', 'dry_run'), body_md=
            'No projects are selected for you. Keep **Preview only** on to test first. '
            'Turn it off when you want uploads and completion syncing.'),
        SetupStep(kind='done', title='Ready to sync selected projects', body_md=
            'Choose **Enable & run preview** to check without writing. With preview off, '
            '**Enable & start sync** writes to **vault/tasks/todoist/** and allows ordinary '
            'task completion in either direction. Recurring tasks must be completed in Todoist. '
            'This version checks 30 days of completion history; longer offline gaps may need review.'),
    ),
)
