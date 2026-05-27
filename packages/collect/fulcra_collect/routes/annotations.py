"""Annotation write / delete + quick-record surface (definitions + favorites).

These endpoints back the menubar quick-record popover and any direct web-UI
callers that need to write a single annotation immediately to Fulcra.
"""
from __future__ import annotations

from fastapi import Depends, FastAPI

from ._deps import QuickRecordFavoritesBody, RecordAnnotationBody, RouteContext


def register(app: FastAPI, ctx: RouteContext) -> None:
    daemon = ctx.daemon
    require_token = ctx.require_token

    # ------------------------------------------------------------------
    # Quick-record surface — menubar popover Moment annotations
    # ------------------------------------------------------------------

    @app.get("/api/quick-record/definitions", dependencies=[Depends(require_token)])
    def get_quick_record_definitions():
        """Return the user's Moment annotation definitions for the menubar
        quick-record surface. Delegates to the daemon's quick_record_list
        handler which applies its own 60-second in-memory cache."""
        return daemon.handle_request({"cmd": "quick_record_list"})

    @app.post("/api/annotations", dependencies=[Depends(require_token)])
    def record_annotation(body: RecordAnnotationBody):
        """Write one annotation immediately to Fulcra. Used by the
        menubar quick-record buttons and any direct web UI callers.

        Writes a Moment when ``start_time`` / ``end_time`` are absent;
        writes a Duration when BOTH are present (Sprint B,
        2026-05-26 — see daemon._record_annotation for the type-aware
        wire shape).
        """
        return daemon.handle_request({
            "cmd": "record_annotation",
            "definition_id": body.definition_id,
            "comment": body.comment,
            "start_time": body.start_time,
            "end_time": body.end_time,
        })

    # ------------------------------------------------------------------
    # Quick-record favorites — the per-machine pin list (task #64).
    # Two surfaces share these endpoints: the menubar popover's per-row
    # star toggle (one-def diff) and the web UI Settings page's bulk
    # multi-select. Both PUT the full list each time; the daemon's
    # ``_set_quick_record_favorites`` busts the in-memory quick-record
    # cache so the very next list call reflects the new order.
    # ------------------------------------------------------------------

    @app.get("/api/quick-record/favorites",
             dependencies=[Depends(require_token)])
    def get_quick_record_favorites():
        return daemon.handle_request({"cmd": "get_quick_record_favorites"})

    @app.put("/api/quick-record/favorites",
             dependencies=[Depends(require_token)])
    def put_quick_record_favorites(body: QuickRecordFavoritesBody):
        return daemon.handle_request({
            "cmd": "set_quick_record_favorites",
            "favorites": body.favorites,
        })

    @app.delete("/api/annotations/{source_id}",
                dependencies=[Depends(require_token)])
    def delete_annotation(source_id: str):
        """Soft-delete an annotation by writing a tombstone marker.

        Caveat: Fulcra has no per-event delete primitive. This endpoint
        writes a separate sentinel annotation referencing ``source_id``
        in its data payload — the menubar uses it for the "Undo"
        affordance on the Recently-recorded list. The original record
        remains visible on the user's Fulcra timeline.
        See ``daemon._delete_annotation`` for the full rationale and
        ``packages/media-helpers/scripts/probe_soft_delete_3.py`` for
        the survey of why this is the best we can do.
        """
        return daemon.handle_request({
            "cmd": "delete_annotation",
            "source_id": source_id,
        })
