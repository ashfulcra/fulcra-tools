"""HTTP server that fronts the daemon via JSON API + static frontend.

Bound to 127.0.0.1 only on an ephemeral port. Writes the resulting
URL to ~/.config/fulcra-collect/web-url so the menubar can open it.
Auth: a Bearer token from ~/.config/fulcra-collect/web-token (0600)
seeded into a cookie on the initial HTML load.
"""
from __future__ import annotations

import secrets
import socket
import threading
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Depends, HTTPException, status
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from . import config as _config


def _web_token_path() -> Path:
    return _config.config_dir() / "web-token"


def _web_url_path() -> Path:
    return _config.config_dir() / "web-url"


def _ensure_token() -> str:
    p = _web_token_path()
    if p.exists():
        return p.read_text(encoding="utf-8").strip()
    token = secrets.token_urlsafe(32)
    p.write_text(token, encoding="utf-8")
    p.chmod(0o600)
    return token


def _frontend_dir() -> Path:
    # packages/collect/fulcra_collect/web.py → packages/web-ui/dist/
    here = Path(__file__).resolve()
    workspace_root = here.parents[3]
    return workspace_root / "packages" / "web-ui" / "dist"


def build_app(daemon) -> FastAPI:
    """Construct the FastAPI app with the daemon injected."""
    app = FastAPI(title="Fulcra Collect")
    token = _ensure_token()
    bearer = HTTPBearer(auto_error=False)

    def require_token(creds: HTTPAuthorizationCredentials = Depends(bearer)):
        if creds is None or creds.credentials != token:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "auth required")

    @app.get("/")
    def root():
        idx = _frontend_dir() / "index.html"
        if not idx.exists():
            return {"error": "web UI not built", "expected_at": str(idx)}
        resp = FileResponse(str(idx))
        resp.set_cookie("fulcra_token", token, httponly=False,
                         samesite="strict", secure=False, path="/")
        return resp

    static_dir = _frontend_dir() / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    @app.get("/api/status", dependencies=[Depends(require_token)])
    def status_route():
        return daemon.handle_request({"cmd": "status"})

    return app


def serve(daemon, *, host: str = "127.0.0.1", port: int = 0) -> tuple[str, threading.Thread]:
    """Start the HTTP server in a background thread. Returns (url, thread)."""
    if port == 0:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind((host, 0))
        port = s.getsockname()[1]
        s.close()

    app = build_app(daemon)
    config = uvicorn.Config(app, host=host, port=port, log_level="warning")
    server = uvicorn.Server(config)

    url = f"http://{host}:{port}"
    url_file = _web_url_path()
    url_file.write_text(url, encoding="utf-8")
    url_file.chmod(0o600)

    thread = threading.Thread(target=server.run, daemon=True, name="fulcra-web")
    thread.start()
    return url, thread
