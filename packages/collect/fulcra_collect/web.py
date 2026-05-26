"""HTTP server that fronts the daemon via JSON API + static frontend.

Bound to 127.0.0.1 only on a stable port (default 9292; override via
`[daemon] web_port = N` in config.toml). A stable port is what lets OAuth
redirect URIs stay valid across daemon restarts (otherwise every plugin's
OAuth registration breaks when the daemon restarts) and lets the
attention browser extension post to a known endpoint without separate
discovery.

Writes the resulting URL to ~/.config/fulcra-collect/web-url so the
menubar and ad-hoc tools can keep using the file (it's now just the
same URL every time).

Auth: a Bearer token from ~/.config/fulcra-collect/web-token (0600)
seeded into a cookie on the initial HTML load.
"""
from __future__ import annotations

import logging
import os
import secrets
import threading
import time
from dataclasses import asdict
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI, Depends, File, HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel

from . import config as _config


# Cap for /api/plugin/{id}/upload — generous because some takeouts (e.g.
# Spotify Extended Streaming History) can be multiple GB, but bounded so
# a buggy client can't fill the disk even though the route is loopback-only.
_UPLOAD_MAX_BYTES = 10 * 1024 * 1024 * 1024  # 10 GB

# Chunk size for streaming uploads to disk. Big enough to keep syscall
# overhead low, small enough that the per-request memory footprint stays flat.
_UPLOAD_CHUNK_BYTES = 64 * 1024


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

    def _require_plugin(plugin_id: str) -> None:
        """Translate an unknown plugin id into a proper HTTP 404 so frontend
        code can rely on the status code (task #83). Routes that just
        delegate to daemon.handle_request() previously returned 200 with
        ``{ok: false, error: ...}`` for unknown ids — same shape the
        internal control-socket protocol uses, but inappropriate over HTTP.
        """
        if plugin_id not in daemon.registry.plugins:
            raise HTTPException(404, f"unknown plugin {plugin_id!r}")

    @app.post("/api/plugin/{plugin_id}/run", dependencies=[Depends(require_token)])
    def plugin_run(plugin_id: str):
        _require_plugin(plugin_id)
        return daemon.handle_request({"cmd": "run", "plugin": plugin_id})

    @app.post("/api/reload", dependencies=[Depends(require_token)])
    def reload_plugins():
        return daemon.handle_request({"cmd": "reload"})

    @app.get("/api/version", dependencies=[Depends(require_token)])
    def get_version():
        return daemon.handle_request({"cmd": "version"})

    @app.get("/api/plugin/{plugin_id}/credentials", dependencies=[Depends(require_token)])
    def plugin_credentials(plugin_id: str):
        _require_plugin(plugin_id)
        return daemon.handle_request({"cmd": "credential_status", "plugin": plugin_id})

    @app.put("/api/plugin/{plugin_id}/credential/{key}", dependencies=[Depends(require_token)])
    def plugin_set_credential(plugin_id: str, key: str, body: SecretBody):
        _require_plugin(plugin_id)
        return daemon.handle_request({
            "cmd": "set_credential",
            "plugin": plugin_id,
            "key": key,
            "secret": body.secret,
        })

    @app.delete("/api/plugin/{plugin_id}/credential/{key}", dependencies=[Depends(require_token)])
    def plugin_delete_credential(plugin_id: str, key: str):
        _require_plugin(plugin_id)
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
    # File upload — backs the wizard's file_upload step. The user picks a
    # local file (e.g. a Spotify Extended Streaming History zip, an Apple
    # takeout, a Netflix viewing CSV); the browser POSTs it here as
    # multipart/form-data; we stream it to a per-plugin uploads directory
    # and persist the resulting absolute path into the plugin's settings
    # under the supplied `key`. Plugins' run() then reads
    # ctx.config[<key>] as a filesystem path (which is what they already
    # expect — see e.g. fulcra_media.collect_plugins).
    #
    # The previous wizard implementation base64-encoded the file in the
    # browser and stuffed the blob into the setting value directly; that
    # crashed plugins (which tried to resolve the blob as a path) and
    # OOMed the browser tab for multi-GB takeouts.
    # ------------------------------------------------------------------

    @app.post(
        "/api/plugin/{plugin_id}/upload",
        dependencies=[Depends(require_token)],
    )
    async def plugin_upload(
        plugin_id: str,
        key: str,
        request: Request,
        file: UploadFile = File(...),
    ):
        plugin = daemon.registry.plugins.get(plugin_id)
        if plugin is None:
            raise HTTPException(404, f"unknown plugin {plugin_id!r}")
        # The setting must be declared on the plugin AND be of kind "path".
        # Anything else (text/url/enum/etc.) almost certainly indicates a
        # frontend bug — fail loudly rather than silently shoving a path
        # into a free-text field.
        declared = {s.key: s for s in plugin.required_settings}
        setting = declared.get(key)
        if setting is None:
            raise HTTPException(400, f"unknown setting key {key!r}")
        if setting.kind != "path":
            raise HTTPException(
                400,
                f"setting {key!r} has kind {setting.kind!r}; "
                f"uploads only allowed for 'path' settings",
            )

        # Filename sanitation. UploadFile.filename comes straight from the
        # multipart headers — treat it as adversarial input. Reject any
        # name containing path separators, "..", or that resolves to a
        # special component, so a malicious client can't escape the
        # per-plugin uploads directory. We validate the *raw* name (not
        # Path(raw).name) so that a request with filename="../etc/passwd"
        # produces a 400 rather than silently being de-fanged to
        # "passwd" — making the bug visible to the caller.
        raw_name = file.filename or ""
        if (
            not raw_name
            or raw_name in (".", "..")
            or ".." in raw_name
            or "/" in raw_name
            or "\\" in raw_name
            or raw_name.startswith("~")
            or Path(raw_name).is_absolute()
        ):
            raise HTTPException(400, f"invalid filename {raw_name!r}")
        safe_name = raw_name

        # Short-circuit obviously oversize uploads using the Content-Length
        # header. We re-check the actual byte count while streaming below;
        # this header check just spares us from accepting a 50 GB POST and
        # discovering the cap mid-write.
        cl_header = request.headers.get("content-length")
        if cl_header:
            try:
                declared_len = int(cl_header)
            except ValueError:
                declared_len = -1
            if declared_len > _UPLOAD_MAX_BYTES:
                raise HTTPException(
                    413,
                    f"upload exceeds maximum size of {_UPLOAD_MAX_BYTES} bytes",
                )

        target_dir = _config.config_dir() / "uploads" / plugin_id
        target_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Belt-and-braces: ensure permissions even if the dir already existed
        # with looser perms from an older daemon version.
        try:
            target_dir.chmod(0o700)
        except OSError:
            pass

        target = target_dir / safe_name
        tmp = target.with_suffix(target.suffix + ".tmp")

        # Atomic write: stream into a sibling .tmp file, then os.rename onto
        # the target. Avoids leaving a half-written file behind on a crash
        # or upload-cap trip, and avoids the small window where another
        # reader could see a partial file at the final path.
        written = 0
        try:
            fd = os.open(
                str(tmp),
                os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                0o600,
            )
            with os.fdopen(fd, "wb") as out:
                while True:
                    chunk = await file.read(_UPLOAD_CHUNK_BYTES)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > _UPLOAD_MAX_BYTES:
                        raise HTTPException(
                            413,
                            f"upload exceeds maximum size of "
                            f"{_UPLOAD_MAX_BYTES} bytes",
                        )
                    out.write(chunk)
            os.replace(str(tmp), str(target))
            # os.replace preserves the .tmp file's 0600 mode on POSIX, but
            # re-chmod defensively in case the filesystem dropped it.
            os.chmod(str(target), 0o600)
        except HTTPException:
            # Clean up the partial .tmp file on a size-cap trip etc.
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass
            raise
        except Exception as exc:
            logging.getLogger("fulcra_collect.web").exception(
                "upload failed for plugin=%s key=%s", plugin_id, key,
            )
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass
            raise HTTPException(500, f"upload failed: {type(exc).__name__}: {exc}")
        finally:
            # UploadFile holds an underlying SpooledTemporaryFile; close it
            # so the temp-file slot is released even on the error paths.
            try:
                await file.close()
            except Exception:
                pass

        absolute = str(target.resolve())

        # Persist the absolute path into the plugin's settings. We bypass
        # the PUT /settings route here because that route validates via
        # `body[k] in body` etc. against declared settings, but the upload
        # path is just a string assignment — and we've already validated
        # the key + kind above.
        cfg = _config.load()
        if plugin_id not in cfg.plugin_settings:
            cfg.plugin_settings[plugin_id] = {}
        cfg.plugin_settings[plugin_id][key] = absolute
        _config.save(cfg)
        # Surface the new value to the running daemon so the plugin's next
        # run sees the path immediately (mirrors PUT /settings' behaviour).
        daemon.handle_request({"cmd": "reload"})

        return {"ok": True, "path": absolute, "size": written}

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
                    "enum_labels": list(s.enum_labels) if s.enum_labels else None,
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
            "permission_check_available": plugin.permission_check is not None,
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
        # Populate credentials from the keychain AND settings from the
        # plugin_settings store so health checks that read either see real
        # values, not an empty dict. Until 2026-05-25 only credentials were
        # populated — a plugin like Last.fm (whose username is a Setting,
        # not a Credential) couldn't tell its health_check who to look up,
        # which made adding a test_connection step pointless. Plugin run
        # contexts get both; health contexts now match.
        from . import config as _config
        from . import credentials as _creds
        from . import state as _state_mod
        from .plugin import RunContext
        ctx_credentials = {}
        for c in plugin.required_credentials:
            val = _creds.get_secret(plugin_id, c.key)
            if val is not None:
                ctx_credentials[c.key] = val
        ctx_config = dict(_config.load().plugin_settings.get(plugin_id, {}))
        ctx = RunContext(
            plugin_id=plugin_id,
            config=ctx_config,
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
    # Plugin permission check — verify an OS permission (e.g. Full Disk
    # Access) actually works, so the wizard can show "verified" instead
    # of guessing.
    # ------------------------------------------------------------------

    @app.post("/api/plugin/{plugin_id}/check_permission",
              dependencies=[Depends(require_token)])
    def plugin_check_permission(plugin_id: str):
        plugin = daemon.registry.plugins.get(plugin_id)
        if plugin is None:
            raise HTTPException(404, f"unknown plugin {plugin_id!r}")
        if plugin.permission_check is None:
            raise HTTPException(404, f"plugin {plugin_id!r} has no permission_check")
        # Build a minimal RunContext, mirroring the health_check route.
        # We populate ctx.config from the persisted plugin settings so
        # the check can branch on user choices (e.g. dayone's mode enum).
        from . import credentials as _creds
        from . import state as _state_mod
        from .plugin import RunContext
        ctx_credentials = {}
        for c in plugin.required_credentials:
            val = _creds.get_secret(plugin_id, c.key)
            if val is not None:
                ctx_credentials[c.key] = val
        cfg = _config.load()
        ctx = RunContext(
            plugin_id=plugin_id,
            config=cfg.plugin_settings.get(plugin_id, {}),
            credentials=ctx_credentials,
            state=_state_mod.load(plugin_id),
            log=logging.getLogger(f"fulcra_collect.permission.{plugin_id}"),
            _emit=lambda evt: None,
        )
        try:
            result = plugin.permission_check(ctx)
            granted = bool(result.get("granted", False))
            hint = result.get("hint")
            return {"granted": granted, "hint": hint}
        except Exception as exc:
            return {
                "granted": False,
                "hint": f"{type(exc).__name__}: {exc}",
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
                    "https://api.fulcradynamics.com/user/v1alpha1/annotation",
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
    # Fulcra account auth — delegate to the `fulcra` CLI's browser-based
    # device-authorization flow as a friendlier alternative to paste-token.
    #
    # cli_status probes whether the user already has working fulcra-CLI
    # credentials on disk; cli_login shells out to `fulcra auth login`,
    # which opens a browser and polls up to 2 minutes for the user to
    # complete sign-in. On success we capture the token via
    # `fulcra auth print-access-token` and stash it in the same keychain
    # slot the paste-token path uses, so all downstream code is unchanged.
    # ------------------------------------------------------------------

    @app.get("/api/fulcra/auth/cli_status", dependencies=[Depends(require_token)])
    def fulcra_auth_cli_status():
        import shutil
        import subprocess

        cli_path = shutil.which("fulcra")
        if not cli_path:
            return {"available": False, "signed_in": False,
                    "error": "The fulcra CLI is not on PATH."}
        try:
            r = subprocess.run(
                [cli_path, "auth", "print-access-token"],
                capture_output=True, text=True, timeout=10,
            )
        except subprocess.TimeoutExpired:
            return {"available": True, "signed_in": False,
                    "error": "fulcra auth print-access-token timed out."}
        signed_in = r.returncode == 0 and r.stdout.strip() != ""
        return {"available": True, "signed_in": signed_in}

    @app.post("/api/fulcra/auth/cli_login", dependencies=[Depends(require_token)])
    def fulcra_auth_cli_login():
        import shutil
        import subprocess
        from . import credentials as _creds

        _log = logging.getLogger("fulcra_collect.web")

        cli_path = shutil.which("fulcra")
        if not cli_path:
            raise HTTPException(
                424,
                "The fulcra CLI is not on PATH. Install it with "
                "`uv tool install fulcra-api`, or sign in with a token instead.",
            )

        # `fulcra auth login` polls up to 120 s by default; give ourselves a
        # little headroom for browser startup + the user clicking through.
        try:
            login = subprocess.run(
                [cli_path, "auth", "login"],
                capture_output=True, text=True, timeout=150,
            )
        except subprocess.TimeoutExpired:
            raise HTTPException(
                504,
                "Sign-in didn't complete within 2 minutes. "
                "Try again, or sign in with a token.",
            )
        if login.returncode != 0:
            # fulcra-api's login prints a user-readable error to stderr
            # (e.g. "Authorization denied", "Network error"). Surface it.
            tail = (login.stderr or login.stdout or "").strip().splitlines()
            msg = tail[-1] if tail else f"fulcra auth login exit {login.returncode}"
            raise HTTPException(400, f"Fulcra sign-in failed: {msg}")

        # Login succeeded — capture the token via print-access-token.
        try:
            tok = subprocess.run(
                [cli_path, "auth", "print-access-token"],
                capture_output=True, text=True, timeout=10,
            )
        except subprocess.TimeoutExpired:
            raise HTTPException(
                504, "Could not read the token from the fulcra CLI in time.",
            )
        if tok.returncode != 0 or not tok.stdout.strip():
            raise HTTPException(
                500,
                "Sign-in reported success but no token could be read from "
                "the fulcra CLI.",
            )
        token = tok.stdout.strip()

        # Belt-and-braces: validate against the Fulcra API before storing,
        # same as the paste-token path. Catches the (rare) case where the
        # CLI's stored token is for the wrong tenant or has been revoked
        # server-side.
        try:
            with httpx.Client(timeout=10.0) as client:
                r = client.get(
                    "https://api.fulcradynamics.com/user/v1alpha1/annotation",
                    headers={"Authorization": f"Bearer {token}"},
                )
            if r.status_code in (401, 403):
                raise HTTPException(
                    401,
                    "The fulcra CLI returned a token Fulcra wouldn't accept. "
                    "Try `fulcra auth login` in a terminal to diagnose.",
                )
            r.raise_for_status()
        except HTTPException:
            raise
        except httpx.TimeoutException:
            raise HTTPException(
                504,
                "Fulcra didn't respond in time during token validation.",
            )
        except httpx.HTTPStatusError as exc:
            # Surface the real status code + a snippet of the body so the
            # user (and the daemon log) tell us exactly what Fulcra rejected,
            # instead of the opaque "HTTPStatusError" wrapper.
            body_snip = (exc.response.text or "").strip().replace("\n", " ")[:200]
            _log.warning(
                "Fulcra token validation (CLI path) status=%d body=%r",
                exc.response.status_code, body_snip,
            )
            raise HTTPException(
                502,
                f"Fulcra returned HTTP {exc.response.status_code} during "
                f"token validation"
                + (f": {body_snip}" if body_snip else "."),
            )
        except httpx.HTTPError as exc:
            _log.exception("Fulcra token validation (CLI path) failed: %s", exc)
            raise HTTPException(502, f"Could not reach Fulcra: {type(exc).__name__}")

        _creds.set_user_secret("bearer-token", token)
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

    # ------------------------------------------------------------------
    # One-click extension pairing — generate a fresh extension-token,
    # stash it in the user-level keychain, and return it (plus the
    # daemon URL) so the wizard can postMessage it straight to the
    # Fulcra Attention browser extension via a content script.
    #
    # Idempotent on re-pair: a second call overwrites the previously
    # stored token, which is what the user wants if they re-installed
    # the extension or rotated machines. The keychain write is wrapped
    # in try/except so a keychain failure surfaces as a clean 503 rather
    # than a 500 traceback.
    # ------------------------------------------------------------------

    @app.post(
        "/api/plugin/attention-relay/pair",
        dependencies=[Depends(require_token)],
    )
    def attention_relay_pair():
        from . import credentials as _creds
        new_token = secrets.token_urlsafe(32)
        try:
            _creds.set_user_secret("extension-token", new_token)
        except Exception as exc:
            logging.getLogger("fulcra_collect.web").exception(
                "attention-relay/pair: keychain write failed",
            )
            raise HTTPException(
                503,
                f"Could not store extension token in keychain: "
                f"{type(exc).__name__}: {exc}",
            )
        # Same resolution order as the OAuth start route — prefer the
        # daemon's live _web_url so the URL is always exactly the one
        # the running daemon is bound to.
        base_url: str | None = getattr(daemon, "_web_url", None)
        if not base_url:
            url_file = _web_url_path()
            if url_file.exists():
                base_url = url_file.read_text(encoding="utf-8").strip()
        if not base_url:
            # Fall back to constructing the URL from the daemon config so
            # tests (which don't run serve()) can still hit this route.
            port = (
                daemon.config.web_port if hasattr(daemon, "config")
                else _config.DEFAULT_WEB_PORT
            )
            base_url = f"http://127.0.0.1:{port}"
        return {"token": new_token, "daemon_url": base_url}

    # ------------------------------------------------------------------
    # Extension endpoint — receives browse-attention events from the
    # Fulcra Attention browser extension. Replaces the old standalone
    # `fulcra-attention` relay on port 8771; that process is gone and
    # the extension now points at this daemon's stable port.
    #
    # Auth: a Bearer token under user-level keychain key "extension-token"
    # (distinct from the web-token that gates the UI and from the Fulcra
    # bearer-token that gates Fulcra). The extension stores the same
    # token in its options page.
    # ------------------------------------------------------------------

    @app.post("/api/extension/attention")
    async def extension_attention(request: Request):
        """Accept one attention event from the browser extension and
        forward it to Fulcra via the attention package's ingest helpers.

        Every error path is wrapped — a malformed event must never crash
        the daemon. Auth failures return 401, schema failures return 400,
        upstream failures return 502. The HTTP body shape matches the
        old standalone relay's `/attention` endpoint so the extension
        only needs its URL updated, not its payload code.
        """
        from . import credentials as _creds
        _log = logging.getLogger("fulcra_collect.web.extension")

        # --- Auth ---------------------------------------------------------
        # We do auth by hand (not via the require_token dependency) because
        # the extension uses a different keychain entry than the web UI's
        # cookie token. Both are bearer tokens, but they're different
        # secrets.
        expected = _creds.get_user_secret("extension-token")
        if not expected:
            # Daemon never had an extension token configured. Return 401
            # rather than 503 because, from the extension's perspective,
            # the auth header it sent is invalid here (there's nothing to
            # match against).
            raise HTTPException(401, "extension-token not configured")
        header = request.headers.get("authorization") or ""
        sent = header[7:].strip() if header.lower().startswith("bearer ") else ""
        if not sent or not secrets.compare_digest(sent, expected):
            raise HTTPException(401, "unauthorized")

        # --- Body + dispatch into the attention ingest helpers -----------
        try:
            payload = await request.json()
        except Exception as exc:
            # FastAPI's await request.json() raises on any decode failure
            # (invalid utf-8, malformed JSON, etc.). 400, not 500.
            _log.info("extension POST: malformed JSON body: %r", exc)
            raise HTTPException(400, "malformed JSON body")

        try:
            # Deferred import: the daemon doesn't formally depend on
            # `fulcra-attention`, but in the workspace it's always
            # installed alongside. If for some reason it isn't, surface
            # a clear 503 rather than a 500-shaped traceback.
            try:
                from fulcra_attention.ingest import (
                    build_attention_event, validate_payload, _to_second_iso,
                )
                from fulcra_attention import state as _att_state_mod
                from fulcra_attention.fulcra import FulcraClient
            except ImportError as exc:
                _log.warning(
                    "extension POST: fulcra_attention is not installed (%s)", exc,
                )
                raise HTTPException(
                    503,
                    "fulcra_attention package not installed; "
                    "install it to enable this endpoint",
                )

            # Schema check — same validator the old relay used.
            try:
                validate_payload(payload)
            except ValueError as exc:
                raise HTTPException(400, f"bad payload: {exc}")

            # Load the attention plugin's persisted state (definition id +
            # tag cache). If the user hasn't bound a definition yet, we
            # can't ingest — return 412 (precondition failed) so the
            # extension can show a meaningful error.
            attention_state = _att_state_mod.load()
            if not attention_state.attention_definition_id:
                # Wizard's definition_picker step writes the chosen def
                # id to per-plugin state (state/attention-relay.json) via
                # /api/plugin/{id}/definition. The extension's per-package
                # store (fulcra-attention/state.json) only gets the id
                # via attention.run()'s ensure_definitions path — which
                # doesn't fire when the user just walks the wizard and
                # then starts browsing. Without this fallback the wizard's
                # "Attention is set" message would be a lie. See task #29.
                from . import state as _collect_state_mod
                try:
                    relay_state = _collect_state_mod.load("attention-relay")
                except Exception:
                    relay_state = None
                fallback_id = (
                    getattr(relay_state, "definition_id", None)
                    if relay_state is not None else None
                )
                if fallback_id:
                    attention_state.attention_definition_id = fallback_id
                    # Seed the base tags too — build_attention_event
                    # below reads state.tag_ids["attention"] and ["web"]
                    # eagerly. ensure_definitions handles both id + tags
                    # in one trip and adopts the existing def by name
                    # rather than creating a duplicate.
                    try:
                        _tmp_client = FulcraClient()
                        _tmp_client.ensure_definitions(attention_state)
                    except Exception:
                        _log.exception(
                            "extension POST: ensure_definitions during "
                            "lazy-migrate failed"
                        )
                    try:
                        _att_state_mod.save(attention_state)
                    except Exception:
                        _log.exception(
                            "extension POST: lazy-migrate of attention "
                            "def_id from per-plugin state failed"
                        )
                else:
                    raise HTTPException(
                        412,
                        "attention definition not bound; complete the "
                        "attention plugin setup in the Fulcra Collect UI",
                    )

            client = FulcraClient()

            # Stale-definition guard: validate that the cached
            # attention_definition_id still exists on the *current*
            # Fulcra account, every _attention_validation_interval_s.
            # Without this, a daemon that re-auths to a different account
            # keeps ingesting events whose source_id points at a def in
            # the previous account — Fulcra accepts them (HTTP 200) but
            # they're invisible in the timeline because they have no
            # metadata to render against. See task #12.
            now_mono = daemon._monotonic()
            stale = (
                daemon._attention_def_validated_id
                    != attention_state.attention_definition_id
                or now_mono - daemon._attention_def_validated_at
                    >= daemon._attention_validation_interval_s
            )
            if stale:
                if not client.definition_exists(
                    attention_state.attention_definition_id,
                ):
                    # Orphan def. Clear it (and the tag cache, which was
                    # populated alongside it from the previous account)
                    # and re-resolve against the current account. The
                    # subsequent ensure_definitions call adopts an
                    # existing "Attention" def if one's already there,
                    # else creates a fresh one with the canonical tags.
                    previous_id = attention_state.attention_definition_id
                    _log.warning(
                        "extension POST: attention def %s does not exist on "
                        "current account; clearing state and re-resolving",
                        previous_id,
                    )
                    attention_state.attention_definition_id = None
                    attention_state.tag_ids = {}
                    try:
                        client.ensure_definitions(attention_state)
                    except Exception as exc:
                        _log.exception(
                            "extension POST: ensure_definitions failed during "
                            "stale-def recovery"
                        )
                        raise HTTPException(
                            502,
                            f"could not re-resolve attention definition: "
                            f"{type(exc).__name__}",
                        )
                    _att_state_mod.save(attention_state)
                    daemon.activity.add(
                        plugin_id="attention-relay",
                        summary=(
                            f"Attention def re-resolved: previous def "
                            f"{previous_id[:8]}… not present on this Fulcra "
                            f"account; now bound to "
                            f"{attention_state.attention_definition_id[:8]}…"
                        ),
                        ok=True,
                    )
                daemon._attention_def_validated_id = (
                    attention_state.attention_definition_id
                )
                daemon._attention_def_validated_at = now_mono

            # Lazy-create identity:<chrome_identity> tag if a new identity
            # appears. Mirrors the side effect the standalone relay had —
            # keeps the ingest_event tag list complete for first-time
            # identities. Failure is non-fatal: the event just lacks the
            # identity tag this round.
            identity = payload.get("chrome_identity")
            if identity:
                from fulcra_attention.fulcra import build_tag_name
                try:
                    tag_key = build_tag_name("identity", identity)
                except ValueError:
                    tag_key = None
                if tag_key and tag_key not in attention_state.tag_ids:
                    try:
                        client.ensure_tag(tag_key, attention_state)
                        _att_state_mod.save(attention_state)
                    except Exception as exc:
                        _log.warning(
                            "extension POST: lazy identity-tag create failed: %r",
                            exc,
                        )

            # Build the wire event and POST to Fulcra via the attention
            # FulcraClient, which already knows how to talk to /ingest.
            event = build_attention_event(payload, state=attention_state)
            try:
                client.ingest_batch([event])
            except Exception as exc:
                _log.warning("extension POST: Fulcra ingest failed: %r", exc)
                raise HTTPException(
                    502, f"ingest failed: {type(exc).__name__}",
                )

            # Update the per-client watermark + persist state. Same shape
            # as the old relay did. Best-effort — a failed state save
            # doesn't roll back the successful ingest.
            try:
                end_iso = _to_second_iso(payload["end_time"])
                cur = attention_state.watermarks.get(payload["client"])
                if cur is None or end_iso > cur:
                    attention_state.watermarks[payload["client"]] = end_iso
                    _att_state_mod.save(attention_state)
            except Exception:
                _log.exception("extension POST: watermark persist failed")

            # Surface in the dashboard activity feed via the daemon's
            # throttled note hook — coalesces bursts into one entry per
            # minute so the 50-entry ring isn't blown through during
            # active browsing. See Daemon.note_attention_event.
            try:
                daemon.note_attention_event(client=payload.get("client"))
            except Exception:
                # UI plumbing must never break the ingest path.
                _log.exception("extension POST: note_attention_event failed")

            return {"posted": 1, "dropped": 0}
        except HTTPException:
            raise
        except Exception:
            # Final backstop. The daemon must never 500-with-traceback on
            # a bad event — that's what the wrap-in-aggressive-try is for.
            _log.exception("extension POST: unexpected failure")
            raise HTTPException(500, "unexpected failure handling event")

    return app


def serve(daemon, *, host: str = "127.0.0.1", port: int | None = None) -> tuple[str, threading.Thread]:
    """Start the HTTP server in a background thread. Returns (url, thread).

    The daemon binds to a stable TCP port (default 9292; override via
    `[daemon] web_port` in config.toml). A stable port is essential for
    OAuth redirect URIs and the browser extension, both of which bake the
    URL into a third-party configuration that can't be re-read after a
    restart. If the port is in use we raise a clear RuntimeError instead
    of letting uvicorn fail somewhere deep in its bind path.

    The optional `port` keyword forces a specific port (used by tests);
    when None we read the value from Config so the daemon's chosen port
    is the source of truth.
    """
    import socket as _socket

    if port is None:
        port = daemon.config.web_port if hasattr(daemon, "config") else _config.DEFAULT_WEB_PORT

    # Probe the port before handing to uvicorn so a clash produces a
    # user-readable error instead of the cryptic OSError uvicorn would
    # raise from inside its event loop. Defense in depth — uvicorn would
    # also fail, but the message wouldn't tell the user how to fix it.
    #
    # SO_REUSEADDR on both the probe AND uvicorn: without it, a daemon
    # restart fails for ~60-90s while the OS holds the prior socket in
    # TIME_WAIT (Darwin-specific kernel behaviour). The probe inheriting
    # the option only proves we *could* bind — uvicorn must set it on
    # its own listening socket too, which it does via uvicorn.Config(
    # ... )'s underlying loop bind. Setting SO_REUSEADDR is safe here:
    # we only bind 127.0.0.1, so reuse can't grab traffic that wasn't
    # already ours.
    probe = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    probe.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
    # Darwin needs SO_REUSEPORT too for TIME_WAIT bypass — SO_REUSEADDR
    # alone won't let the probe rebind during the ~60-90s window after
    # the previous daemon's socket was closed. Linux behaviour is the
    # same. uvicorn's bind sets SO_REUSEADDR by default so the gap was
    # always at the probe layer.
    if hasattr(_socket, "SO_REUSEPORT"):
        try:
            probe.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEPORT, 1)
        except OSError:
            # Some platforms expose the constant but reject the setsockopt
            # (e.g. older kernels). Not fatal — fall back to REUSEADDR alone.
            pass
    try:
        try:
            probe.bind((host, port))
        except OSError as exc:
            raise RuntimeError(
                f"port {port} is in use; set [daemon] web_port = ... in "
                f"~/.config/fulcra-collect/config.toml"
            ) from exc
    finally:
        probe.close()

    app = build_app(daemon)
    config = uvicorn.Config(
        app=app, host=host, port=port, log_level="warning",
    )
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
