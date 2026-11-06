"""Account/epoch authority for interactive quick-record actions.

The versioned daemon backend uses this authority. Default native/web clients
and unversioned commands remain on legacy paths pending their atomic cutover.
Generic setup sessions retain the enabled-source ingestion gate.
"""
from __future__ import annotations

import logging
from uuid import UUID

from fulcra_api.core import FulcraAPI
from fulcra_api.credentials import FulcraCredentials
from fulcra_common.v1 import _canonical_uuid
from fulcra_common.v1 import V1DataClient

from . import config
from .config_leases import settings_update_scope, task_mutation_scope
from .plugin import RunContext
from .state import PluginState
from .v1_session import CollectRecordSession

_PLUGIN = "quick-record"


def _context(cfg):
    return RunContext(plugin_id=_PLUGIN, config=cfg.plugin_settings.get(_PLUGIN, {}),
        config_epoch=cfg.plugin_epochs.get(_PLUGIN, ""), credentials={},
        state=PluginState(plugin_id=_PLUGIN), log=logging.getLogger("fulcra_collect.quick_record"),
        _emit=lambda _: None)


def _captured_context(cfg):
    if cfg.account_transition:
        raise RuntimeError("Collect account changed; reconnect before recording")
    ctx = _context(cfg)
    token = ctx.collect_token()
    if not token:
        raise ValueError("Reconnect to Fulcra in Collect before recording")
    try:
        account = str(UUID(FulcraAPI(credentials=FulcraCredentials(access_token=token)).get_fulcra_userid()))
    except Exception:
        raise ValueError("Cannot establish Collect's authenticated account") from None
    return ctx, account


def quick_record_context() -> tuple[RunContext, str]:
    """Capture Collect's principal and register an epoch without enabling work.

    Registering this internal actor in plugin_epochs makes every interactive
    account transition revoke its context, including a reconnect to the same
    account. The caller returns this account/epoch in its UI handshake; writes
    must send both back. This does not resolve destinations or write records;
    Collect credentials may renew only within the established account.
    """
    # Once registered, ordinary reads/captures need only the shared account /
    # settings lease, so an active source's shared mutation lease cannot block
    # the UI behind an unnecessary settings-exclusive acquisition.
    with task_mutation_scope():
        cfg = config.load()
        ctx, account = _captured_context(cfg)
        if ctx.config_epoch:
            return ctx, account
    # Upgrade only for the initial registration, then reload/re-prove identity
    # after dropping the shared lease. Never upgrade a held file lock in place.
    with settings_update_scope():
        cfg = config.load()
        ctx, account = _captured_context(cfg)
        if not ctx.config_epoch:
            cfg.rotate_plugin_epoch(_PLUGIN)
            config._save_locked(cfg, config._config_path())
            ctx = _context(cfg)
        return ctx, account


class QuickRecordSession(CollectRecordSession):
    """Explicit interactive authority, bound to a prior account/epoch handshake."""

    def __init__(self, ctx, *, expected_account: str, expected_epoch: str, transport=None):
        self.expected_account = _canonical_uuid(expected_account)
        if ctx.plugin_id != _PLUGIN:
            raise ValueError("interactive authority is restricted to quick-record")
        if not isinstance(expected_epoch, str) or not expected_epoch or expected_epoch != ctx.config_epoch:
            raise ValueError("quick-record consent epoch changed; refresh before recording")
        super().__init__(ctx, transport=transport, require_enabled=False)

    def guard(self, *, mutating=False, require_enabled=None):
        if self.ctx.plugin_id != _PLUGIN:
            raise RuntimeError("quick-record authority changed; refresh before recording")
        # Only this dedicated interactive actor may ingest without a source's
        # enabled flag. Keep all account/settings/epoch/preview/request fences.
        super().guard(mutating=mutating, require_enabled=False)
        if self.account_id != self.expected_account:
            raise RuntimeError("Collect account changed; refresh before recording")

    def validate_action_record(self, data_type, record):
        self.guard()
        return V1DataClient(self.client).validate_records(data_type, [record])[0]

    def submit_action_record(self, data_type, record, *, before_submit):
        """One guarded record attempt with a durable pre-dispatch checkpoint."""
        with self.record_write_scope():
            return V1DataClient(self.client).ingest(data_type, [record], before_submit=before_submit)

    def exclude_action_record(self, *, data_type, record_id, action_id, requested_at, evidence):
        with self.record_write_scope():
            return self._exclusions(allow_create=True).exclude(data_type=data_type,
                record_id=record_id, action_id=action_id, requested_at=requested_at,
                reason="undo", evidence=evidence)
