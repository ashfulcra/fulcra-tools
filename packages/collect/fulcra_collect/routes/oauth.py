"""OAuth flow — plugin-agnostic start + callback routes plus the small
HTML pages shown in the OAuth-redirect tab after success/failure."""
from __future__ import annotations

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import HTMLResponse

from ._deps import RouteContext


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


def register(app: FastAPI, ctx: RouteContext) -> None:
    daemon = ctx.daemon
    require_token = ctx.require_token

    @app.post("/api/oauth/{plugin_id}/start", dependencies=[Depends(require_token)])
    def oauth_start(plugin_id: str):
        plugin = daemon.registry.plugins.get(plugin_id)
        if plugin is None or plugin.oauth_handler is None:
            raise HTTPException(404, f"plugin {plugin_id!r} has no oauth_handler")
        from ..oauth import start_flow
        from .. import web as _web  # for _web_url_path
        # Prefer the URL held on the daemon object (set by serve() after the
        # socket is bound and uvicorn is ready). Fall back to the persisted
        # web-url file, then to the daemon's _web_url attribute if present,
        # then error — "http://localhost" (port-less) would cause Trakt to
        # redirect to port 80, where nothing listens.
        base_url: str | None = getattr(daemon, "_web_url", None)
        if not base_url:
            url_file = _web._web_url_path()
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
            from .. import credentials as _creds
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
        from ..oauth import complete_flow
        from .. import credentials as _creds
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
