"""In-app docs viewer.

Serves docs/<name>.md as raw markdown so the frontend can render it with
the marked library it already loads for the wizard. Used by the
dashboard's "Data sources" link. The GitHub fallback won't work while
the repo is private, so the daemon hosts these locally instead.
"""
from __future__ import annotations

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import Response

from ._deps import RouteContext


def register(app: FastAPI, ctx: RouteContext) -> None:
    require_token = ctx.require_token

    @app.get("/api/docs/{name}", dependencies=[Depends(require_token)])
    def get_docs(name: str):
        """Return the raw markdown of docs/<name>.md (path-validated).

        `name` must be a single safe identifier — letters, digits,
        hyphens, underscores only — no slashes, no leading dot, no
        traversal. We reject anything else with 400 so a typo / a
        crafted URL can't escape the docs directory.
        """
        import re
        from .. import web as _web  # for _docs_dir
        if not re.fullmatch(r"[A-Za-z0-9_-]+", name or ""):
            raise HTTPException(400, "invalid doc name")
        docs_dir = _web._docs_dir()
        path = docs_dir / f"{name}.md"
        # Defence-in-depth: even with the regex above, confirm the
        # resolved path is inside the docs dir before reading.
        try:
            path.resolve().relative_to(docs_dir.resolve())
        except (ValueError, OSError):
            raise HTTPException(400, "invalid doc path")
        if not path.is_file():
            raise HTTPException(404, f"no doc named {name!r}")
        return Response(
            content=path.read_text(encoding="utf-8"),
            media_type="text/markdown; charset=utf-8",
        )
