"""Legacy CSV read/admin/tag compatibility; annotation writes fail before effects."""

from __future__ import annotations

from fulcra_common import BaseFulcraClient, ImportResult


__all__ = ["FulcraClient", "ImportResult"]


class FulcraClient(BaseFulcraClient):
    USER_AGENT = "fulcra-csv-importer/0.1"
    # follow_redirects=False so the per-request Authorization header never
    # rides along on a 3xx to a host the user didn't intend. This client
    # percent-encodes tag names (quote_name below), so it doesn't depend on
    # following the 303 the tag-name lookup answers for some names.
    FOLLOW_REDIRECTS = False

    def ensure_tag(self, name: str) -> str:
        # quote_name=True percent-encodes the name in the lookup path so
        # tags with `/`, `?`, `#`, or spaces don't break the GET.
        return self._resolve_tag(name, quote_name=True)

    def _build_record(self, *args, **kwargs):
        raise RuntimeError(
            "Legacy CSV annotation writer retired; use fulcra-csv import with a full v1 destination"
        )

    def ingest_batch(self, *args, **kwargs):
        raise RuntimeError(
            "Legacy CSV annotation writer retired; use fulcra-csv import with a full v1 destination"
        )

    def run_import(self, *args, **kwargs):
        raise RuntimeError(
            "Legacy CSV annotation writer retired; use fulcra-csv import with a full v1 destination"
        )
