"""HTTP server that fronts the daemon via JSON API + static frontend.

Bound to 127.0.0.1 only on an ephemeral port. Writes the resulting
URL to ~/.config/fulcra-collect/web-url so the menubar can open it.
Auth: a Bearer token from ~/.config/fulcra-collect/web-token (0600)
seeded into a cookie on the initial HTML load.
"""
from __future__ import annotations

import logging
import os
import secrets
import socket
import threading
import time
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


def _write_secret_file(path: Path, content: str) -> None:
    """Write content to path with 0600 permissions atomically.

    For newly-created files, uses O_CREAT|O_EXCL so there is never a
    world-readable window between write_text and chmod. For files that
    already exist (already restricted), falls back to a plain overwrite.
    """
    if path.exists():
        path.write_text(content, encoding="utf-8")
        path.chmod(0o600)
        return
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(content)


def _ensure_token() -> str:
    p = _web_token_path()
    if p.exists():
        return p.read_text(encoding="utf-8").strip()
    token = secrets.token_urlsafe(32)
    _write_secret_file(p, token)
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
    next step, then auto-closes after 2 seconds. The postMessage target
    origin is set to window.location.origin (same origin) rather than
    "*" to prevent message interception by cross-origin frames.
    """
    import html as _html
    safe_id = _html.escape(plugin_id)
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
  <h1>Signed in to {safe_id}</h1>
  <p>This tab will close automatically…</p>
  <script>
    if (window.opener) {{
      window.opener.postMessage(
        {{ type: "oauth_complete", plugin_id: {plugin_id!r} }},
        window.location.origin
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
        # Use secrets.compare_digest to prevent timing-based token oracle attacks.
        if creds is None or not secrets.compare_digest(creds.credentials, token):
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
                    "annotation_type": s.annotation_type,
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
        # Populate credentials from the keychain so health checks that
        # call ctx.credentials.get("api_key") see real values, not an
        # empty dict.
        from . import credentials as _creds
        from . import state as _state_mod
        from .plugin import RunContext
        ctx_credentials = {}
        for c in plugin.required_credentials:
            val = _creds.get_secret(plugin_id, c.key)
            if val is not None:
                ctx_credentials[c.key] = val
        ctx = RunContext(
            plugin_id=plugin_id,
            config={},
            credentials=ctx_credentials,
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
        token = body.token.strip()
        if not token:
            raise HTTPException(400, "token is empty")
        # Validate against Fulcra before storing so typos are caught at
        # the door, not on first plugin run.
        _log = logging.getLogger("fulcra_collect.web")
        try:
            with httpx.Client(timeout=10.0) as client:
                r = client.get(
                    "https://api.fulcradynamics.com/data/v0/annotation-defs",
                    headers={"Authorization": f"Bearer {token}"},
                )
            if r.status_code == 401 or r.status_code == 403:
                raise HTTPException(
                    401,
                    "Fulcra rejected the token. Double-check you copied the full value.",
                )
            r.raise_for_status()
        except HTTPException:
            raise
        except httpx.TimeoutException:
            raise HTTPException(
                504,
                "Fulcra didn't respond in time. Check your internet, then try again.",
            )
        except httpx.HTTPError as exc:
            _log.exception("Fulcra token validation failed: %s", exc)
            raise HTTPException(502, f"Could not reach Fulcra: {type(exc).__name__}")
        _creds.set_user_secret("bearer-token", token)
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
        # Prefer the URL held on the daemon object (set by serve() after the
        # socket is bound and uvicorn is ready). Fall back to the persisted
        # web-url file, then to the daemon's _web_url attribute if present,
        # then error — "http://localhost" (port-less) would cause Trakt to
        # redirect to port 80, where nothing listens.
        base_url: str | None = getattr(daemon, "_web_url", None)
        if not base_url:
            url_file = _web_url_path()
            if url_file.exists():
                base_url = url_file.read_text(encoding="utf-8").strip()
        if not base_url:
            raise HTTPException(
                503,
                "Server URL not yet available — retry in a moment once the"
                " daemon has finished starting.",
            )
        redirect_uri = f"{base_url}/api/oauth/{plugin_id}/callback"
        state, _verifier, challenge = start_flow(plugin_id, redirect_uri)
        response: dict = {
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "redirect_uri": redirect_uri,
        }
        # If the plugin supplies an oauth_authorize_url callable, use it to
        # build the full authorize URL so the wizard can open it directly in
        # a new tab without knowing the plugin's OAuth endpoint or client_id.
        if plugin.oauth_authorize_url is not None:
            from . import credentials as _creds
            client_id = _creds.get_secret(plugin_id, "client_id") or ""
            response["authorize_url"] = plugin.oauth_authorize_url(
                client_id, redirect_uri, state, challenge
            )
        return response

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
        _log = logging.getLogger("fulcra_collect.web")
        fulcra_token = _fulcra_token_or_401()
        try:
            with _fulcra_http_client(fulcra_token) as client:
                r = client.get("/user/v1alpha1/annotation")
                r.raise_for_status()
                defs = r.json()
        except HTTPException:
            raise
        except Exception as exc:
            _log.exception("list_definitions: Fulcra API request failed")
            raise HTTPException(
                502,
                "Fulcra didn't respond. Check your internet, then try again.",
            ) from exc
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
            logging.getLogger("fulcra_collect.web").exception(
                "definition_recent(%s): Fulcra API request failed", def_id
            )
            raise HTTPException(
                502,
                "Fulcra didn't respond. Check your internet, then try again.",
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
    """Start the HTTP server in a background thread. Returns (url, thread).

    Port-assignment strategy: when port=0, we open a socket, bind it to an
    ephemeral port, and hand the *already-bound* file descriptor to uvicorn
    via uvicorn.Config(fd=...) — no close/reopen race window. uvicorn 0.30+
    supports the fd parameter.
    """
    bound_socket_fd: int | None = None
    if port == 0:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((host, 0))
        s.listen()
        port = s.getsockname()[1]
        bound_socket_fd = s.fileno()
        # Keep s alive until we've handed the fd to uvicorn; the fd stays
        # valid across the Config/Server construction below.

    app = build_app(daemon)
    config_kwargs: dict = {"app": app, "host": host, "port": port, "log_level": "warning"}
    if bound_socket_fd is not None:
        config_kwargs["fd"] = bound_socket_fd
    config = uvicorn.Config(**config_kwargs)
    server = uvicorn.Server(config)

    url = f"http://{host}:{port}"

    thread = threading.Thread(target=server.run, daemon=True, name="fulcra-web")
    thread.start()

    # Wait for uvicorn to finish binding before advertising the URL. The
    # server.started flag is set inside uvicorn after the socket is in the
    # accept loop. Timeout is generous (5 s); if uvicorn never sets it we
    # proceed anyway — the menubar's retry loop will catch a brief delay.
    for _ in range(50):
        if getattr(server, "started", False):
            break
        time.sleep(0.1)

    # Store URL on the daemon object so oauth_start can use it without
    # reading the file (avoids a race if the file hasn't been written yet).
    if hasattr(daemon, "_web_url"):
        daemon._web_url = url

    url_file = _web_url_path()
    _write_secret_file(url_file, url)

    return url, thread
