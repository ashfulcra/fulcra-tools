"""Fulcra client for fulcra-dayone — adds the Journal definition bootstrap.

Subclasses fulcra_csv.FulcraClient so it inherits run_import, ensure_tag,
the httpx client, and auth from fulcra-common.
"""
from __future__ import annotations

from fulcra_csv.fulcra import FulcraClient

JOURNAL_DEFINITION_NAME = "Journal"


class DayOneFulcraClient(FulcraClient):
    USER_AGENT = "fulcra-dayone/0.1"

    def ensure_journal_definition(self) -> str:
        """Return the id of the live "Journal" InstantAnnotation
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
            and d.get("annotation_type") == "instant"
            and not d.get("deleted_at")
        ]
        if matches:
            matches.sort(key=lambda d: d.get("created_at") or "")
            return matches[0]["id"]
        body = {
            "annotation_type": "instant",
            "name": JOURNAL_DEFINITION_NAME,
            "description": "Day One journal entries.",
            "tags": [],
            "measurement_spec": {
                "measurement_type": "instant",
                "value_type": "none",
                "unit": None,
            },
        }
        r = self._client().post(
            "/user/v1alpha1/annotation", json=body,
            headers=self._authed_headers(),
        )
        r.raise_for_status()
        return r.json()["id"]
