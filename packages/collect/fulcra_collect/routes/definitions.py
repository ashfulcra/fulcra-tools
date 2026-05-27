"""Definitions — list, preview recent entries, bind / clear, delete.

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


def register(app: FastAPI, ctx: RouteContext) -> None:
    daemon = ctx.daemon
    require_token = ctx.require_token
    fulcra_token_or_401 = ctx.fulcra_token_or_401
    fulcra_http_client = ctx.fulcra_http_client

    @app.get("/api/definitions", dependencies=[Depends(require_token)])
    def list_definitions(annotation_type: str | None = None):  # noqa: ARG001
        """List all non-deleted Fulcra annotation definitions.

        The annotation_type query parameter is accepted for backwards
        compatibility but ignored — all definitions are returned so the
        frontend can group compatible vs. other types itself.

        Calls the Fulcra API directly with the user-level bearer token.
        """
        from .. import web as _web  # late import — tests monkeypatch web.httpx

        _log = logging.getLogger("fulcra_collect.web")
        fulcra_token = fulcra_token_or_401()
        try:
            with fulcra_http_client(fulcra_token) as client:
                r = client.get("/user/v1alpha1/annotation")
                r.raise_for_status()
                defs = r.json()
        except HTTPException:
            raise
        except _web.httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            _log.warning("list_definitions: Fulcra returned %s", status)
            if status in (401, 403):
                raise HTTPException(
                    401,
                    "Fulcra rejected the request — your sign-in may have expired. "
                    "Re-run sign-in from the wizard or paste a fresh token.",
                ) from exc
            if 500 <= status < 600:
                raise HTTPException(
                    502,
                    f"Fulcra returned {status}. Try again in a moment.",
                ) from exc
            raise HTTPException(
                502,
                f"Fulcra returned an unexpected {status}.",
            ) from exc
        except (_web.httpx.ConnectError, _web.httpx.ConnectTimeout) as exc:
            _log.warning("list_definitions: connect failed: %r", exc)
            raise HTTPException(
                502,
                "Couldn't reach Fulcra. Check your internet, then try again.",
            ) from exc
        except _web.httpx.TimeoutException as exc:
            _log.warning("list_definitions: timed out: %r", exc)
            raise HTTPException(
                504,
                "Fulcra took too long to respond. Try again in a moment.",
            ) from exc
        except Exception as exc:
            _log.exception("list_definitions: unexpected failure")
            raise HTTPException(
                502,
                f"Fulcra request failed unexpectedly ({type(exc).__name__}). "
                "Check the daemon log for details.",
            ) from exc
        # Filter out soft-deleted definitions; annotation_type filtering is
        # intentionally NOT applied — the frontend groups types itself.
        defs = [d for d in defs if not d.get("deleted_at")]
        return {"definitions": defs}

    @app.get("/api/definitions/{def_id}/recent", dependencies=[Depends(require_token)])
    def definition_recent(def_id: str, limit: int = 5):
        """Return the last N annotations from a Fulcra definition for
        preview in the definition-picker UI.

        Uses the DurationAnnotation data type by default; the response
        contains raw event records from the Fulcra API. limit must be 1-20.
        """
        from .. import web as _web  # late import — tests monkeypatch web.httpx

        if limit < 1 or limit > 20:
            raise HTTPException(400, "limit must be 1-20")
        fulcra_token = fulcra_token_or_401()
        try:
            from datetime import datetime, timezone, timedelta
            now = datetime.now(timezone.utc)
            # Look back 1 year as a practical window for "recent" entries.
            start = now - timedelta(days=365)
            with fulcra_http_client(fulcra_token) as client:
                # Try DurationAnnotation first, fall back to MomentAnnotation
                # if the definition has no duration events.
                entries: list[dict] = []
                for data_type in ("DurationAnnotation", "MomentAnnotation"):
                    r = client.get(
                        f"/data/v1alpha1/event/{data_type}",
                        params={
                            "start_time": start.isoformat().replace("+00:00", "Z"),
                            "end_time": now.isoformat().replace("+00:00", "Z"),
                        },
                    )
                    r.raise_for_status()
                    body = r.json()
                    records = body if isinstance(body, list) else body.get("data", []) or []
                    # Filter to only events belonging to this definition
                    def_source = f"com.fulcradynamics.annotation.{def_id}"
                    matched = [
                        rec for rec in records
                        if def_source in ((rec.get("metadata") or {}).get("source") or [])
                        or rec.get("source_id") == def_source
                    ]
                    entries.extend(matched)
                    if entries:
                        break
        except HTTPException:
            raise
        except _web.httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            _log = logging.getLogger("fulcra_collect.web")
            _log.warning("definition_recent(%s): Fulcra returned %s",
                          def_id, status)
            if status in (401, 403):
                raise HTTPException(
                    401,
                    "Fulcra rejected the request — your sign-in may have "
                    "expired. Re-run sign-in from the wizard or paste a "
                    "fresh token.",
                ) from exc
            if 500 <= status < 600:
                raise HTTPException(
                    502,
                    f"Fulcra returned {status}. Try again in a moment.",
                ) from exc
            raise HTTPException(
                502, f"Fulcra returned an unexpected {status}.",
            ) from exc
        except (_web.httpx.ConnectError, _web.httpx.ConnectTimeout) as exc:
            logging.getLogger("fulcra_collect.web").warning(
                "definition_recent(%s): connect failed: %r", def_id, exc,
            )
            raise HTTPException(
                502,
                "Couldn't reach Fulcra. Check your internet, then try again.",
            ) from exc
        except _web.httpx.TimeoutException as exc:
            logging.getLogger("fulcra_collect.web").warning(
                "definition_recent(%s): timed out: %r", def_id, exc,
            )
            raise HTTPException(
                504,
                "Fulcra took too long to respond. Try again in a moment.",
            ) from exc
        except Exception as exc:
            logging.getLogger("fulcra_collect.web").exception(
                "definition_recent(%s): unexpected failure", def_id,
            )
            raise HTTPException(
                502,
                f"Fulcra request failed unexpectedly ({type(exc).__name__}). "
                "Check the daemon log for details.",
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

        Returns 200 with {"ok": True} on success, 404 if the def doesn't
        exist (or was already soft-deleted server-side). Events written
        under the def remain in Fulcra but the def no longer appears in
        the definitions list and any plugin caching it gets its cached
        definition_id cleared so the next run resolves a fresh one
        instead of trying to write to a dangling reference.
        """
        from .. import web as _web  # late import — tests monkeypatch web.httpx

        _log = logging.getLogger("fulcra_collect.web")
        fulcra_token = fulcra_token_or_401()
        # Talk to Fulcra via httpx the same way /api/definitions does —
        # the BaseFulcraClient.soft_delete_definition primitive expects
        # its own auth path, and we want to share error-handling +
        # connection setup with the existing list/recent routes.
        try:
            with fulcra_http_client(fulcra_token) as client:
                r = client.delete(f"/user/v1alpha1/annotation/{def_id}")
                if r.status_code == 404:
                    raise HTTPException(
                        404,
                        "Definition not found — it may have already been deleted.",
                    )
                if r.status_code != 204:
                    r.raise_for_status()
        except HTTPException:
            raise
        except _web.httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            _log.warning("delete_definition(%s): Fulcra returned %s", def_id, status)
            if status in (401, 403):
                raise HTTPException(
                    401,
                    "Fulcra rejected the request — your sign-in may have expired. "
                    "Re-run sign-in from the wizard or paste a fresh token.",
                ) from exc
            if 500 <= status < 600:
                raise HTTPException(
                    502,
                    f"Fulcra returned {status}. Try again in a moment.",
                ) from exc
            raise HTTPException(
                502,
                f"Fulcra returned an unexpected {status}.",
            ) from exc
        except (_web.httpx.ConnectError, _web.httpx.ConnectTimeout) as exc:
            _log.warning("delete_definition(%s): connect failed: %r", def_id, exc)
            raise HTTPException(
                502,
                "Couldn't reach Fulcra. Check your internet, then try again.",
            ) from exc
        except _web.httpx.TimeoutException as exc:
            _log.warning("delete_definition(%s): timed out: %r", def_id, exc)
            raise HTTPException(
                504,
                "Fulcra took too long to respond. Try again in a moment.",
            ) from exc
        except Exception as exc:
            _log.exception("delete_definition(%s): unexpected failure", def_id)
            raise HTTPException(
                502,
                f"Fulcra request failed unexpectedly ({type(exc).__name__}). "
                "Check the daemon log for details.",
            ) from exc

        # Clear the cached definition_id on any plugin that was bound to
        # the deleted def. Without this the next run would try to write
        # to a tombstoned def and either silently fail or re-create a new
        # def on the side (depending on the plugin's error path).
        from .. import state as _state_mod
        cleared: list[str] = []
        for p in daemon.registry.plugins.values():
            try:
                st = _state_mod.load(p.id)
            except Exception:
                # Per-plugin state corruption shouldn't abort the delete.
                continue
            if getattr(st, "definition_id", None) == def_id:
                st.definition_id = None
                _state_mod.save(st)
                cleared.append(p.id)
        if cleared:
            _log.info(
                "delete_definition(%s): cleared cached definition_id on %d plugin(s): %s",
                def_id, len(cleared), ", ".join(cleared),
            )
        # Also drop this def from quick-record favorites if it was pinned.
        # Without this the favorites file would accumulate orphan UUIDs
        # the menubar would keep trying to surface but Fulcra would no
        # longer return. Best-effort: a favorites I/O failure shouldn't
        # roll back the (successful) Fulcra-side delete.
        try:
            from .. import quick_record_favorites as _favs
            current = _favs.load()
            if def_id in current:
                current.discard(def_id)
                _favs.save(current)
                # Bust the daemon's quick-record cache so the next list
                # call doesn't briefly resurrect the deleted def with a
                # stale ``pinned`` flag.
                daemon._quick_record_cache = None
                _log.info(
                    "delete_definition(%s): removed from quick-record favorites",
                    def_id,
                )
        except Exception:
            _log.exception(
                "delete_definition(%s): could not prune favorites; non-fatal",
                def_id,
            )
        return {"ok": True, "cleared_plugins": cleared}

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
