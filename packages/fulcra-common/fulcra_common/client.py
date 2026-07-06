"""Base Fulcra REST API client.

Every fulcra-tools package talks to the same Fulcra API the same way:
the same auth (a bearer token from the `fulcra` CLI or an env var), the
same httpx client, the same tag-lookup / soft-delete / event-readback
calls. That shared core lives here. Each package subclasses
`BaseFulcraClient` and adds its own definition/ingest logic on top.
"""
from __future__ import annotations

import os
import shutil
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


def find_fulcra_cli() -> str | None:
    """Resolve the `fulcra` CLI binary, launchd-proof.

    A launchd/systemd-managed process inherits a minimal PATH
    (`/usr/bin:/bin:/usr/sbin:/sbin`) that misses every common install
    location for a user-installed Python CLI, so bare
    ``shutil.which("fulcra")`` fails exactly where the daemon runs (live
    failure 2026-06-10: every worker-side ingest died with "fulcra CLI not
    found" until the binary was hand-symlinked into the daemon venv).

    Resolution order:
      1. next to the running interpreter (a venv-local install wins),
      2. PATH (terminal-launched processes, manual installs),
      3. well-known locations the launchd PATH misses — ``~/.local/bin``
         (uv tool install), ``/opt/homebrew/bin`` (Apple Silicon brew),
         ``/usr/local/bin`` (Intel brew + general).

    This mirrors (and supersedes) collect's ``credentials._find_fulcra_cli``,
    which only did steps 2–3; it lives here so every fulcra-tools package
    that shells out to the CLI shares one resolver. Returns the absolute
    path, or None when the CLI is nowhere.
    """
    sibling = Path(sys.executable).parent / "fulcra"
    if sibling.is_file() and os.access(sibling, os.X_OK):
        return str(sibling)
    found = shutil.which("fulcra")
    if found:
        return found
    for candidate in (
        os.path.expanduser("~/.local/bin/fulcra"),
        "/opt/homebrew/bin/fulcra",
        "/usr/local/bin/fulcra",
    ):
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


#: The only definition fields that may change through update_definition /
#: the collect daemon's _update_definition. annotation_type changes would
#: re-type every existing event; measurement_spec / spec changes silently
#: re-scale historical data — both are different, dangerous operations.
DEFINITION_UPDATABLE_FIELDS = frozenset({"name", "description", "tags"})

#: The keys of the PUT /user/v1alpha1/annotation/{id} discriminated-union
#: body (AnnotationRoot: moment/duration/boolean/numeric/people/scale).
#: The GET record carries more (created_at, fulcra_userid, …) — those are
#: record-only and must NOT be echoed back into the PUT body.
_DEFINITION_PUT_BODY_FIELDS = (
    "annotation_type",
    "name",
    "description",
    "tags",
    "measurement_spec",
    "spec",
)


def validate_definition_update(updates: dict) -> dict:
    """Validate a definition-update field dict; return the effective updates.

    Raises ValueError when `updates` names any non-updatable field (see
    DEFINITION_UPDATABLE_FIELDS), when no field is provided (None values
    mean "not provided"), or when a provided value has the wrong shape
    (empty/blank name, non-string description, non-list tags).

    Shared by `BaseFulcraClient.update_definition` and collect's
    `Daemon._update_definition` so the forbidden-field guard cannot drift
    between the two surfaces.
    """
    forbidden = set(updates) - DEFINITION_UPDATABLE_FIELDS
    if forbidden:
        raise ValueError(
            "definition update can only change name/description/tags; "
            f"refusing to change: {', '.join(sorted(forbidden))}"
        )
    effective = {k: v for k, v in updates.items() if v is not None}
    if not effective:
        raise ValueError(
            "empty update — provide at least one of name, description, tags"
        )
    name = effective.get("name")
    if name is not None and (not isinstance(name, str) or not name.strip()):
        raise ValueError("name must be a non-empty string")
    description = effective.get("description")
    if description is not None and not isinstance(description, str):
        raise ValueError("description must be a string")
    tags = effective.get("tags")
    if tags is not None and (
        not isinstance(tags, list)
        or not all(isinstance(t, str) for t in tags)
    ):
        raise ValueError("tags must be a list of strings")
    return effective


