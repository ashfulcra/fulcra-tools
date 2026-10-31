"""Scoped protocol2 quick record and authenticated obsolete-client refusal."""
from __future__ import annotations

from fastapi import Depends, FastAPI
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt

from ._deps import QuickRecordFavoritesBody, RecordAnnotationBody, RouteContext


class QuickRecordV2Body(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation: str = Field(min_length=1, max_length=64)
    protocol_version: StrictInt | None = None
    account_id: str | None = Field(default=None, max_length=36)
    config_epoch: str | None = Field(default=None, max_length=256)
    action_id: str | None = Field(default=None, max_length=36)
    record_action_id: str | None = Field(default=None, max_length=36)
    definition_id: str | None = Field(default=None, max_length=36)
    choice_id: str | None = Field(default=None, max_length=36)
    choice_digest: str | None = Field(default=None, max_length=64)
    start_time: str | None = Field(default=None, max_length=128)
    end_time: str | None = Field(default=None, max_length=128)
    comment: str | None = Field(default=None, max_length=16384)
    timer_id: str | None = Field(default=None, max_length=36)
    pinned: StrictBool | None = None
    expected_revision: StrictInt | None = None
    action_kind: str | None = Field(default=None, max_length=16)
    statuses: list[str] | None = Field(default=None, max_length=5)
    limit: StrictInt | None = None
    cursor: str | None = Field(default=None, max_length=2048)
    include_finished: StrictBool | None = None


def register(app: FastAPI, ctx: RouteContext) -> None:
    daemon = ctx.daemon
    require_token = ctx.require_token

    @app.get("/api/quick-record/v2", dependencies=[Depends(require_token)])
    def quick_record_v2_handshake():
        return daemon.handle_request({"cmd": "quick_record_v2", "operation": "quick_record_list"})

    @app.post("/api/quick-record/v2", dependencies=[Depends(require_token)])
    def quick_record_v2(body: QuickRecordV2Body):
        return daemon.handle_request({"cmd": "quick_record_v2", **body.model_dump(exclude_unset=True)})

    # Retain authenticated obsolete URLs only to give old clients a visible
    # refresh error. These routes never inspect Collect credentials or state.
    def obsolete():
        return {"ok": False, "code": "refresh_required",
                "error": "Quick record requires the current Collect app. Refresh or update it."}

    @app.get("/api/quick-record/definitions", dependencies=[Depends(require_token)])
    def old_definitions():
        return obsolete()

    @app.get("/api/quick-record/favorites", dependencies=[Depends(require_token)])
    def old_favorites():
        return obsolete()

    @app.put("/api/quick-record/favorites", dependencies=[Depends(require_token)])
    def old_pin_change(body: QuickRecordFavoritesBody):
        return obsolete()

    @app.post("/api/annotations", dependencies=[Depends(require_token)])
    def old_record(body: RecordAnnotationBody):
        return obsolete()

    @app.delete("/api/annotations/{source_id}", dependencies=[Depends(require_token)])
    def old_undo(source_id: str):
        return obsolete()
