"""Fulcra API client + run-import pipeline.

Single point of contact with the Fulcra REST API. Importers produce
NormalizedEvent instances; this module handles auth, definitions, tags,
ingest, dedup readback, and verification.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime
from typing import TYPE_CHECKING

import httpx

from .state import State

if TYPE_CHECKING:
    from .importers.base import NormalizedEvent

DEFAULT_BASE_URL = os.environ.get("FULCRA_API_BASE", "https://api.fulcradynamics.com")


class FulcraClient:
    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url
        self._transport = transport
        self._http: httpx.Client | None = None

    def get_token(self) -> str:
        env = os.environ.get("FULCRA_ACCESS_TOKEN")
        if env:
            return env
        try:
            result = subprocess.run(
                ["fulcra", "auth", "print-access-token"],
                check=True,
                capture_output=True,
            )
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(
                "fulcra auth print-access-token failed; run `fulcra auth login` first. "
                f"stderr={exc.stderr!r}"
            ) from exc
        return result.stdout.decode().strip()

    def _client(self) -> httpx.Client:
        if self._http is None:
            self._http = httpx.Client(
                base_url=self.base_url,
                transport=self._transport,
                timeout=30.0,
                headers={"User-Agent": "fulcra-media-helpers/0.1"},
            )
        return self._http

    def _authed_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.get_token()}"}

    def ensure_tag(self, name: str, state: State) -> str:
        if name in state.tag_ids:
            return state.tag_ids[name]
        c = self._client()
        r = c.get(f"/user/v1alpha1/tag/name/{name}", headers=self._authed_headers())
        if r.status_code == 200:
            tag_id = r.json()["id"]
        else:
            r = c.post(
                "/user/v1alpha1/tag",
                json={"name": name},
                headers=self._authed_headers(),
            )
            r.raise_for_status()
            tag_id = r.json()["id"]
        state.tag_ids[name] = tag_id
        return tag_id

    def ensure_definitions(self, state: State) -> None:
        if state.watched_definition_id and state.listened_definition_id:
            return
        media = self.ensure_tag("media", state)
        watched = self.ensure_tag("watched", state)
        listened = self.ensure_tag("listened", state)

        if not state.watched_definition_id:
            state.watched_definition_id = self._create_duration_definition(
                name="Watched",
                description="Media content watched (movies, TV, video).",
                tags=[media, watched],
            )
        if not state.listened_definition_id:
            state.listened_definition_id = self._create_duration_definition(
                name="Listened",
                description="Media content listened to (music, podcasts).",
                tags=[media, listened],
            )

    def _create_duration_definition(self, name: str, description: str, tags: list[str]) -> str:
        body = {
            "annotation_type": "duration",
            "name": name,
            "description": description,
            "tags": tags,
            "measurement_spec": {
                "measurement_type": "duration",
                "value_type": "duration",
                "unit": None,
            },
        }
        r = self._client().post(
            "/user/v1alpha1/annotation",
            json=body,
            headers=self._authed_headers(),
        )
        r.raise_for_status()
        return r.json()["id"]

    def ingest_batch(
        self, events: list["NormalizedEvent"], state: "State"
    ) -> None:
        if not events:
            return
        lines: list[bytes] = []
        for ev in events:
            def_id = (
                state.watched_definition_id
                if ev.category == "watched"
                else state.listened_definition_id
            )
            if def_id is None:
                raise RuntimeError(
                    f"missing {ev.category} definition id in state; run bootstrap first"
                )
            data_inner = {
                "note": ev.note,
                "title": ev.title,
                "service": ev.service,
                "timestamp_confidence": ev.timestamp_confidence,
                "external_ids": ev.external_ids,
            }
            service_tag = state.tag_ids.get(ev.service)
            tags = [service_tag] if service_tag else []
            metadata = {
                "data_type": "DurationAnnotation",
                "recorded_at": {
                    "start_time": ev.start_time.isoformat().replace("+00:00", "Z"),
                    "end_time":   ev.end_time.isoformat().replace("+00:00", "Z"),
                },
                "tags": tags,
                "source": [ev.deterministic_id, f"com.fulcradynamics.annotation.{def_id}"],
                "content_type": "application/json",
            }
            line = {
                "specversion": 1,
                "data": json.dumps(data_inner, sort_keys=True),
                "metadata": metadata,
            }
            lines.append(json.dumps(line, sort_keys=True).encode())
        body = b"\n".join(lines)
        r = self._client().post(
            "/ingest/v1/record/batch",
            content=body,
            headers={
                **self._authed_headers(),
                "content-type": "application/x-jsonl",
            },
        )
        r.raise_for_status()

    def fetch_existing_source_ids(
        self, start: datetime, end: datetime
    ) -> set[str]:
        r = self._client().get(
            "/data/v1alpha1/event/DurationAnnotation",
            params={
                "start_time": start.isoformat().replace("+00:00", "Z"),
                "end_time": end.isoformat().replace("+00:00", "Z"),
            },
            headers=self._authed_headers(),
        )
        r.raise_for_status()
        records = r.json() if isinstance(r.json(), list) else r.json().get("data", [])
        out: set[str] = set()
        for rec in records:
            for s in (rec.get("metadata") or {}).get("source") or []:
                out.add(s)
        return out
