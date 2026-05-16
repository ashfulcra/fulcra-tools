"""Fulcra API client + run-import pipeline.

Single point of contact with the Fulcra REST API. Importers produce
NormalizedEvent instances; this module handles auth, definitions, tags,
ingest, dedup readback, and verification.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

import httpx

from .state import State

if TYPE_CHECKING:
    from .importers.base import NormalizedEvent

DEFAULT_BASE_URL = os.environ.get("FULCRA_API_BASE", "https://api.fulcradynamics.com")


@dataclass
class ImportResult:
    total: int
    skipped_existing: int
    posted: int
    verified: int


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
                follow_redirects=True,
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
            for s in rec.get("sources") or []:
                out.add(s)
            for s in (rec.get("metadata") or {}).get("source") or []:
                out.add(s)
        return out

    def run_import(
        self,
        events: "list[NormalizedEvent]",
        state: State,
        chunk_size: int = 500,
        window_pad_minutes: int = 10,
    ) -> ImportResult:
        events = list(events)
        total = len(events)
        if total == 0:
            return ImportResult(0, 0, 0, 0)

        # Dedup readback and verification both operate per-chunk on the chunk's
        # own narrow time window. The Fulcra event endpoint has an undocumented
        # pagination ceiling (~4,000 records) and no cursor/limit param, so a
        # single readback over a multi-year window misses records. Per-chunk
        # narrow windows stay well under the ceiling.
        events_sorted = sorted(events, key=lambda e: e.start_time)
        posted = 0
        skipped = 0
        verified = 0

        for i in range(0, len(events_sorted), chunk_size):
            chunk = events_sorted[i : i + chunk_size]
            win_start = min(e.start_time for e in chunk) - timedelta(minutes=window_pad_minutes)
            win_end = max(e.end_time for e in chunk) + timedelta(minutes=window_pad_minutes)

            existing = self.fetch_existing_source_ids(win_start, win_end)
            new_events = [e for e in chunk if e.deterministic_id not in existing]
            skipped += len(chunk) - len(new_events)

            if new_events:
                self.ingest_batch(new_events, state)
                posted += len(new_events)
                after = self.fetch_existing_source_ids(win_start, win_end)
                verified += sum(1 for e in new_events if e.deterministic_id in after)

        if verified < posted:
            raise RuntimeError(
                f"verified {verified} < posted {posted} — readback did not see "
                f"all newly-ingested events across the chunked windows."
            )
        return ImportResult(total=total, skipped_existing=skipped, posted=posted, verified=verified)
