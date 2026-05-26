"""Base Fulcra REST API client.

Every fulcra-tools package talks to the same Fulcra API the same way:
the same auth (a bearer token from the `fulcra` CLI or an env var), the
same httpx client, the same tag-lookup / soft-delete / event-readback
calls. That shared core lives here. Each package subclasses
`BaseFulcraClient` and adds its own definition/ingest logic on top.
"""
from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import httpx

DEFAULT_BASE_URL = os.environ.get("FULCRA_API_BASE", "https://api.fulcradynamics.com")


@dataclass
class ImportResult:
    """Outcome of an import run — how many events were seen, skipped as
    already-present, posted, and confirmed present on readback."""

    total: int
    skipped_existing: int
    posted: int
    verified: int


class BaseFulcraClient:
    """Shared Fulcra API client. Subclass it; do not instantiate directly.

    Subclasses may override `USER_AGENT` and `FOLLOW_REDIRECTS`.
    """

    #: Sent as the User-Agent header. Subclasses override with their own name.
    USER_AGENT = "fulcra-tools/0.1"
    #: Whether the httpx client follows 3xx redirects. The Fulcra tag-name
    #: lookup answers 303 for some names, so clients that resolve tags via
    #: an unencoded path need this True. Clients that percent-encode the
    #: name (and would rather not let a per-request Authorization header
    #: ride a redirect to another host) set it False.
    FOLLOW_REDIRECTS = True

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url
        self._transport = transport
        self._http: httpx.Client | None = None

    def get_token(self) -> str:
        """Return a bearer token: the FULCRA_ACCESS_TOKEN env var if set,
        otherwise the output of `fulcra auth print-access-token`."""
        env = os.environ.get("FULCRA_ACCESS_TOKEN")
        if env:
            return env
        # A launchd/systemd-managed process inherits a minimal PATH that
        # excludes the venv bin dir, so look for `fulcra` next to the
        # running interpreter before falling back to PATH.
        sibling = Path(sys.executable).parent / "fulcra"
        fulcra_cmd = str(sibling) if sibling.exists() else "fulcra"
        try:
            result = subprocess.run(
                [fulcra_cmd, "auth", "print-access-token"],
                check=True,
                capture_output=True,
                timeout=30,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                "fulcra auth print-access-token timed out after 30s; the Fulcra "
                "CLI may be stuck on an interactive re-auth flow. Run "
                "`fulcra auth login`, or set FULCRA_ACCESS_TOKEN."
            ) from exc
        except FileNotFoundError as exc:
            raise RuntimeError(
                "fulcra CLI not found; install it or set FULCRA_ACCESS_TOKEN."
            ) from exc
        except subprocess.CalledProcessError as exc:
            # Truncate stderr so a future CLI change can't spill a long
            # message (possibly carrying a credential) into logs.
            stderr = (getattr(exc, "stderr", b"") or b"")[:200]
            raise RuntimeError(
                "fulcra auth print-access-token failed; run `fulcra auth login` first. "
                f"stderr={stderr!r}"
            ) from exc
        return result.stdout.decode().strip()

    def _client(self) -> httpx.Client:
        if self._http is None:
            self._http = httpx.Client(
                base_url=self.base_url,
                transport=self._transport,
                timeout=30.0,
                headers={"User-Agent": self.USER_AGENT},
                follow_redirects=self.FOLLOW_REDIRECTS,
            )
        return self._http

    def _authed_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.get_token()}"}

    def _resolve_tag(self, name: str, *, quote_name: bool = False) -> str:
        """Return the id of the tag called `name`, creating it if absent.

        `quote_name` percent-encodes the name in the lookup path — needed
        for names with `/`, `?`, `#`, or spaces. The POST body always uses
        the raw name.
        """
        path_name = quote(name, safe="") if quote_name else name
        c = self._client()
        r = c.get(
            f"/user/v1alpha1/tag/name/{path_name}",
            headers=self._authed_headers(),
        )
        if r.status_code == 200:
            return r.json()["id"]
        r = c.post(
            "/user/v1alpha1/tag",
            json={"name": name},
            headers=self._authed_headers(),
        )
        r.raise_for_status()
        return r.json()["id"]

    def definition_exists(self, definition_id: str) -> bool:
        """Return True iff `definition_id` is a live (non-deleted)
        annotation definition on the current Fulcra account.

        Used by `ctx.resolved_definition_id` / `ctx.ensure_definition` to
        detect when a cached def id points at a def in a different
        account or a soft-deleted def — the same hazard that left
        attention events orphaned after the daemon was re-authed to a
        different account (see task #12 history). Without this check,
        Fulcra's ingest endpoint accepts events with a source_id whose
        def doesn't exist and they're silently invisible in the timeline.

        Network failure → returns True (conservative: assume the def is
        fine, retry on the next validation window). A flaky API should
        never trigger spurious re-resolutions.
        """
        try:
            r = self._client().get(
                "/user/v1alpha1/annotation",
                headers=self._authed_headers(),
            )
            r.raise_for_status()
            for d in r.json():
                if d.get("id") == definition_id and not d.get("deleted_at"):
                    return True
            return False
        except Exception:
            return True

    def soft_delete_definition(self, definition_id: str) -> bool:
        """Soft-delete an annotation definition.

        Returns True on a 204, False on a 404. Events under the def are
        NOT removed from query results — they stay visible but their
        source_id points at a deleted def. This is the only delete
        primitive Fulcra exposes; there is no per-event delete.
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

    def fetch_records(
        self,
        start: datetime,
        end: datetime,
        *,
        data_type: str = "DurationAnnotation",
    ) -> list[dict]:
        """Return raw records of `data_type` over [start, end].

        The Fulcra endpoint returns either a plain list or `{"data": [...]}`;
        both shapes are normalised to a list here.
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

        `only_for_defs`: when set, restrict to records whose top-level
        `source_id` is in this set. This is the dedup-vs-orphan story for
        user-defined annotations — events orphaned by a soft-deleted def
        still surface, but their source_id points at the deleted def. Pass
        None for built-in data types, which have no definition to filter on.
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
