"""Lightweight Fulcra ingest client for general CSV → annotation.

Service-agnostic: caller supplies the target annotation definition id and
optional tag ids. The client handles auth shell-out, JSONL ingest, dedup
readback, and per-chunk verification.
"""
from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta

import httpx

from .events import GenericEvent

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
                headers={"User-Agent": "fulcra-csv-importer/0.1"},
                follow_redirects=True,
            )
        return self._http

    def _authed_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.get_token()}"}

    def ensure_tag(self, name: str) -> str:
        c = self._client()
        r = c.get(f"/user/v1alpha1/tag/name/{name}", headers=self._authed_headers())
        if r.status_code == 200:
            return r.json()["id"]
        r = c.post(
            "/user/v1alpha1/tag",
            json={"name": name},
            headers=self._authed_headers(),
        )
        r.raise_for_status()
        return r.json()["id"]

    def ingest_batch(
        self,
        events: list[GenericEvent],
        *,
        definition_id: str,
        tag_id_for: dict[str, str] | None = None,
    ) -> None:
        if not events:
            return
        tag_id_for = tag_id_for or {}
        lines: list[bytes] = []
        for ev in events:
            tag_id = tag_id_for.get(ev.tag or "") if ev.tag else None
            tags = [tag_id] if tag_id else []
            data_inner = {
                "note": ev.note,
                "title": ev.title,
                "external_ids": ev.external_ids,
            }
            if ev.tag:
                data_inner["tag"] = ev.tag
            metadata = {
                "data_type": "DurationAnnotation",
                "recorded_at": {
                    "start_time": ev.start_time.isoformat().replace("+00:00", "Z"),
                    "end_time":   ev.end_time.isoformat().replace("+00:00", "Z"),
                },
                "tags": tags,
                "source": [ev.source_id, f"com.fulcradynamics.annotation.{definition_id}"],
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
        self,
        start: datetime,
        end: datetime,
        only_for_defs: set[str] | None = None,
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
        definition_id: str,
        tag_id_for: dict[str, str] | None = None,
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
        only_for_defs = {f"com.fulcradynamics.annotation.{definition_id}"}

        for i in range(0, len(events_sorted), chunk_size):
            chunk = events_sorted[i : i + chunk_size]
            win_start = min(e.start_time for e in chunk) - timedelta(minutes=window_pad_minutes)
            win_end = max(e.end_time for e in chunk) + timedelta(minutes=window_pad_minutes)

            existing = self.fetch_existing_source_ids(win_start, win_end, only_for_defs=only_for_defs)
            new_events = [e for e in chunk if e.source_id not in existing]
            skipped += len(chunk) - len(new_events)

            if new_events:
                self.ingest_batch(new_events, definition_id=definition_id, tag_id_for=tag_id_for)
                posted += len(new_events)
                after = self.fetch_existing_source_ids(
                    win_start, win_end, only_for_defs=only_for_defs
                )
                verified += sum(1 for e in new_events if e.source_id in after)

        return ImportResult(total=total, skipped_existing=skipped, posted=posted, verified=verified)
