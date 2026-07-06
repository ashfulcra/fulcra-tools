"""Definitions — list, preview recent entries, bind / clear, delete, restore, update.

This module talks to the Fulcra HTTP API via the shared http-client
factory on the :class:`RouteContext`. The factory pulls ``httpx`` from
:mod:`fulcra_collect.web` (late-imported each call) so existing tests
that monkeypatch ``fulcra_collect.web.httpx`` continue to override the
client used here.
"""
from __future__ import annotations

import logging

from fastapi import Depends, FastAPI, HTTPException

from ._deps import DefinitionBindBody, RouteContext

#: Maps the machine-readable ``code`` field on daemon-method error returns
#: (``_delete_definition`` / ``_restore_definition``) to the HTTP status the
#: route surfaces. Shared so the two shim routes can't drift.
_DAEMON_CODE_TO_HTTP = {
    "bad_request": 400,
    "unauthorized": 401,
    "not_found": 404,
    "timeout": 504,
    "upstream_error": 502,
}


def _fulcra_error_to_http(exc: Exception, op: str) -> HTTPException:
    """Translate a failed direct-to-Fulcra call into an ``HTTPException``.

    Single home for the five-way ladder previously duplicated verbatim in
    ``list_definitions`` and ``definition_recent`` — same statuses, same
    user-facing messages, same log lines (parameterised by ``op``).
    ``httpx`` is reached via :mod:`fulcra_collect.web` so tests that
    monkeypatch ``fulcra_collect.web.httpx`` keep working. Callers use it
    as ``raise _fulcra_error_to_http(exc, "op") from exc`` inside a broad
    ``except Exception`` (after re-raising ``HTTPException`` untouched).

    isinstance order matters: ``ConnectTimeout`` subclasses
    ``TimeoutException``, so the connect-failure pair is checked first —
    matching the original except-clause ordering.
    """
    from .. import web as _web  # late import — tests monkeypatch web.httpx

    _log = logging.getLogger("fulcra_collect.web")
    if isinstance(exc, _web.httpx.HTTPStatusError):
        status = exc.response.status_code
        _log.warning("%s: Fulcra returned %s", op, status)
        if status in (401, 403):
            return HTTPException(
                401,
                "Fulcra rejected the request — your sign-in may have expired. "
                "Re-run sign-in from the wizard or paste a fresh token.",
            )
        if 500 <= status < 600:
            return HTTPException(
                502,
                f"Fulcra returned {status}. Try again in a moment.",
            )
        return HTTPException(
            502,
            f"Fulcra returned an unexpected {status}.",
        )
    if isinstance(exc, (_web.httpx.ConnectError, _web.httpx.ConnectTimeout)):
        _log.warning("%s: connect failed: %r", op, exc)
        return HTTPException(
            502,
            "Couldn't reach Fulcra. Check your internet, then try again.",
        )
    if isinstance(exc, _web.httpx.TimeoutException):
        _log.warning("%s: timed out: %r", op, exc)
        return HTTPException(
            504,
            "Fulcra took too long to respond. Try again in a moment.",
        )
    _log.exception("%s: unexpected failure", op)
    return HTTPException(
        502,
        f"Fulcra request failed unexpectedly ({type(exc).__name__}). "
        "Check the daemon log for details.",
    )


