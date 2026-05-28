"""Menubar app status + manual launch routes.

The daemon auto-launches the menubar on startup but a user who quit it
should have a one-click path back rather than going to a terminal. (#66)
"""
from __future__ import annotations

from fastapi import Depends, FastAPI, HTTPException

from ._deps import RouteContext


def register(app: FastAPI, ctx: RouteContext) -> None:
    require_token = ctx.require_token

    @app.get("/api/menubar/status", dependencies=[Depends(require_token)])
    def menubar_status():
        from .. import menubar_launcher as _ml
        return {
            "status": _ml.status(),
            "command": _ml.menubar_command_display(),
            "supported": _ml.is_supported(),
        }

    @app.post("/api/menubar/launch", dependencies=[Depends(require_token)])
    def menubar_launch():
        from .. import menubar_launcher as _ml
        if not _ml.is_supported():
            raise HTTPException(400, "menubar app is macOS-only")
        if _ml.find_menubar_command() is None:
            raise HTTPException(
                404,
                "menubar app not installed on this machine. Install with "
                "`uv tool install fulcra-menubar` or run from a checkout "
                "with `uv run --extra macos fulcra-menubar`.",
            )
        ok = _ml.try_launch_menubar(only_if_not_running=True)
        return {"ok": ok, "status": _ml.status()}
