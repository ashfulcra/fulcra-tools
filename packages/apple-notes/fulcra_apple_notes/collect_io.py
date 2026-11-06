"""Explicit Collect Files transport and consent for the Notes-only workflow."""
from contextlib import contextmanager
from copy import deepcopy
import os

import puremagic

from fulcra_collect import config
from fulcra_collect.config_leases import task_mutation_scope

from fulcra_collect.files_io import CollectFiles


class NotesConsent:
    """Local-only preview still honors captured source consent without auth."""
    def __init__(self, ctx, session=None):
        self.ctx, self.session = ctx, session
        raw = getattr(ctx, "_persisted_config", None)
        self.settings = deepcopy(ctx.config if raw is None else raw)
        self.epoch = ctx.config_epoch

    def guard(self, *, mutating=False):
        if self.session is not None:
            return self.session.guard(mutating=mutating)
        current = config.load()
        if (mutating or current.account_transition or self.ctx.plugin_id not in current.enabled
                or current.plugin_epochs.get(self.ctx.plugin_id, "") != self.epoch
                or not config.settings_match(current.plugin_settings.get(self.ctx.plugin_id, {}), self.settings)):
            raise RuntimeError("Collect configuration changed; retry the selected Notes source")

    @contextmanager
    def mutation_scope(self, *, mutating=True):
        with task_mutation_scope():
            self.guard(mutating=mutating)
            yield
            self.guard(mutating=mutating)


class CollectNotesFiles(CollectFiles):
    """Shared Collect Files transport plus content-based Notes attachments."""
    def upload_file(self, local_path, remote, *, timeout=300):
        self.session.guard()
        with open(local_path, "rb") as handle:
            try:
                content_type = puremagic.from_stream(handle, mime=True)
            except puremagic.PureError:
                content_type = "application/octet-stream"
            handle.seek(0)
            self._upload(handle, remote, os.fstat(handle.fileno()).st_size, content_type, timeout=timeout)
