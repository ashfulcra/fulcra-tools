"""Media source seam for configured standalone file and poll CLI imports.

Authority settings describe stable destinations, not source selections. Callers
must open this context before acquiring a new source sample, so recovery owns
precedence even when that sample would be empty. Preview remains read-only and
never recovers pending work. Importer and daemon entry points retain their own
defaults.
"""
from contextlib import contextmanager
from dataclasses import dataclass
from copy import deepcopy

from fulcra_collect.standalone_v1 import standalone_runtime
from fulcra_common.client import ImportResult

from .collect_v1 import MediaRecordWriter


@dataclass(frozen=True)
class StandaloneMediaRun:
    writer: MediaRecordWriter
    ctx: object
    session: object
    _check_only: bool

    @property
    def progress(self):
        """Writer's immutable last-invocation lower bound; local after close.

        Dedicated standalone callers should perform one invocation per context
        when attributing an error; recovery/source acquisition is not an import.
        """
        return self.writer.progress

    def inspect_twins(self, events):
        return self.writer.inspect_twins(events)

    def import_events(self, events, *, chunk_size=500, twin_policy=None, approved_twins=None) -> ImportResult:
        return self.writer.run_import(events, chunk_size=chunk_size,
                                      check_only=self._check_only, twin_policy=twin_policy,
                                      approved_twins=approved_twins)


@contextmanager
def open_media(settings, *, client=None, check_only=False, before_resume=None):
    """Recover one durable cli-media authority before yielding its source seam.

    Preview captures separate, read-only authority and never adopts a pending
    write. The shared runtime owns JSON validation, snapshots and all fences.
    ``before_resume`` is an internal account/configuration/journal verifier;
    it runs before recovery and must never acquire a new source sample.
    """
    if not isinstance(settings, dict):
        raise ValueError('standalone settings must be an object')
    if type(check_only) is not bool:
        raise ValueError('check_only must be on or off')
    for key in ('dry_run', 'preview_only'):
        if key in settings:
            value = settings[key]
            if type(value) is not bool or value != check_only:
                raise ValueError('preview flags must agree with check_only')
    authority = deepcopy(settings)
    if check_only:
        authority['preview_only'] = True
    transport = client._transport if client is not None else None
    with standalone_runtime('cli-media', authority, client=client,
                            transport=transport, identity_transport=transport) as (ctx, session):
        if before_resume is not None:
            before_resume(ctx, session)
        writer = MediaRecordWriter(ctx, session)
        if not check_only:
            writer.resume()
        yield StandaloneMediaRun(writer, ctx, session, check_only)
