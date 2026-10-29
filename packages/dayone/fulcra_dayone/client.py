"""Explicit CLI entrypoint for the shared v1 JournalWriter.

API migration: annotation-definition/bootstrap and GenericEvent import methods
are retired. Use ``with client.open(invocation_settings) as writer`` and pass
original DayOneEntry values to JournalWriter after calling writer.resume().
"""
from contextlib import contextmanager

from fulcra_common.client import BaseFulcraClient
from fulcra_collect.standalone_v1 import standalone_runtime

from .collect_v1 import JournalWriter


class DayOneFulcraClient(BaseFulcraClient):
    USER_AGENT = 'fulcra-dayone/v1'

    @contextmanager
    def open(self, settings):
        with standalone_runtime('cli-dayone', settings, client=self,
                transport=self._transport, identity_transport=self._transport) as (ctx, session):
            yield JournalWriter(ctx, session)

    def ensure_journal_definition(self):
        raise RuntimeError('Annotation API retired: use open(settings) with the v1 JournalWriter')

    def run_import(self, *_args, **_kwargs):
        raise RuntimeError('GenericEvent API retired: pass DayOneEntry values to the v1 JournalWriter')
