"""Selected Reminders lists with explicit access and completion consent."""
from datetime import timedelta
from functools import partial

from fulcra_collect import config as collect_config
from fulcra_collect.plugin import Permission, Plugin, Setting, SetupStep
from fulcra_task_sync.engine import run_sync
from fulcra_task_sync.vault import FulcraVault

from .provider import AppleRemindersProvider

PLUGIN_ID = 'apple-reminders'


def selected_lists(value):
    if (not isinstance(value, list) or any(not isinstance(v, str) or not v.strip() for v in value)
            or len(value) != len(set(value))):
        raise ValueError('Choose valid Reminders lists in Collect settings.')
    return set(value)


def current_selection(epoch=None):
    """Re-read user intent before writes; disabling or preview cancels mutations."""
    config = collect_config.load()
    settings = config.plugin_settings.get(PLUGIN_ID, {})
    if (PLUGIN_ID not in config.enabled or settings.get('dry_run', True) is not False
            or (epoch is not None and config.plugin_epochs.get(PLUGIN_ID, '') != epoch)):
        return set()
    return selected_lists(settings.get('selected_lists', []))


def permission_check(ctx):
    return AppleRemindersProvider().permission_check(request=False)


def permission_request(ctx):
    return AppleRemindersProvider().permission_check(request=True)


def setting_options(ctx, key):
    if key != 'selected_lists':
        raise ValueError('Unknown Reminders setting.')
    return [{'value': item.id, 'label': item.name, 'disabled': not item.writable}
            for item in AppleRemindersProvider().collections()]


def run(ctx):
    selected = selected_lists(ctx.config.get('selected_lists', []))
    if not selected:
        raise ValueError('Choose at least one Reminders list before syncing.')
    preview = ctx.config.get('dry_run', True)
    if type(preview) is not bool:
        raise ValueError('Preview only must be on or off.')
    epoch = getattr(ctx, 'config_epoch', '')
    provider = AppleRemindersProvider()
    vault = FulcraVault(ctx.fulcra_token())
    try:
        result = run_sync(provider=provider, selected_ids=selected, vault=vault,
                          load_state=ctx.kv_get, save_state=ctx.kv_set,
                          dry_run=preview, still_selected=partial(current_selection, epoch=epoch),
                          selection_epoch=epoch,
                          deadline_s=600)
    finally:
        vault.close()
    ctx.progress(stage='partial' if result.partial else 'done',
                 remaining=result.remaining, errors=len(result.errors), **result.counts)
    if result.errors:
        raise RuntimeError(f'{len(result.errors)} task sync issue(s); progress is saved. '
                           'Check access, selected lists, and task ownership. '
                           'Recurring tasks cannot be completed from Fulcra in this version.')


PLUGIN = Plugin(
    id=PLUGIN_ID, name='Apple Reminders', kind='scheduled', collect_mode='live_polled',
    run=run, category='other', default_interval=timedelta(minutes=5),
    description='Copy selected Reminders lists to Fulcra and sync completion in both '
                'directions. Recurring tasks are imported but must be completed in Reminders.',
    required_permissions=(Permission(id='reminders', explanation=
        'Reads selected lists and marks ordinary reminders completed when you resolve their Fulcra copies.'),),
    permission_check=permission_check, permission_request=permission_request,
    setting_options=setting_options,
    required_settings=(
        Setting(key='selected_lists', label='Lists to sync', kind='multiselect',
                help='Only checked lists sync. Removing a selection stops future imports and completion writes.'),
        Setting(key='dry_run', label='Preview only', kind='toggle', default=True, required=False,
                help='Read and check tasks without changing Reminders or uploading to Fulcra.'),
    ),
    setup_steps=(
        SetupStep(kind='intro', title='Bring selected reminders into Fulcra', body_md=
            'Choose the lists you want to share with your Fulcra account. Collect checks every '
            'five minutes while this Mac is running. Completing an ordinary reminder in Apple '
            'updates its Fulcra copy. Resolving its Fulcra copy completes it in Apple. '
            'Titles, dates, and list membership stay managed in Reminders.'),
        SetupStep(kind='permission_request', title='Allow access to Reminders', body_md=
            'Click **Allow access**, then approve the macOS Reminders prompt. '
            'macOS grants access to Reminders; your list selection in the next step determines '
            'what Collect syncs. Open Reminders first so iCloud has time to download your lists.'),
        SetupStep(kind='input', title='Choose lists and preview mode',
                  settings_keys=('selected_lists', 'dry_run'), body_md=
            'No lists are selected for you. Keep **Preview only** on to check the setup first. '
            'Turn it off when you want uploads and two-way completion. Read-only lists cannot be selected.'),
        SetupStep(kind='done', title='Ready to sync your selected lists', body_md=
            'Choose **Enable & run preview** to check without writing. With preview off, '
            '**Enable & start sync** uploads tasks to **vault/tasks/apple-reminders/** and '
            'allows completion in either direction. Recurring reminders must be completed in '
            'Apple; Collect imports their current state but does not complete an occurrence for you. '
            'Removing a list from selection leaves existing copies in Fulcra.'),
    ),
)
