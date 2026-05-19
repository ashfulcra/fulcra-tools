"""Lightweight Fulcra ingest client for general CSV → annotation.

Service-agnostic: caller supplies the target annotation definition id and
optional tag ids. Supports both DurationAnnotation and InstantAnnotation
shapes; data_type can be overridden for custom annotation kinds.
"""
from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import quote

import httpx

from .events import DURATION, GenericEvent

DEFAULT_BASE_URL = os.environ.get("FULCRA_API_BASE", "https://api.fulcradynamics.com")


@dataclass
class ImportResult:
    total: int
    skipped_existing: int
    posted: int
    verified: int


def _default_data_type(annotation_type: str) -> str:
    return "DurationAnnotation" if annotation_type == DURATION else "InstantAnnotation"


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
                headers={"User-Agent": "fulcra-csv-importer/0.1"},
                # follow_redirects=False so the Authorization header
                # we attach per-request never rides along on a 3xx to a
                # host the user didn't intend. (httpx's auto auth-strip
                # only applies to client-level auth, not per-request
                # Authorization headers, which is how we send the
                # bearer token.) If a redirect is legitimately required,
                # callers should handle it explicitly with re-auth.
                follow_redirects=False,
            )
        return self._http

    def _authed_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.get_token()}"}

    def soft_delete_definition(self, definition_id: str) -> bool:
        """Soft-delete an annotation definition.

        Returns True on a successful 204. The events under the def are NOT
        removed from query results — they stay visible but their source_id
        points at a deleted def. To effectively "reset" a stream of imports,
        soft-delete the def AND bump the source-id prefix on the importer
        so the next run doesn't get silently deduped against the orphans.

        This is the only delete primitive Fulcra currently exposes; there's
        no per-event delete (probed and confirmed 2026-05-17).
        """
        r = self._client().delete(
            f"/user/v1alpha1/annotation/{definition_id}",
            headers=self._authed_headers(),
        )
        if r.status_code == 204:
            return True
        if r.status_code == 404:
            return False
        r.raise_for_status()
        return False

    def ensure_tag(self, name: str) -> str:
        c = self._client()
        # urlencode the name so tags with `/`, `?`, `#`, or spaces don't
        # break the GET path. Fulcra validates the value server-side, but
        # the URL itself must stay well-formed.
        r = c.get(f"/user/v1alpha1/tag/name/{quote(name, safe='')}", headers=self._authed_headers())
        if r.status_code == 200:
            return r.json()["id"]
        r = c.post(
            "/user/v1alpha1/tag",
            json={"name": name},
            headers=self._authed_headers(),
        )
        r.raise_for_status()
        return r.json()["id"]

    def _build_record(
        self,
        ev: GenericEvent,
        *,
        definition_id: str | None,
        tag_id_for: dict[str, str],
        data_type: str | None,
    ) -> dict:
        tag_id = tag_id_for.get(ev.tag or "") if ev.tag else None
        tags = [tag_id] if tag_id else []
        # Only include fields that are actually populated. When targeting a
        # built-in Fulcra type (e.g. BodyMass), the schema may not have a
        # `note` field — emitting empties pollutes downstream consumers.
        data_inner: dict = {}
        if ev.note:
            data_inner["note"] = ev.note
        if ev.title:
            data_inner["title"] = ev.title
        if ev.value is not None:
            data_inner["value"] = ev.value
        if ev.tag:
            data_inner["tag"] = ev.tag
        data_inner.update(ev.data_fields)
        if ev.external_ids:
            data_inner["external_ids"] = ev.external_ids

        recorded_at: dict = {
            "start_time": ev.start_time.isoformat().replace("+00:00", "Z"),
        }
        if ev.annotation_type == DURATION:
            assert ev.end_time is not None  # enforced in GenericEvent
            recorded_at["end_time"] = ev.end_time.isoformat().replace("+00:00", "Z")

        # Source array: source_id is always first (the per-row dedup key).
        # The annotation-def source is appended ONLY when targeting a
        # user-defined annotation. Built-in data types (BodyMass, HeartRate,
        # ...) don't have a definition id and dedup purely on source_id.
        source: list[str] = [ev.source_id]
        if definition_id:
            source.append(f"com.fulcradynamics.annotation.{definition_id}")

        return {
            "specversion": 1,
            "data": json.dumps(data_inner, sort_keys=True),
            "metadata": {
                "data_type": data_type or _default_data_type(ev.annotation_type),
                "recorded_at": recorded_at,
                "tags": tags,
                "source": source,
                "content_type": "application/json",
            },
        }

    def ingest_batch(
        self,
        events: list[GenericEvent],
        *,
        definition_id: str | None = None,
        tag_id_for: dict[str, str] | None = None,
        data_type: str | None = None,
    ) -> None:
        if not events:
            return
        tag_id_for = tag_id_for or {}
        lines = [
            json.dumps(
                self._build_record(
                    ev, definition_id=definition_id, tag_id_for=tag_id_for,
                    data_type=data_type,
                ),
                sort_keys=True,
            ).encode()
            for ev in events
        ]
        r = self._client().post(
            "/ingest/v1/record/batch",
            content=b"\n".join(lines),
            headers={
                **self._authed_headers(),
                "content-type": "application/x-jsonl",
            },
        )
        r.raise_for_status()

    def fetch_records(
        self,
        start: datetime,
        end: datetime,
        *,
        data_type: str = "DurationAnnotation",
    ) -> list[dict]:
        """Return raw records of `data_type` over [start, end].

        Shared by dedup (fetch_existing_source_ids) and export. The Fulcra
        endpoint returns either a plain list or `{"data": [...]}` depending
        on the response shape; both are normalised here.
        """
        r = self._client().get(
            f"/data/v1alpha1/event/{data_type}",
            params={
                "start_time": start.isoformat().replace("+00:00", "Z"),
                "end_time": end.isoformat().replace("+00:00", "Z"),
            },
            headers=self._authed_headers(),
        )
        r.raise_for_status()
        body = r.json()
        if isinstance(body, list):
            return body
        return body.get("data", []) or []

    def fetch_existing_source_ids(
        self,
        start: datetime,
        end: datetime,
        *,
        data_type: str = "DurationAnnotation",
        only_for_defs: set[str] | None = None,
    ) -> set[str]:
        """Return the set of source-id strings present in [start, end].

        only_for_defs: when set, restrict to records whose top-level source_id
        is in this set. This is the dedup-vs-orphan story for user-defined
        annotations: prior soft-deleted defs still surface records, but
        their source_id points at the deleted def. For built-in types
        (BodyMass etc.) pass None — there's no definition to filter on.
        """
        records = self.fetch_records(start, end, data_type=data_type)
        out: set[str] = set()
        for rec in records:
            if only_for_defs is not None and rec.get("source_id") not in only_for_defs:
                continue
            for s in rec.get("sources") or []:
                out.add(s)
            for s in (rec.get("metadata") or {}).get("source") or []:
                out.add(s)
        return out

    def run_import(
        self,
        events: list[GenericEvent],
        *,
        definition_id: str | None = None,
        tag_id_for: dict[str, str] | None = None,
        data_type: str | None = None,
        chunk_size: int = 500,
        window_pad_minutes: int = 10,
    ) -> ImportResult:
        events = list(events)
        total = len(events)
        if total == 0:
            return ImportResult(0, 0, 0, 0)

        events_sorted = sorted(events, key=lambda e: e.start_time)
        posted = 0
        skipped = 0
        verified = 0
        only_for_defs = (
            {f"com.fulcradynamics.annotation.{definition_id}"} if definition_id else None
        )
        # Use the events' actual data type for the readback endpoint —
        # otherwise an instant import would read back DurationAnnotation and
        # find nothing, missing the dedup.
        read_data_type = data_type or _default_data_type(events_sorted[0].annotation_type)

        for i in range(0, len(events_sorted), chunk_size):
            chunk = events_sorted[i : i + chunk_size]
            win_start = min(e.start_time for e in chunk) - timedelta(minutes=window_pad_minutes)
            win_end_dt = max((e.end_time or e.start_time) for e in chunk)
            win_end = win_end_dt + timedelta(minutes=window_pad_minutes)

            existing = self.fetch_existing_source_ids(
                win_start, win_end, data_type=read_data_type,
                only_for_defs=only_for_defs,
            )
            new_events = [e for e in chunk if e.source_id not in existing]
            skipped += len(chunk) - len(new_events)

            if new_events:
                self.ingest_batch(
                    new_events, definition_id=definition_id, tag_id_for=tag_id_for,
                    data_type=data_type,
                )
                posted += len(new_events)
                after = self.fetch_existing_source_ids(
                    win_start, win_end, data_type=read_data_type,
                    only_for_defs=only_for_defs,
                )
                verified += sum(1 for e in new_events if e.source_id in after)

        return ImportResult(total=total, skipped_existing=skipped, posted=posted, verified=verified)
