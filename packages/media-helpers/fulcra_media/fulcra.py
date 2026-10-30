"""Read/admin client compatibility and captured Collect v1 media bridge.

Unbound legacy record writes are retired. Standalone imports use the configured
v1 CLI; Collect plugins delegate through their captured guarded runtime.
ImportResult remains re-exported for existing callers.
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING

from fulcra_common import BaseFulcraClient, ImportResult

from .state import State

if TYPE_CHECKING:
    from .importers.base import NormalizedEvent

__all__ = ["FulcraClient", "ImportResult"]

_MEDIA_NAMESPACE = "com.fulcra.media."
_RETIRED_WRITER = (
    "Legacy media record writes are retired; use the configured v1 standalone CLI "
    "or a captured Collect runtime"
)


def _importer_namespace_prefix(det_id: str) -> str | None:
    """Per-importer namespace prefix of a deterministic_id, or None.

    Importer det-ids share the shape ``com.fulcra.media.<importer>.<rest>``
    where ``<rest>`` varies across importers: ``v1.<sha16>`` (lastfm,
    deezer, spotify-extended, ...), ``v2.<sha16>`` (netflix), a bare
    ``<sha16>`` with NO version segment (netflix-rich), ``v1.history.<id>``
    (trakt), ``v1.<service id>`` (strava). The one stable invariant is the
    importer segment immediately after ``com.fulcra.media.`` — so the
    prefix is derived as ``com.fulcra.media.<importer>.`` rather than by
    stripping a trailing ``v1.<hash>``, which several importers don't have.

    Returns None for anything that doesn't fit the shape; callers fall
    back to the conservative skip-on-fingerprint-match behaviour.
    """
    if not det_id.startswith(_MEDIA_NAMESPACE):
        return None
    rest = det_id[len(_MEDIA_NAMESPACE):]
    importer_seg, _, tail = rest.partition(".")
    if not importer_seg or not tail:
        return None
    return f"{_MEDIA_NAMESPACE}{importer_seg}."


class FulcraClient(BaseFulcraClient):
    USER_AGENT = "fulcra-media-helpers/0.1"

    def __init__(self, *args, collect_context=None, **kwargs):
        self._collect_context = collect_context
        self._collect_v1 = getattr(collect_context, "_media_record_writer", None)
        if collect_context is not None and self._collect_v1 is None:
            raise RuntimeError("Collect media client requires a guarded v1 run")
        super().__init__(*args, **kwargs)

    def get_token(self) -> str:
        if self._collect_v1 is not None:
            return self._collect_v1.session.client.get_token()
        return super().get_token()

    def _client(self):
        if self._collect_v1 is not None:
            return self._collect_v1.session.client._client()
        return super()._client()

    def _lib(self):
        if self._collect_v1 is not None:
            raise RuntimeError("Legacy media API is unavailable in a Collect v1 run")
        return super()._lib()

    @property
    def record_destination_ids(self):
        return self._collect_v1.destination_ids if self._collect_v1 is not None else None

    @property
    def record_preview_mode(self):
        return self._collect_context is not None and any(
            self._collect_context.config.get(key) is True for key in ("dry_run", "preview_only"))

    def prepare_media(self, categories):
        if self._collect_v1 is None:
            raise RuntimeError("Service destination preparation requires a guarded Collect v1 run")
        return self._collect_v1.prepare(categories)

    def ensure_tag(self, name: str, state: State) -> str:
        """Look up / create a tag, caching the id in `state.tag_ids`."""
        if self._collect_v1 is not None:
            preview = any(self._collect_context.config.get(key) is True for key in ("dry_run", "preview_only"))
            return self._collect_v1.session.resolve_tag(name, allow_create=not preview)
        if name in state.tag_ids:
            return state.tag_ids[name]
        tag_id = self._resolve_tag(name)
        state.tag_ids[name] = tag_id
        return tag_id

    def ensure_definitions(self, state: State) -> None:
        """Refuse legacy annotation setup, including within Collect."""
        if self._collect_v1 is None:
            raise RuntimeError(_RETIRED_WRITER)
        raise RuntimeError("Use v1 destination setup before collecting media")

    def ingest_batch(
        self, events: list["NormalizedEvent"], state: State
    ) -> ImportResult:
        """Delegate receiver batches to the captured Collect v1 writer."""
        if self._collect_v1 is None:
            raise RuntimeError(_RETIRED_WRITER)
        return self.run_import(events, state)

    def ingest_batch_typed(
        self, events: list["NormalizedEvent"], state: State
    ) -> ImportResult:
        """Compatibility entry point for the captured Collect v1 writer."""
        if self._collect_v1 is None:
            raise RuntimeError(_RETIRED_WRITER)
        return self.run_import(events, state)

    def run_import(
        self,
        events: "list[NormalizedEvent]",
        state: State,
        chunk_size: int = 500,
        window_pad_minutes: int = 10,
        check_only: bool = False,
        claim: "Callable[[set[str]], bool] | None" = None,
        unclaim: "Callable[[set[str]], None] | None" = None,
        updates_summary: "Callable[[datetime, datetime], dict[str, int]] | None" = None,
        sleep_fn: "Callable[[float], None] | None" = None,
    ) -> ImportResult:
        """Delegate to Collect v1, forcing preview from the captured context.

        Legacy claim/readback callbacks and state remain accepted for caller
        compatibility; only the native writer owns persistence and recovery.
        """
        if self._collect_v1 is None:
            raise RuntimeError(_RETIRED_WRITER)
        preview = any(self._collect_context.config.get(key) is True for key in ("dry_run", "preview_only"))
        return self._collect_v1.run_import(events, chunk_size=chunk_size, check_only=check_only or preview)
