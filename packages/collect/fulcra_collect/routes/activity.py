"""Activity feed — recent annotation writes / attempts."""
from __future__ import annotations

from dataclasses import asdict

from fastapi import Depends, FastAPI, HTTPException

from ._deps import RouteContext


def register(app: FastAPI, ctx: RouteContext) -> None:
    daemon = ctx.daemon
    require_token = ctx.require_token

    @app.get("/api/activity", dependencies=[Depends(require_token)])
    def get_activity(limit: int = 50):
        if limit < 1 or limit > 200:
            raise HTTPException(400, "limit must be 1-200")
        entries = daemon.activity.recent(limit=limit)
        return {"entries": [asdict(e) for e in entries]}