def merge_definition_update(current: dict, effective: dict) -> dict:
    """Build the FULL-REPLACE PUT body from the GET record + changed fields.

    The Fulcra PUT annotation endpoint takes a discriminated union
    (AnnotationRoot) where every member requires name+description+tags and
    the measured types (boolean/duration/numeric/scale) also require
    measurement_spec (scale additionally requires spec). It is NOT a patch:
    a partial body would null-out measurement_spec/spec and corrupt scale /
    numeric definitions. So the complete body is reconstructed here —
    union-body keys copied verbatim from `current` (including explicit
    nulls for moment/people measurement_spec), then only the validated
    `effective` fields overlaid. Record-only keys (created_at, id, …) are
    dropped: the union members don't accept them.
    """
    body = {k: current[k] for k in _DEFINITION_PUT_BODY_FIELDS if k in current}
    body.update(effective)
    return body


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
        fulcra_cmd = find_fulcra_cli()
        if fulcra_cmd is None:
            raise RuntimeError(
                "fulcra CLI not found; install it or set FULCRA_ACCESS_TOKEN."
            )
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
        their source_id points at a deleted def. There is no per-EVENT
        delete primitive; definition soft-delete is reversible via
        `restore_definition` (Fulcra's cancel_deletion endpoint).

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

    def restore_definition(self, definition_id: str) -> bool:
        """Restore (un-soft-delete) an annotation definition.

        Inverse of `soft_delete_definition` — calls Fulcra's
        `POST /user/v1alpha1/annotation/{id}/cancel_deletion` via the lib's
        `restore_annotation`. Returns True on success, False on a 404
        (unknown definition id).

        Any non-404 error from the lib is propagated to the caller.
        """
        import urllib.error as _ue

        try:
            self._lib().restore_annotation(definition_id)
            return True
        except _ue.HTTPError as exc:
            if exc.code == 404:
                return False
            raise

    def data_updates_summary(
        self,
        window_start: datetime,
        window_end: datetime,
    ) -> dict[str, int]:
        """Per-data-type processed-record counts over [window_start, window_end).

        GET /data/v1/updates (fulcra-api >= 0.1.35; the endpoint is absent
        from the published OpenAPI spec — this is coded against the lib's
        ``FulcraAPI.data_updates``, which sends the same ``start_time`` /
        ``end_time`` query params). The server's response has two keys:

        * ``data_types`` — {data type: number of records PROCESSED during
          the window}. PROCESSING time, not event time: a record ingested
          today with a 2020 event timestamp counts in TODAY's window and in
          no window around 2020 (verified live 2026-07-06 — 501 records
          with 2020-06 event times exist while data_updates over 2020-06
          returns ``{}``). Callers gating work on these counts must reason
          in processing time.
        * ``file_changes`` — a list of changed uploaded files that can run
          to MEGABYTES on accounts with coordination-bus churn (1,793
          entries in a single hour, live). It is dropped here, never
          returned, and must NEVER be logged or embedded in an error.

        Definition create/soft-delete/restore does NOT surface in
        ``data_types`` at all (verified live: an 11-definition soft-delete
        burst shows ``{}`` over its exact window), so this is NOT a signal
        for definition-cache staleness.

        On any HTTP error this RAISES (httpx.HTTPStatusError etc. — the
        server 500s on large windows, seen live with a 7-day range).
        Callers treat a raise as "can't gate; proceed without the
        optimization" — a gating failure must never block an import.
        """
        r = self._client().get(
            "/data/v1/updates",
            params={
                "start_time": window_start.isoformat().replace("+00:00", "Z"),
                "end_time": window_end.isoformat().replace("+00:00", "Z"),
            },
            headers=self._authed_headers(),
        )
        r.raise_for_status()
        data_types = r.json().get("data_types") or {}
        # Rebuild the dict so no reference to the parsed body (and its
        # file_changes sibling) escapes to callers.
        return {str(k): int(v) for k, v in data_types.items()}
    def update_definition(
        self,
        definition_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
        tags: list[str] | None = None,
        **forbidden: object,
    ) -> bool:
        """Rename/update an annotation definition IN PLACE — keeping all
        history under it (unlike delete-and-recreate, which orphans every
        event recorded against the old definition id).

        Fulcra's ``PUT /user/v1alpha1/annotation/{id}`` is a FULL-REPLACE
        over a discriminated union, so this GETs the current record first,
        merges only the changed fields, and PUTs the complete body back —
        ``measurement_spec`` / ``spec`` always ride through verbatim (see
        ``merge_definition_update``). The installed fulcra_api lib (0.1.35)
        has no update method, so both legs are raw httpx via ``_client()``.

        Only name/description/tags may change. Attempting anything else
        (``annotation_type``, ``measurement_spec``, ``spec``, …) raises
        ValueError before any HTTP request — a type or spec change re-types
        / re-scales existing events and is a different, dangerous
        operation. An all-None update also raises ValueError.

        Returns True on success, False on a 404 from either leg (unknown
        id, or deleted between the GET and the PUT). Any other HTTP error
        propagates as ``httpx.HTTPStatusError``, like the siblings above.
        """
        if forbidden:
            raise ValueError(
                "definition update can only change name/description/tags; "
                f"refusing to change: {', '.join(sorted(forbidden))}"
            )
        effective = validate_definition_update(
            {"name": name, "description": description, "tags": tags}
        )
        path = f"/user/v1alpha1/annotation/{definition_id}"
        r = self._client().get(path, headers=self._authed_headers())
        if r.status_code == 404:
            return False
        r.raise_for_status()
        body = merge_definition_update(r.json(), effective)
        r2 = self._client().put(path, json=body, headers=self._authed_headers())
        if r2.status_code == 404:
            return False
        # The spec declares a 307 success response for this PUT; the client
        # follows redirects, so anything below 400 that survives to here is
        # success. Only 4xx/5xx (minus the 404 above) raise.
        if r2.status_code >= 400:
            r2.raise_for_status()
        return True

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
