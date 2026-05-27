"""Status / version / reload routes."""
from __future__ import annotations

from fastapi import Depends, FastAPI

from ._deps import RouteContext


def register(app: FastAPI, ctx: RouteContext) -> None:
    daemon = ctx.daemon
    require_token = ctx.require_token

    @app.get("/api/status", dependencies=[Depends(require_token)])
    def status_route():
        return daemon.handle_request({"cmd": "status"})

    @app.post("/api/reload", dependencies=[Depends(require_token)])
    def reload_plugins():
        return daemon.handle_request({"cmd": "reload"})

    @app.get("/api/version", dependencies=[Depends(require_token)])
    def get_version():
        return daemon.handle_request({"cmd": "version"})
