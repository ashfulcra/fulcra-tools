"""HTTP server that fronts the daemon via JSON API + static frontend.

Bound to 127.0.0.1 only on an ephemeral port. Writes the resulting
URL to ~/.config/fulcra-collect/web-url so the menubar can open it.
Auth: a Bearer token from ~/.config/fulcra-collect/web-token (0600)
seeded into a cookie on the initial HTML load.
"""
from __future__ import annotations

import logging
import secrets
import socket
import threading
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Depends, HTTPException, status
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel

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


# ---------------------------------------------------------------------------
# Pydantic request bodies
# ---------------------------------------------------------------------------

class SecretBody(BaseModel):
    secret: str


class FulcraTokenBody(BaseModel):
    token: str


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def build_app(daemon) -> FastAPI:
    """Construct the FastAPI app with the daemon injected."""
    app = FastAPI(title="Fulcra Collect")
    token = _ensure_token()
    bearer = HTTPBearer(auto_error=False)

    def require_token(creds: HTTPAuthorizationCredentials = Depends(bearer)):
        if creds is None or creds.credentials != token:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "auth required")

    # ------------------------------------------------------------------
    # Frontend
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # Existing status route
    # ------------------------------------------------------------------

    @app.get("/api/status", dependencies=[Depends(require_token)])
    def status_route():
        return daemon.handle_request({"cmd": "status"})

    # ------------------------------------------------------------------
    # Plugin operations — delegates to daemon.handle_request
    # ------------------------------------------------------------------

    @app.post("/api/plugin/{plugin_id}/run", dependencies=[Depends(require_token)])
    def plugin_run(plugin_id: str):
        return daemon.handle_request({"cmd": "run", "plugin": plugin_id})

    @app.post("/api/reload", dependencies=[Depends(require_token)])
    def reload_plugins():
        return daemon.handle_request({"cmd": "reload"})

    @app.get("/api/version", dependencies=[Depends(require_token)])
    def get_version():
        return daemon.handle_request({"cmd": "version"})

    @app.get("/api/plugin/{plugin_id}/credentials", dependencies=[Depends(require_token)])
    def plugin_credentials(plugin_id: str):
        return daemon.handle_request({"cmd": "credential_status", "plugin": plugin_id})

    @app.put("/api/plugin/{plugin_id}/credential/{key}", dependencies=[Depends(require_token)])
    def plugin_set_credential(plugin_id: str, key: str, body: SecretBody):
        return daemon.handle_request({
            "cmd": "set_credential",
            "plugin": plugin_id,
            "key": key,
            "secret": body.secret,
        })

    @app.delete("/api/plugin/{plugin_id}/credential/{key}", dependencies=[Depends(require_token)])
    def plugin_delete_credential(plugin_id: str, key: str):
        return daemon.handle_request({
            "cmd": "delete_credential",
            "plugin": plugin_id,
            "key": key,
        })

    # ------------------------------------------------------------------
    # Plugin settings — validates against required_settings declarations
    # ------------------------------------------------------------------

    @app.get("/api/plugin/{plugin_id}/settings", dependencies=[Depends(require_token)])
    def get_settings(plugin_id: str):
        if plugin_id not in daemon.registry.plugins:
            raise HTTPException(404, f"unknown plugin {plugin_id!r}")
        cfg = _config.load()
        return cfg.plugin_settings.get(plugin_id, {})

    @app.put("/api/plugin/{plugin_id}/settings", dependencies=[Depends(require_token)])
    def put_settings(plugin_id: str, body: dict[str, object]):
        plugin = daemon.registry.plugins.get(plugin_id)
        if plugin is None:
            raise HTTPException(404, f"unknown plugin {plugin_id!r}")
        # Validate keys against required_settings declarations
        declared = {s.key: s for s in plugin.required_settings}
        unknown = [k for k in body if k not in declared]
        if unknown:
            raise HTTPException(400, f"unknown setting keys: {unknown}")
        # Validate enum values for enum-kind settings
        for k, v in body.items():
            s = declared[k]
            if s.kind == "enum" and s.enum_values and v not in s.enum_values:
                raise HTTPException(400, f"setting {k!r}: value {v!r} not in {s.enum_values}")
        # Persist
        cfg = _config.load()
        if plugin_id not in cfg.plugin_settings:
            cfg.plugin_settings[plugin_id] = {}
        cfg.plugin_settings[plugin_id].update(body)
        _config.save(cfg)
        daemon.handle_request({"cmd": "reload"})
        return {"ok": True}

    # ------------------------------------------------------------------
    # Plugin enable / disable
    # ------------------------------------------------------------------

    @app.post("/api/plugin/{plugin_id}/enable", dependencies=[Depends(require_token)])
    def plugin_enable(plugin_id: str):
        if plugin_id not in daemon.registry.plugins:
            raise HTTPException(404, f"unknown plugin {plugin_id!r}")
        cfg = _config.load()
        cfg.enable(plugin_id)
        _config.save(cfg)
        daemon.handle_request({"cmd": "reload"})
        return {"ok": True}

    @app.post("/api/plugin/{plugin_id}/disable", dependencies=[Depends(require_token)])
    def plugin_disable(plugin_id: str):
        if plugin_id not in daemon.registry.plugins:
            raise HTTPException(404, f"unknown plugin {plugin_id!r}")
        cfg = _config.load()
        cfg.disable(plugin_id)
        _config.save(cfg)
        daemon.handle_request({"cmd": "reload"})
        return {"ok": True}

    # ------------------------------------------------------------------
    # Plugin contract introspection — drives the onboarding wizard
    # ------------------------------------------------------------------

    @app.get("/api/plugin/{plugin_id}/contract", dependencies=[Depends(require_token)])
    def plugin_contract(plugin_id: str):
        plugin = daemon.registry.plugins.get(plugin_id)
        if plugin is None:
            raise HTTPException(404, f"unknown plugin {plugin_id!r}")
        return {
            "id": plugin.id,
            "name": plugin.name,
            "kind": plugin.kind,
            "category": plugin.category,
            "description": plugin.description,
            "default_interval_s": (
                int(plugin.default_interval.total_seconds())
                if plugin.default_interval else None
            ),
            "required_settings": [
                {
                    "key": s.key,
                    "label": s.label,
                    "kind": s.kind,
                    "help": s.help,
                    "enum_values": list(s.enum_values) if s.enum_values else None,
                    "default": s.default,
                    "required": s.required,
                    "placeholder": s.placeholder,
                }
                for s in plugin.required_settings
            ],
            "required_credentials": [
                {"key": c.key, "label": c.label, "help": c.help}
                for c in plugin.required_credentials
            ],
            "required_permissions": [
                {"id": p.id, "explanation": p.explanation}
                for p in plugin.required_permissions
            ],
            "setup_steps": [
                {
                    "kind": s.kind,
                    "title": s.title,
                    "body_md": s.body_md,
                    "settings_keys": list(s.settings_keys),
                    "external_link": s.external_link,
                    "extension_url": s.extension_url,
                }
                for s in plugin.setup_steps
            ],
            "health_check_available": plugin.health_check is not None,
        }

    # ------------------------------------------------------------------
    # Plugin health check
    # ------------------------------------------------------------------

    @app.post("/api/plugin/{plugin_id}/health_check", dependencies=[Depends(require_token)])
    def plugin_health_check(plugin_id: str):
        plugin = daemon.registry.plugins.get(plugin_id)
        if plugin is None:
            raise HTTPException(404, f"unknown plugin {plugin_id!r}")
        if plugin.health_check is None:
            return {"available": False}
        # Build a minimal RunContext for the health probe.
        from . import state as _state_mod
        from .plugin import RunContext
        ctx = RunContext(
            plugin_id=plugin_id,
            config={},
            credentials={},
            state=_state_mod.load(plugin_id),
            log=logging.getLogger(f"fulcra_collect.health.{plugin_id}"),
            _emit=lambda evt: None,
        )
        try:
            result = plugin.health_check(ctx)
            return {
                "available": True,
                "ok": result.ok,
                "summary": result.summary,
                "preview": result.preview,
            }
        except Exception as exc:
            return {
                "available": True,
                "ok": False,
                "summary": f"{type(exc).__name__}: {exc}",
                "preview": [],
            }

    # ------------------------------------------------------------------
    # Fulcra account auth
    # ------------------------------------------------------------------

    @app.get("/api/fulcra/auth/status", dependencies=[Depends(require_token)])
    def fulcra_auth_status():
        from . import credentials as _creds
        return {"authenticated": _creds.has_user_secret("bearer-token")}

    @app.post("/api/fulcra/auth/token", dependencies=[Depends(require_token)])
    def fulcra_auth_set(body: FulcraTokenBody):
        from . import credentials as _creds
        if not body.token.strip():
            raise HTTPException(400, "token is empty")
        _creds.set_user_secret("bearer-token", body.token.strip())
        return {"ok": True}

    @app.delete("/api/fulcra/auth/token", dependencies=[Depends(require_token)])
    def fulcra_auth_clear():
        from . import credentials as _creds
        _creds.delete_user_secret("bearer-token")
        return {"ok": True}

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