def register(app: FastAPI, ctx: RouteContext) -> None:
    daemon = ctx.daemon
    require_token = ctx.require_token
    fulcra_token_or_401 = ctx.fulcra_token_or_401
    fulcra_http_client = ctx.fulcra_http_client

    @app.get("/api/definitions", dependencies=[Depends(require_token)])
    def list_definitions(
        annotation_type: str | None = None,  # noqa: ARG001
        include_deleted: bool = False,
    ):
        """List Fulcra annotation definitions (non-deleted by default).

        The annotation_type query parameter is accepted for backwards
        compatibility but ignored — all definitions are returned so the
        frontend can group compatible vs. other types itself.

        ``include_deleted=true`` also returns soft-deleted definitions
        (identifiable by a non-null ``deleted_at``) so the UI can offer a
        restore affordance backed by ``POST /api/definitions/{id}/restore``.

        Calls the Fulcra API directly with the user-level bearer token.
        """
        fulcra_token = fulcra_token_or_401()
        try:
            with fulcra_http_client(fulcra_token) as client:
                r = client.get("/user/v1alpha1/annotation")
                r.raise_for_status()
                defs = r.json()
        except HTTPException:
            raise
        except Exception as exc:
            raise _fulcra_error_to_http(exc, "list_definitions") from exc
        # Filter out soft-deleted definitions unless the caller asked for
        # them; annotation_type filtering is intentionally NOT applied —
        # the frontend groups types itself.
        if not include_deleted:
            defs = [d for d in defs if not d.get("deleted_at")]
        return {"definitions": defs}

    @app.get("/api/definitions/{def_id}/recent", dependencies=[Depends(require_token)])
    def definition_recent(def_id: str, limit: int = 5):
        """Return the last N annotations from a Fulcra definition for
        preview in the definition-picker UI.

        The def's ``annotation_type`` (from the definitions listing)
        selects which event data type to query — a moment def never pays
        a DurationAnnotation fetch. If the def isn't in the listing (or
        its type doesn't map), fall back to trying Duration then Moment.

        Each fetch pushes the work server-side: ``sort=desc`` plus a
        ``filter=source:...`` for this definition (both documented on
        GET /data/v1alpha1/event/{data_type}), over a short window that
        widens 7d → 30d → 365d and stops at the first hit — instead of
        the old "fetch a full year of ALL events, filter client-side"
        per preview click. The response contains raw event records from
        the Fulcra API. limit must be 1-20.
        """
        if limit < 1 or limit > 20:
            raise HTTPException(400, "limit must be 1-20")
        fulcra_token = fulcra_token_or_401()
        _TYPE_TO_DATA_TYPE = {
            "moment": "MomentAnnotation",
            "duration": "DurationAnnotation",
        }
        def_source = f"com.fulcradynamics.annotation.{def_id}"
        try:
            from datetime import datetime, timezone, timedelta
            now = datetime.now(timezone.utc)
            with fulcra_http_client(fulcra_token) as client:
                # Learn the def's annotation_type from the (cheap) defs
                # listing so we query only the matching event data type.
                data_types = ("DurationAnnotation", "MomentAnnotation")
                r = client.get("/user/v1alpha1/annotation")
                r.raise_for_status()
                for d in r.json():
                    if d.get("id") == def_id:
                        mapped = _TYPE_TO_DATA_TYPE.get(d.get("annotation_type"))
                        if mapped:
                            data_types = (mapped,)
                        break
                entries: list[dict] = []
                for data_type in data_types:
                    for days in (7, 30, 365):
                        start = now - timedelta(days=days)
                        r = client.get(
                            f"/data/v1alpha1/event/{data_type}",
                            params={
                                "start_time": start.isoformat().replace("+00:00", "Z"),
                                "end_time": now.isoformat().replace("+00:00", "Z"),
                                "sort": "desc",
                                "filter": [f"source:{def_source}"],
                            },
                        )
                        r.raise_for_status()
                        body = r.json()
                        records = body if isinstance(body, list) else body.get("data", []) or []
                        # Belt-and-braces: the server already filtered by
                        # source, but keep the original client-side match
                        # so a permissive upstream can't change results.
                        matched = [
                            rec for rec in records
                            if def_source in ((rec.get("metadata") or {}).get("source") or [])
                            or rec.get("source_id") == def_source
                        ]
                        if matched:
                            entries = matched
                            break
                    if entries:
                        break
        except HTTPException:
            raise
        except Exception as exc:
            raise _fulcra_error_to_http(
                exc, f"definition_recent({def_id})",
            ) from exc
        # Sort by recorded_at descending and return the most recent `limit`
        def _sort_key(rec: dict) -> str:
            rat = (rec.get("metadata") or {}).get("recorded_at") or ""
            if isinstance(rat, dict):
                return rat.get("end_time") or rat.get("start_time") or ""
            return str(rat)
        entries.sort(key=_sort_key, reverse=True)
        return {"entries": entries[:limit]}

    @app.post("/api/plugin/{plugin_id}/definition", dependencies=[Depends(require_token)])
    def bind_definition(plugin_id: str, body: DefinitionBindBody):
        """Bind a plugin to a chosen Fulcra definition id, or clear the cached
        id so the next run force-resolves a new one.

        Body: {"definition_id": "<uuid>"} to pick an existing definition, or
        {"force_new": true} to clear the cache and let the next run create a
        fresh definition. If {"force_new": true, "new_name": "My Watched"} is
        sent, that exact name is persisted on plugin state and used verbatim
        by the resolver instead of the plugin's canonical_definition_name —
        no machine-id suffix is appended. Empty or whitespace-only new_name
        is ignored (falls back to the canonical-name + suffix behavior).

        Implementation: path A (state-carries-name). We persist the override
        on PluginState; the next run's RunContext.resolved_definition_id
        consumes and clears it. This avoids needing a Fulcra client at
        request time and keeps the create deferred to the worker's normal
        error-handling path.
        """
        if plugin_id not in daemon.registry.plugins:
            raise HTTPException(404, f"unknown plugin {plugin_id!r}")
        if not body.definition_id and not body.force_new:
            raise HTTPException(400, "body must include definition_id or force_new=true")
        from .. import state as _state_mod
        st = _state_mod.load(plugin_id)
        if body.force_new:
            # Clear the cached definition_id; the plugin's next run will
            # call resolve_definition_id with force_new=True via RunContext.
            st.definition_id = None
            override = (body.new_name or "").strip()
            st.override_definition_name = override or None
        else:
            st.definition_id = body.definition_id
            # Picking an existing def supersedes any pending override name.
            st.override_definition_name = None
        _state_mod.save(st)
        return {"ok": True}

    @app.delete("/api/definitions/{def_id}", dependencies=[Depends(require_token)])
    def delete_definition_route(def_id: str):
        """Soft-delete a Fulcra annotation definition (task #42).

        HTTP shim over :meth:`Daemon._delete_definition` — the business
        logic moved to ``daemon.py`` in SP2 task 1 so the menubar can
        call the same code path via UDS. Returns the same shape as
        before; translates ``{"ok": False, "error": ...}`` returns back
        into ``HTTPException`` so the HTTP API contract is unchanged.

        The route still performs the 401 check upfront (rather than
        relying on the daemon method's "not signed in" return) so the
        HTTP surface keeps returning 401 with the original
        ``HTTPException`` message for callers that depend on it. The
        daemon method's identical "not signed in" return is reserved
        for UDS callers (menubar) that bypass HTTP auth entirely.
        """
        # Reuse the existing 401 path so the HTTP response carries the
        # original "set a bearer token first" wording. The daemon method
        # also re-checks the token (it can't trust the caller) — extra
        # work in the success path, but cheap and keeps both surfaces
        # self-contained.
        fulcra_token_or_401()
        result = daemon._delete_definition(def_id)
        if result.get("ok"):
            return result
        # Translate UDS error returns back into HTTPException for the
        # HTTP surface. The daemon returns a machine-readable `code`
        # field alongside `error` so the mapping is stable across
        # daemon-message wording tweaks (the previous string-sniff
        # implementation coupled HTTP status to error-text content).
        err = result.get("error", "delete failed")
        code = result.get("code", "upstream_error")
        raise HTTPException(_DAEMON_CODE_TO_HTTP.get(code, 502), err)

    @app.post(
        "/api/definitions/{def_id}/restore",
        dependencies=[Depends(require_token)],
    )
    def restore_definition_route(def_id: str):
        """Restore a soft-deleted Fulcra annotation definition — the undo
        for ``DELETE /api/definitions/{def_id}``.

        HTTP shim over :meth:`Daemon._restore_definition`, mirroring the
        delete route above: the upfront 401 check keeps the original
        "set a bearer token first" wording on the HTTP surface, and the
        daemon method's structured ``code`` returns are translated back
        into ``HTTPException`` via the shared map.

        Note the success payload's ``rebound: false`` — plugin bindings
        and quick-record favorites cleared by the delete are NOT
        re-established; the caller must re-bind in plugin settings.
        """
        fulcra_token_or_401()
        result = daemon._restore_definition(def_id)
        if result.get("ok"):
            return result
        err = result.get("error", "restore failed")
        code = result.get("code", "upstream_error")
        raise HTTPException(_DAEMON_CODE_TO_HTTP.get(code, 502), err)

    @app.put(
        "/api/definitions/{def_id}",
        dependencies=[Depends(require_token)],
    )
    def update_definition_route(def_id: str, body: dict | None = None):
        """Rename/update a Fulcra annotation definition IN PLACE (P2 #7).

        Body: any of ``{"name": ..., "description": ..., "tags": [...]}``.
        This is the history-preserving alternative to the force_new+new_name
        flow above — the definition id never changes, so every event and
        plugin binding under it stays attached. Empty payloads and attempts
        to change anything else (``annotation_type``, ``measurement_spec``,
        ``spec``, unknown keys) are rejected with 400 by the daemon method's
        validation (``code: bad_request`` → 400 via the shared map).

        HTTP shim over :meth:`Daemon._update_definition`, mirroring the
        delete/restore routes above: upfront 401 check for the original
        wording, structured ``code`` returns translated back into
        ``HTTPException`` via ``_DAEMON_CODE_TO_HTTP``.
        """
        fulcra_token_or_401()
        result = daemon._update_definition(def_id, body or {})
        if result.get("ok"):
            return result
        err = result.get("error", "update failed")
        code = result.get("code", "upstream_error")
        raise HTTPException(_DAEMON_CODE_TO_HTTP.get(code, 502), err)

    @app.delete("/api/plugin/{plugin_id}/definition", dependencies=[Depends(require_token)])
    def clear_definition(plugin_id: str):
        """Clear the plugin's cached definition_id. The next run will
        re-resolve (adopt an existing matching definition, or create one)."""
        if plugin_id not in daemon.registry.plugins:
            raise HTTPException(404, f"unknown plugin {plugin_id!r}")
        from .. import state as _state_mod
        st = _state_mod.load(plugin_id)
        st.definition_id = None
        _state_mod.save(st)
        return {"ok": True}
