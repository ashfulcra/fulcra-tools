"""Explicit CLI authority over the shared record kernel and profile-local outbox.

Lock order: invocation namespace, shared task/config, account record lease.
No saved Collect credentials, plugin enablement or daemon source consent is used.
"""
from __future__ import annotations

import fcntl
import logging
import math
import time
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from uuid import UUID

import httpx
from fulcra_common.client import BaseFulcraClient
from fulcra_common.json_values import snapshot_json

from . import config, db
from .config_leases import _file_lock
from .plugin import RunContext
from .state import PluginState
from .v1_session import CollectRecordSession, _CollectClient

STANDALONE_RUNTIME_VERSION = 1
_NAMESPACES = frozenset({'cli-dayone', 'cli-media', 'cli-csv', 'cli-labs', 'cli-netflix'})


def standalone_namespace_supported(name) -> bool:
    """Pure installed-capability query; no profile, client or auth access."""
    return isinstance(name, str) and name in _NAMESPACES


class StandaloneRecordSession(CollectRecordSession):
    """Explicit invocation consent, independent of daemon sign-in/settings."""

    def __init__(self, ctx, *, client: BaseFulcraClient, profile: Path,
                 transport=None, identity_transport=None):
        self.ctx = ctx
        self.authority = client
        self.profile = profile.resolve()
        self.origin = client.base_url
        self.epoch = ctx.config_epoch
        self.settings = snapshot_json(ctx.config)
        self.preview_settings = deepcopy(self.settings)
        self.settings_fingerprint = config.settings_fingerprint(self.settings)
        self.require_enabled = False
        self._cancelled = False
        self._exclusion_type = None
        self._identity_http = httpx.Client(base_url=self.origin, transport=identity_transport,
                                          timeout=10, follow_redirects=False)
        try:
            self._token, self.account_id = self._prove_identity()
            self.client = _CollectClient(self, transport=transport)
            self.client.base_url = self.origin
            self.guard()
        except BaseException:
            self._identity_http.close()
            raise

    def cancel(self):
        self._cancelled = True

    def _local_guard(self):
        if config.profile_path() != self.profile:
            raise RuntimeError("Standalone profile changed; retain pending work")
        if (self._cancelled or self.authority.base_url != self.origin
                or (hasattr(self, "client") and self.client.base_url != self.origin)
                or self.ctx.config_epoch != self.epoch
                or not config.settings_match(self.ctx.config, self.settings)):
            raise RuntimeError("Standalone authority changed; retain pending work")

    def _prove_identity(self):
        self._local_guard()
        try:
            token = self.authority.get_token()
            if not isinstance(token, str) or not token.strip():
                raise ValueError
            response = self._identity_http.get('/user/v1alpha1/info',
                headers={'Authorization': 'Bearer ' + token})
            response.raise_for_status()
            body = response.json()
            account = str(UUID(body['userid']))
        except Exception:
            raise RuntimeError("Standalone authentication unavailable; retain pending work") from None
        self._local_guard()
        return token, account

    def guard(self, *, mutating=False, require_enabled=None):
        token, account = self._prove_identity()
        if account != self.account_id:
            raise RuntimeError("Standalone authority changed; retain pending work")
        if mutating:
            for key in ('dry_run', 'preview_only'):
                if type(self.ctx.config.get(key, False)) is not bool or self.ctx.config.get(key, False):
                    raise RuntimeError("Standalone preview enabled; no records uploaded")
        self._token = token

    def close(self):
        super().close()
        self._identity_http.close()


@contextmanager
def standalone_runtime(namespace, settings, *, client=None, transport=None,
                       identity_transport=None, lease_timeout_s=10):
    """Yield a real scoped durable context/session under an exclusive CLI lease.

    Call the writer's recovery method before opening its source. Namespaces are
    code-owned constants: users cannot choose another plugin's state namespace.
    """
    if not standalone_namespace_supported(namespace):
        raise ValueError('unsupported standalone namespace')
    captured = snapshot_json(settings)
    if not isinstance(captured, dict):
        raise ValueError('standalone settings must be an object')
    fingerprint = config.settings_fingerprint(captured)
    timeout = float(lease_timeout_s)
    if not math.isfinite(timeout) or timeout < 0:
        raise ValueError('standalone lease timeout must be finite and nonnegative')
    root = config.profile_path()
    with config.pinned_profile(root):
        with _file_lock(config.config_dir() / ('.' + namespace + '.lock'),
                        time.monotonic() + min(timeout, 30), mode=fcntl.LOCK_EX):
            def check_profile():
                if config.profile_path() != root:
                    raise RuntimeError('Standalone profile changed; retain pending work')

            # Open only the selected profile, never the daemon's dayone state.
            check_profile()
            conn = db.open(root / 'state.db')
            def call(function, *args):
                check_profile()
                return function(conn, namespace, *args)

            ctx = RunContext(namespace, deepcopy(captured), {}, PluginState(namespace),
                logging.getLogger(namespace), lambda event: None,
                config_epoch=fingerprint,
                _plugin_kv_get=lambda key, default: call(db.get_plugin_kv, key, default),
                _plugin_kv_set=lambda key, value: call(db.set_plugin_kv, key, value),
                _record_outbox_get=lambda key, default: call(db.get_record_outbox, key, default),
                _record_outbox_set=lambda key, value: call(db.set_record_outbox, key, value))
            session = StandaloneRecordSession(ctx, client=client or BaseFulcraClient(),
                profile=root, transport=transport, identity_transport=identity_transport)
            try:
                yield ctx, session
            finally:
                session.close()
