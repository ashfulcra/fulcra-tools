"""Fulcra API client for fulcra-attention.

Single point of contact with the Fulcra REST API. Mirrors
fulcra-media/fulcra.py's shape: subprocess-shell-out auth, ensure_tag,
ensure_definitions, ingest_batch. Different annotation type (just
Attention) so we keep it standalone rather than importing from fulcra-media.
"""
from __future__ import annotations

import os
import subprocess

import httpx

from .state import State

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
                headers={"User-Agent": "fulcra-attention/0.1"},
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
        if state.attention_definition_id:
            return
        attention = self.ensure_tag("attention", state)
        web = self.ensure_tag("web", state)
        body = {
            "annotation_type": "duration",
            "name": "Attention",
            "description": "What the user paid attention to (browsing).",
            "tags": [attention, web],
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
        state.attention_definition_id = r.json()["id"]

    def ingest_batch(self, events: list[dict]) -> None:
        """POST a JSONL batch of already-built events to /ingest/v1/record/batch.

        Each event must be a dict with `specversion`, `data`, `metadata` keys
        (the wire format documented in the spec). Source-id idempotency is the
        caller's responsibility — building the deterministic source-id lives
        in ingest.py.
        """
        import json as _json  # local to avoid shadowing
        if not events:
            return
        lines = [_json.dumps(e, sort_keys=True).encode() for e in events]
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
