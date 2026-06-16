"""Base Fulcra REST API client.

Every fulcra-tools package talks to the same Fulcra API the same way:
the same auth (a bearer token from the `fulcra` CLI or an env var), the
same httpx client, the same tag-lookup / soft-delete / event-readback
calls. That shared core lives here. Each package subclasses
`BaseFulcraClient` and adds its own definition/ingest logic on top.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import quote

import httpx

if TYPE_CHECKING:
    from fulcra_api.core import FulcraAPI

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
        self._lib_client: "FulcraAPI | None" = None

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

    def _lib(self) -> "FulcraAPI":
        """Lazily build and cache a FulcraAPI lib client from the current token.

        The lib client is reused across calls on this instance. It is built
        from the same bearer token source as `_authed_headers()` so auth
        stays consistent.

        We construct it from an explicit `FulcraCredentials` with a far-future
        expiration rather than via `FulcraAPI(access_token=...)`. That ctor path
        leaves `access_token_expiration=None`, so `credentials.is_expired()`
        returns True on the very first call, and `fulcra_api()` then tries to
        `refresh_access_token()` — which raises "No refresh token available"
        because we only ever hold a bare access token. `get_token()` already
        owns token freshness (env var or `fulcra auth print-access-token`), so
        we tell the lib the token never expires and never let it refresh.

        The expiration MUST be a naive datetime: `is_expired()` compares it
        against `datetime.now()` (naive), and a tz-aware value raises
        `TypeError: can't compare offset-naive and offset-aware datetimes`.
        """
        from fulcra_api.core import FulcraAPI
        from fulcra_api.credentials import FulcraCredentials

        if self._lib_client is None:
            creds = FulcraCredentials(
                access_token=self.get_token(),
                # Naive (no tzinfo) on purpose — see docstring.
                access_token_expiration=datetime.now() + timedelta(days=3650),
            )
            self._lib_client = FulcraAPI(credentials=creds)
        return self._lib_client

    def _resolve_tag(self, name: str, *, quote_name: bool = False) -> str:
        """Return the id of the tag called `name`, creating it if absent.

        Delegates to the `fulcra_api` lib: looks up the tag by name via
        `get_tag_by_name`; on not-found (HTTP 404 or missing id), creates
        it via `create_tag`.

        The lib does NOT percent-encode the name it interpolates into the
        lookup path (`get_tag_by_name` builds `/tag/name/{name}` verbatim and
        `urlunparse` passes it through unescaped). Real Agent-Tasks tag names
        contain colons (`agent:claude`, `session:Mac`) and other names may
        carry `/` or spaces — an unescaped space even makes urllib raise. So
        we percent-encode the name for the LOOKUP path here, exactly as the
        old httpx path did, and pass the RAW name to `create_tag` (whose body
        is JSON, where no URL-encoding is wanted).

        `quote_name` is accepted for backwards-compatibility. Encoding the
        lookup path is now unconditional (it's a no-op for names with no
        reserved chars and the safe behavior for those that do), so the flag
        has no effect; callers that set it continue to work unchanged.
        """
        lib = self._lib()
        # Encode the name for the GET lookup PATH; the lib won't do it for us.
        path_name = quote(name, safe="")
        try:
            tag = lib.get_tag_by_name(path_name)
            tag_id = tag.get("id") if isinstance(tag, dict) else None
            if tag_id:
                return tag_id
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise
            # 404 → tag not found; fall through to create
        # create_tag puts the name in the JSON body — use the RAW name.
        result = lib.create_tag(name)
        # The lib type hint says List[Dict]; the server may return a plain
        # dict or a list depending on the API version — handle both.
        if isinstance(result, list):
            return result[0]["id"]
        return result["id"]

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
            catalog = self._lib().annotations_catalog()
            for d in catalog:
                if d.get("id") == definition_id and not d.get("deleted_at"):
                    return True
            return False
        except Exception:
            return True

    def soft_delete_definition(self, definition_id: str) -> bool:
        """Soft-delete an annotation definition.

        Returns True on success, False on a 404 (not found). Events under
        the def are NOT removed from query results — they stay visible but
        their source_id points at a deleted def. This is the only delete
        primitive Fulcra exposes; there is no per-event delete.

        Any non-404 error from the lib is propagated to the caller.
        """
        import urllib.error as _ue

        try:
            self._lib().delete_annotation(definition_id)
            return True
        except _ue.HTTPError as exc:
            if exc.code == 404:
                return False
            raise

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

        Routes through the `fulcra_api` lib's generic `fulcra_v1_api`, which
        builds `/data/v1alpha1/event/{data_type}` and returns the raw response
        bytes. We pass the window as pre-formatted ISO strings (`...Z`) under
        the `start_time`/`end_time` param names: the lib urlencodes the params
        dict, so raw datetimes would serialise with a space instead of `T`/`Z`.
        """
        raw = self._lib().fulcra_v1_api(
            "event",
            data_type,
            {
                "start_time": start.isoformat().replace("+00:00", "Z"),
                "end_time": end.isoformat().replace("+00:00", "Z"),
            },
        )
        body = json.loads(raw)
        if isinstance(body, list):
            return body
        return body.get("data", []) or []

    def fetch_existing_source_groups(
        self,
        start: datetime,
        end: datetime,
        *,
        data_type: str = "DurationAnnotation",
        only_for_defs: set[str] | None = None,
    ) -> list[set[str]]:
        """Return one set of source-id strings PER RECORD in [start, end].

        Each set is the union of the record's top-level `sources` array and
        its `metadata.source` array. Keeping the per-record grouping (rather
        than flattening) lets callers reason about WHICH record carries a
        given source id — e.g. the same-source-replay vs cross-source-twin
        distinction in the media import pipeline needs to know whether the
        record that claims a content fingerprint also carries a source id
        from the same importer namespace. Records with no sources at all
        are omitted (they contribute nothing to dedup).

        `only_for_defs`: when set, restrict to records whose top-level
        `source_id` is in this set. This is the dedup-vs-orphan story for
        user-defined annotations — events orphaned by a soft-deleted def
        still surface, but their source_id points at the deleted def. Pass
        None for built-in data types, which have no definition to filter on.
        """
        records = self.fetch_records(start, end, data_type=data_type)
        groups: list[set[str]] = []
        for rec in records:
            if only_for_defs is not None and rec.get("source_id") not in only_for_defs:
                continue
            group: set[str] = set()
            for s in rec.get("sources") or []:
                group.add(s)
            for s in (rec.get("metadata") or {}).get("source") or []:
                group.add(s)
            if group:
                groups.append(group)
        return groups

    def fetch_existing_source_ids(
        self,
        start: datetime,
        end: datetime,
        *,
        data_type: str = "DurationAnnotation",
        only_for_defs: set[str] | None = None,
    ) -> set[str]:
        """Return the FLAT set of source-id strings present in [start, end].

        Thin union over `fetch_existing_source_groups` — same fetch, same
        `only_for_defs` filtering, just without the per-record grouping.
        """
        out: set[str] = set()
        for group in self.fetch_existing_source_groups(
            start, end, data_type=data_type, only_for_defs=only_for_defs
        ):
            out |= group
        return out
