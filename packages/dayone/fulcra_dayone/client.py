"""Fulcra client for fulcra-dayone — adds the Journal definition bootstrap.

Subclasses fulcra_csv.FulcraClient so it inherits run_import, ensure_tag,
the httpx client, and auth from fulcra-common.
"""
from __future__ import annotations

from typing import Any

from fulcra_common import ImportResult
from fulcra_csv.events import GenericEvent
from fulcra_csv.fulcra import FulcraClient

JOURNAL_DEFINITION_NAME = "Journal"

# A Day One entry is a point-in-time annotation. In Fulcra's model the
# annotation *definition* type for that is "moment" (the enum has no
# "instant" — the valid values are boolean/duration/moment/numeric/
# people/scale), and the recorded events read back under the
# "MomentAnnotation" data type. csv-importer's GenericEvent still calls
# the point-in-time event shape INSTANT; that internal name only controls
# the recorded_at shape (a bare scalar timestamp, vs a duration's
# {start_time, end_time} range) and never reaches the API.
JOURNAL_ANNOTATION_TYPE = "moment"
JOURNAL_DATA_TYPE = "MomentAnnotation"


class DayOneFulcraClient(FulcraClient):
    USER_AGENT = "fulcra-dayone/0.1"
    # Fulcra's write endpoints answer 303 See Other, pointing at the newly
    # created resource (POST-redirect-GET) — the annotation-definition POST
    # and the tag POST/lookup all do this. Follow the redirect rather than
    # raise on it. csv-importer's FulcraClient sets this False because it
    # percent-encodes tag names and resolves them without a redirect; that
    # is a csv-importer detail, not one fulcra-dayone should inherit. True
    # is the BaseFulcraClient default; httpx still strips the Authorization
    # header on any cross-origin hop, so following same-host 303s is safe.
    FOLLOW_REDIRECTS = True

    def ensure_journal_definition(self) -> str:
        """Return the id of the live "Journal" moment-annotation
        definition, creating it if none exists. If duplicates exist,
        returns the oldest by created_at — so every run converges on the
        same definition."""
        r = self._client().get(
            "/user/v1alpha1/annotation", headers=self._authed_headers(),
        )
        r.raise_for_status()
        matches = [
            d for d in r.json()
            if d.get("name") == JOURNAL_DEFINITION_NAME
            and d.get("annotation_type") == JOURNAL_ANNOTATION_TYPE
            and not d.get("deleted_at")
        ]
        if matches:
            matches.sort(key=lambda d: d.get("created_at") or "")
            return matches[0]["id"]
        body = {
            "annotation_type": JOURNAL_ANNOTATION_TYPE,
            "name": JOURNAL_DEFINITION_NAME,
            "description": "Day One journal entries.",
            "tags": [],
        }
        r = self._client().post(
            "/user/v1alpha1/annotation", json=body,
            headers=self._authed_headers(),
        )
        r.raise_for_status()
        return r.json()["id"]

    def run_import(self, events: list[GenericEvent], **kwargs: Any) -> ImportResult:
        """Import journal entries, targeting the MomentAnnotation data type.

        Day One entries land as `MomentAnnotation` events; both the
        dedup-readback and the ingest must use that data type. (The
        inherited default would pick `InstantAnnotation`, which Fulcra
        does not expose — querying it 404s.)
        """
        kwargs.setdefault("data_type", JOURNAL_DATA_TYPE)
        return super().run_import(events, **kwargs)
