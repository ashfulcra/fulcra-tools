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
from dataclasses import asdict
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI, Depends, HTTPException, status
from fastapi.responses import FileResponse, HTMLResponse
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


class DefinitionBindBody(BaseModel):
    definition_id: str | None = None
    force_new: bool = False


class RecordAnnotationBody(BaseModel):
    definition_id: str
    comment: str | None = None


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def _oauth_success_html(plugin_id: str) -> str:
    """Small static page shown in the OAuth redirect tab on success.

    Posts a message to the opener wizard tab so it can advance to the
    next step, then auto-closes after 2 seconds.
    """
    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><title>Signed in — Fulcra Collect</title>
<style>
  body {{ font-family: system-ui, sans-serif; max-width: 480px;
         margin: 80px auto; text-align: center; color: #1a1a1a; }}
  h1 {{ color: #16a34a; font-size: 1.5rem; }}
  p {{ color: #555; }}
</style>
</head>
<body>
  <h1>Signed in to {plugin_id}</h1>
  <p>This tab will close automatically…</p>
  <script>
    if (window.opener) {{
      window.opener.postMessage(
        {{ type: "oauth_complete", plugin_id: {plugin_id!r} }}, "*"
      );
      setTimeout(() => window.close(), 2000);
    }}
  </script>
</body>
</html>
"""


def _oauth_failure_html(reason: str) -> str:
    """Small static page shown in the OAuth redirect tab on failure."""
    import html as _html
    safe_reason = _html.escape(reason)
    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><title>Sign-in failed — Fulcra Collect</title>
<style>
  body {{ font-family: system-ui, sans-serif; max-width: 480px;
         margin: 80px auto; text-align: center; color: #1a1a1a; }}
  h1 {{ color: #dc2626; font-size: 1.5rem; }}
  p {{ color: #555; }}
  code {{ background: #f4f4f4; padding: 2px 6px; border-radius: 4px; }}
</style>
</head>
<body>
  <h1>Sign-in failed</h1>
  <p><code>{safe_reason}</code></p>
  <p>Close this tab and try again from Fulcra Collect.</p>
</body>
</html>
"""


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

    # ------------------------------------------------------------------
    # OAuth flow — plugin-agnostic start + callback routes
    # ------------------------------------------------------------------

    @app.post("/api/oauth/{plugin_id}/start", dependencies=[Depends(require_token)])
    def oauth_start(plugin_id: str):
        plugin = daemon.registry.plugins.get(plugin_id)
        if plugin is None or plugin.oauth_handler is None:
            raise HTTPException(404, f"plugin {plugin_id!r} has no oauth_handler")
        from .oauth import start_flow
        # Prefer the persisted web-url file (exact host:port from the bound
        # server); fall back to localhost default for tests and cases where
        # the file has not been written yet (e.g. build_app called directly
        # in tests without going through serve()).
        url_file = _web_url_path()
        if url_file.exists():
            base_url = url_file.read_text(encoding="utf-8").strip()
        else:
            base_url = "http://localhost"
        redirect_uri = f"{base_url}/api/oauth/{plugin_id}/callback"
        state, _verifier, challenge = start_flow(plugin_id, redirect_uri)
        return {
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "redirect_uri": redirect_uri,
        }

    @app.get("/api/oauth/{plugin_id}/callback")  # no Bearer required — comes from the third party
    def oauth_callback(
        plugin_id: str,
        code: str | None = None,
        state: str | None = None,
        error: str | None = None,
    ):
        from .oauth import complete_flow
        from . import credentials as _creds
        if error:
            return HTMLResponse(_oauth_failure_html(error), status_code=400)
        if not code or not state:
            return HTMLResponse(
                _oauth_failure_html("missing code or state"), status_code=400
            )
        pending = complete_flow(state)
        if pending is None or pending.plugin_id != plugin_id:
            return HTMLResponse(
                _oauth_failure_html("invalid or expired state"), status_code=400
            )
        plugin = daemon.registry.plugins.get(plugin_id)
        if plugin is None or plugin.oauth_handler is None:
            return HTMLResponse(
                _oauth_failure_html("plugin not found or no handler"), status_code=400
            )
        try:
            tokens = plugin.oauth_handler(
                plugin_id=plugin_id,
                code=code,
                code_verifier=pending.code_verifier,
                redirect_uri=pending.redirect_uri,
            )
        except Exception as exc:
            return HTMLResponse(
                _oauth_failure_html(f"token exchange failed: {exc}"), status_code=500
            )
        # Store each returned token under the plugin's credentials namespace.
        for key, value in tokens.items():
            if value:
                _creds.set_secret(plugin_id, key, value)
        return HTMLResponse(_oauth_success_html(plugin_id), status_code=200)

    # ------------------------------------------------------------------
    # Activity feed — recent annotation writes / attempts
    # ------------------------------------------------------------------

    @app.get("/api/activity", dependencies=[Depends(require_token)])
    def get_activity(limit: int = 50):
        if limit < 1 or limit > 200:
            raise HTTPException(400, "limit must be 1-200")
        entries = daemon.activity.recent(limit=limit)
        return {"entries": [asdict(e) for e in entries]}

    # ------------------------------------------------------------------
    # Definitions — list, preview recent entries, bind / clear
    # ------------------------------------------------------------------

    def _fulcra_token_or_401():
        """Return the shared user-level Fulcra bearer token, or raise 401."""
        from . import credentials as _creds
        token_val = _creds.get_user_secret("bearer-token")
        if not token_val:
            raise HTTPException(401, "Fulcra not authenticated — set a bearer token first")
        return token_val

    def _fulcra_http_client(fulcra_token: str):
        """Return an httpx.Client pre-configured to talk to the Fulcra API."""
        from fulcra_common import DEFAULT_BASE_URL
        return httpx.Client(
            base_url=DEFAULT_BASE_URL,
            timeout=15.0,
            headers={
                "Authorization": f"Bearer {fulcra_token}",
                "User-Agent": "fulcra-collect/web-ui",
            },
            follow_redirects=True,
        )

    @app.get("/api/definitions", dependencies=[Depends(require_token)])
    def list_definitions(annotation_type: str | None = None):
        """List Fulcra annotation definitions, optionally filtered by
        annotation_type (e.g. 'duration', 'moment').

        Calls the Fulcra API directly with the user-level bearer token.
        Returns only non-deleted definitions.
        """
        fulcra_token = _fulcra_token_or_401()
        try:
            with _fulcra_http_client(fulcra_token) as client:
                r = client.get("/user/v1alpha1/annotation")
                r.raise_for_status()
                defs = r.json()
        except Exception as exc:
            raise HTTPException(502, f"Fulcra API error: {exc}") from exc
        # Filter out soft-deleted definitions
        defs = [d for d in defs if not d.get("deleted_at")]
        if annotation_type:
            defs = [d for d in defs if d.get("annotation_type") == annotation_type]
        return {"definitions": defs}

    @app.get("/api/definitions/{def_id}/recent", dependencies=[Depends(require_token)])
    def definition_recent(def_id: str, limit: int = 5):
        """Return the last N annotations from a Fulcra definition for
        preview in the definition-picker UI.

        Uses the DurationAnnotation data type by default; the response
        contains raw event records from the Fulcra API. limit must be 1-20.
        """
        if limit < 1 or limit > 20:
            raise HTTPException(400, "limit must be 1-20")
        fulcra_token = _fulcra_token_or_401()
        try:
            from datetime import datetime, timezone, timedelta
            now = datetime.now(timezone.utc)
            # Look back 1 year as a practical window for "recent" entries.
            start = now - timedelta(days=365)
            with _fulcra_http_client(fulcra_token) as client:
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
                        if def_source in (rec.get("metadata", {}).get("source") or [])
                        or rec.get("source_id") == def_source
                    ]
                    entries.extend(matched)
                    if entries:
                        break
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(502, f"Fulcra API error: {exc}") from exc
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
        fresh definition (the plugin's canonical_name gets a machine-id suffix).
        """
        if plugin_id not in daemon.registry.plugins:
            raise HTTPException(404, f"unknown plugin {plugin_id!r}")
        if not body.definition_id and not body.force_new:
            raise HTTPException(400, "body must include definition_id or force_new=true")
        from . import state as _state_mod
        st = _state_mod.load(plugin_id)
        if body.force_new:
            # Clear the cached definition_id; the plugin's next run will
            # call resolve_definition_id with force_new=True via RunContext.
            st.definition_id = None
        else:
            st.definition_id = body.definition_id
        _state_mod.save(st)
        return {"ok": True}

    @app.delete("/api/plugin/{plugin_id}/definition", dependencies=[Depends(require_token)])
    def clear_definition(plugin_id: str):
        """Clear the plugin's cached definition_id. The next run will
        re-resolve (adopt an existing matching definition, or create one)."""
        if plugin_id not in daemon.registry.plugins:
            raise HTTPException(404, f"unknown plugin {plugin_id!r}")
        from . import state as _state_mod
        st = _state_mod.load(plugin_id)
        st.definition_id = None
        _state_mod.save(st)
        return {"ok": True}

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
        """Write one Moment annotation immediately to Fulcra. Used by the
        menubar quick-record buttons and any direct web UI callers."""
        return daemon.handle_request({
            "cmd": "record_annotation",
            "definition_id": body.definition_id,
            "comment": body.comment,
        })

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
