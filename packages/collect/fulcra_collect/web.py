"""HTTP server that fronts the daemon via JSON API + static frontend.

Bound to 127.0.0.1 only on a stable port (default 9292; override via
`[daemon] web_port = N` in config.toml). A stable port is what lets OAuth
redirect URIs stay valid across daemon restarts (otherwise every plugin's
OAuth registration breaks when the daemon restarts).

Writes the resulting URL to ~/.config/fulcra-collect/web-url so the
menubar and ad-hoc tools can keep using the file (it's now just the
same URL every time).

Auth: a Bearer token from ~/.config/fulcra-collect/web-token (0600)
seeded into a cookie on the initial HTML load.

The HTTP routes themselves live in the :mod:`fulcra_collect.routes`
sub-package — one module per coherent slice (status, plugins, oauth,
definitions, …). :func:`build_app` is now a thin orchestrator that builds
the FastAPI app, wires up the shared dependencies (auth, the Fulcra
client factory), and calls each route module's ``register(app, ctx)``.

Why ``httpx`` is still imported at module scope even though the routes
that use it live elsewhere: a number of tests monkeypatch
``fulcra_collect.web.httpx`` to stub the Fulcra API. The route modules
reach httpx via this module specifically so those patches continue to
work without per-test changes.
"""
from __future__ import annotations

import os
import secrets
import threading
import time
from pathlib import Path

import httpx  # noqa: F401 — re-exported for monkeypatching in tests; see module docstring
import uvicorn
from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.responses import FileResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles

from . import config as _config
from ._resources import docs_dir as _resources_docs_dir
from ._resources import frontend_dir as _resources_frontend_dir
# RouteContext is used directly below in build_app(); the Pydantic body
# models (DefinitionBindBody, FulcraTokenBody, etc.) used to be re-exported
# here for back-compat but had no remaining importers — route modules
# import them straight from `routes._deps`. Test code that monkeypatches
# RouteContext for refresh-flow assertions targets `_deps.RouteContext`
# and this `web.RouteContext`; keep the import so both stay live.
from .routes._deps import RouteContext


def _web_token_path() -> Path:
    return _config.config_dir() / "web-token"


# ---------------------------------------------------------------------------
# Refresh-aware Fulcra HTTP client (SP5 task 1 / 2)
# ---------------------------------------------------------------------------
#
# ``_RetryingClient`` lives at module scope (rather than nested inside
# :func:`fulcra_http_client`'s closure) so that
# :meth:`fulcra_collect.daemon.Daemon._delete_definition` — which is reached
# via the local UDS socket, not through the FastAPI routing layer — can
# import + instantiate the same wrapper. Both call sites need identical
# refresh-on-401 behaviour; lifting the class avoids duplicating the retry
# plumbing in daemon.py.
#
# The wrapper deliberately reaches httpx via ``fulcra_collect.web.httpx``
# (the module-level attribute imported above), NOT via a captured ``httpx``
# at class-definition time. A number of tests monkeypatch
# ``fulcra_collect.web.httpx`` to substitute a fake HTTP client; reading
# the attribute lazily inside ``__init__`` is what makes the patch win for
# both the route side and the daemon side.

class _RetryingClient:
    """httpx.Client wrapper that refreshes-and-retries on 401.

    Forwards GET/POST/PUT/DELETE/PATCH/HEAD through a wrapping shim that,
    on a 401 response, invokes
    :func:`fulcra_collect.credentials.refresh_fulcra_access_token` to get
    a fresh access token from the CLI's refresh-token store, swaps the
    ``Authorization`` header on the inner client, and retries the same
    request once. If the retry also fails (refresh helper returned None),
    the original 401 response is returned and the credentials module's
    ``_refresh_failed`` flag is already set so the web UI can show a
    Reconnect banner via ``/api/fulcra/auth/status``.

    Every other attribute (``headers``, ``close``, the context-manager
    protocol, etc.) passes through to the inner client unchanged so
    existing callers see no behaviour difference.

    Constructor takes a ``user_agent`` so the route side
    (``fulcra-collect/web-ui``) and the daemon side
    (``fulcra-collect/daemon``) keep their distinct identifiers — useful
    for log triage when something goes wrong upstream.
    """

    # Wrapped HTTP-verb methods. httpx.Client.request() and .stream() are
    # DELIBERATELY NOT in this set — current Fulcra-API call sites only use
    # the named-verb shortcuts, and adding generic .request() interception
    # would require parsing the method out of the *args (since it's the
    # first positional arg, not a method name on the client). If a future
    # caller needs request()/stream() with refresh-on-401, add to the
    # whitelist below AND extend _wrap to handle the method-in-args shape.
    _METHODS = ("get", "post", "put", "delete", "patch", "head")

    def __init__(
        self,
        token: str,
        *,
        user_agent: str = "fulcra-collect/web-ui",
    ) -> None:
        from fulcra_common import DEFAULT_BASE_URL
        # Reach httpx via this module's attribute so tests that monkeypatch
        # ``fulcra_collect.web.httpx`` get their stub used — see the module
        # comment above. Local import keeps the lookup lazy.
        import fulcra_collect.web as _self

        self._inner = _self.httpx.Client(
            base_url=DEFAULT_BASE_URL,
            timeout=15.0,
            headers={
                "Authorization": f"Bearer {token}",
                "User-Agent": user_agent,
            },
            follow_redirects=True,
        )

    # --- context-manager protocol ----------------------------------
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return self._inner.__exit__(*exc)

    def close(self) -> None:
        self._inner.close()

    # --- 401-retry plumbing ---------------------------------------
    def _retry_with_fresh_token(self, method: str, *args, **kwargs):
        """Refresh via CLI, swap auth header, retry the request once.

        Returns the retry response, or ``None`` if the refresh helper
        itself failed (CLI missing, exhausted, etc.) — in that case the
        caller returns the original 401 and the process-level
        ``_refresh_failed`` flag is already set.
        """
        from . import credentials as _creds

        fresh = _creds.refresh_fulcra_access_token()
        if not fresh:
            return None
        self._inner.headers["Authorization"] = f"Bearer {fresh}"
        return getattr(self._inner, method)(*args, **kwargs)

    def _wrap(self, method: str):
        def wrapped(*args, **kwargs):
            response = getattr(self._inner, method)(*args, **kwargs)
            if response.status_code == 401:
                retry_resp = self._retry_with_fresh_token(
                    method, *args, **kwargs,
                )
                if retry_resp is not None:
                    return retry_resp
            return response

        return wrapped

    def __getattr__(self, name: str):
        # Forward GET/POST/PUT/DELETE/etc. — these are the call sites that
        # need 401-retry. Other attributes (e.g. ``.headers``) pass through
        # to the inner client directly.
        if name in self._METHODS:
            return self._wrap(name)
        return getattr(self._inner, name)


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


# uvicorn only accepts these level names; anything else makes
# uvicorn.Config raise KeyError and kills web-server startup. The root
# logger (cli._configure_logging) tolerates a bad FULCRA_COLLECT_LOG_LEVEL
# by falling back to INFO, so this consumer must too — a debug knob that
# can crash the daemon defeats the diagnosability this logging is for.
_UVICORN_LOG_LEVELS = {"critical", "error", "warning", "info", "debug", "trace"}


def _uvicorn_log_level() -> str:
    level = os.environ.get("FULCRA_COLLECT_LOG_LEVEL", "INFO").lower()
    return level if level in _UVICORN_LOG_LEVELS else "info"


def _frontend_dir() -> Path:
    # Dev: <workspace>/packages/web-ui/dist. Frozen: <Resources>/web-ui/dist.
    # _resources owns the branch so this stays a thin alias.
    return _resources_frontend_dir()


def _docs_dir() -> Path:
    # Dev: <workspace>/docs. Frozen: <Resources>/docs.
    return _resources_docs_dir()


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def build_app(daemon) -> FastAPI:
    """Construct the FastAPI app with the daemon injected.

    The body here owns three things:

    1. The auth token + dependency (``require_token``) and the small
       ``require_plugin`` 404-on-unknown-id helper that every plugin
       route shares.
    2. The Fulcra-client factory used by the definitions / delete-def
       routes. We build it as a closure rather than letting each route
       module import it because the token lookup has to be deferred
       (it isn't known until the user signs in).
    3. The frontend root (``/``) + static mount. ``/`` is special — it
       sets the cookie that bootstraps the SPA's auth — so it lives
       here rather than in a route module.

    Everything else is registered by the route modules in
    :mod:`fulcra_collect.routes`.
    """
    app = FastAPI(title="Fulcra Collect")
    token = _ensure_token()
    bearer = HTTPBearer(auto_error=False)

    def require_token(creds: HTTPAuthorizationCredentials = Depends(bearer)):
        # Use secrets.compare_digest to prevent timing-based token oracle attacks.
        if creds is None or not secrets.compare_digest(creds.credentials, token):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "auth required")

    def require_plugin(plugin_id: str) -> None:
        """Translate an unknown plugin id into a proper HTTP 404 so frontend
        code can rely on the status code (task #83). Routes that just
        delegate to daemon.handle_request() previously returned 200 with
        ``{ok: false, error: ...}`` for unknown ids — same shape the
        internal control-socket protocol uses, but inappropriate over HTTP.
        """
        if plugin_id not in daemon.registry.plugins:
            raise HTTPException(404, f"unknown plugin {plugin_id!r}")

    def fulcra_token_or_401() -> str:
        """Return the shared user-level Fulcra bearer token, or raise 401."""
        from . import credentials as _creds
        token_val = _creds.get_user_secret("bearer-token")
        if not token_val:
            raise HTTPException(401, "Fulcra not authenticated — set a bearer token first")
        return token_val

    def fulcra_http_client(fulcra_token: str):
        """Return a refresh-aware httpx.Client-like pre-configured for Fulcra.

        Thin factory; the real wrapper lives at module scope as
        :class:`_RetryingClient` so the daemon's UDS-side soft-delete path
        (``Daemon._delete_definition``) can construct the same kind of
        client without duplicating the retry plumbing — see SP5 task 2.

        On a 401 the wrapper invokes
        :func:`fulcra_collect.credentials.refresh_fulcra_access_token` to
        mint a fresh access token via the ``fulcra`` CLI's refresh-token
        store, swaps the Authorization header, and retries once. If the
        retry also fails the original 401 is returned and the credentials
        module's ``_refresh_failed`` flag is set so the web UI can show
        a Reconnect banner via ``/api/fulcra/auth/status`` (SP5 task 3).
        """
        return _RetryingClient(fulcra_token, user_agent="fulcra-collect/web-ui")

    ctx = RouteContext(
        daemon=daemon,
        require_token=require_token,
        require_plugin=require_plugin,
        fulcra_token_or_401=fulcra_token_or_401,
        fulcra_http_client=fulcra_http_client,
    )

    # ------------------------------------------------------------------
    # Frontend root — kept inline because it's the only HTML route and
    # it has to set the cookie that bootstraps the SPA's bearer auth.
    # ------------------------------------------------------------------

    @app.get("/")
    def root():
        idx = _frontend_dir() / "index.html"
        if not idx.exists():
            return {"error": "web UI not built", "expected_at": str(idx)}
        resp = FileResponse(str(idx))
        resp.set_cookie("fulcra_token", token, httponly=False,
                         samesite="strict", secure=False, path="/")
        # no-store, not just no-cache: this response carries the
        # Set-Cookie that bootstraps the SPA's auth. FileResponse otherwise
        # ships an ETag with no Cache-Control, so a reload could be served
        # from cache/bfcache without ever re-applying the cookie — leaving
        # a tab with a stale fulcra_token stuck on "auth required" even
        # after the user reloads. no-store forces a fresh GET (and thus a
        # fresh cookie) on every load, including the error screen's Reload.
        resp.headers["Cache-Control"] = "no-store"
        return resp

    static_dir = _frontend_dir() / "static"
    if static_dir.exists():
        # StaticFiles defaults: ETag + last-modified, no Cache-Control.
        # Chrome serves the cached body on conditional GETs even when
        # the disk file changes, so frontend edits silently don't reach
        # an already-open tab until a hard reload. Wrap the mount in a
        # tiny ASGI middleware that forces revalidation. The 304 path
        # still works — browsers honour ETag with Cache-Control: no-cache,
        # they just always make the round-trip.
        _static_app = StaticFiles(directory=str(static_dir))

        async def _no_cache_static(scope, receive, send):
            async def _send_with_no_cache(message):
                if message["type"] == "http.response.start":
                    headers = list(message.get("headers", []))
                    headers = [
                        (k, v) for (k, v) in headers
                        if k.lower() != b"cache-control"
                    ]
                    headers.append((b"cache-control", b"no-cache"))
                    message = {**message, "headers": headers}
                await send(message)
            await _static_app(scope, receive, _send_with_no_cache)

        app.mount("/static", _no_cache_static, name="static")

    # ------------------------------------------------------------------
    # Register the per-area route modules.
    # ------------------------------------------------------------------
    from .routes import (
        activity,
        annotations,
        definitions,
        docs,
        fulcra_auth,
        menubar,
        oauth,
        plugins,
        status as status_routes,
    )
    for module in (
        status_routes,
        plugins,
        definitions,
        fulcra_auth,
        oauth,
        activity,
        docs,
        annotations,
        menubar,
    ):
        module.register(app, ctx)

    return app


def serve(daemon, *, host: str = "127.0.0.1", port: int | None = None) -> tuple[str, threading.Thread]:
    """Start the HTTP server in a background thread. Returns (url, thread).

    The daemon binds to a stable TCP port (default 9292; override via
    `[daemon] web_port` in config.toml). A stable port is essential for
    OAuth redirect URIs, which bake the URL into a third-party
    configuration that can't be re-read after a restart. If the port is in
    use we raise a clear RuntimeError instead of letting uvicorn fail
    somewhere deep in its bind path.

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
    # log_level was "warning", which suppressed uvicorn's INFO access log
    # and startup lines — so a request that 401'd (e.g. a stale web-token
    # cookie during onboarding) left no trace in daemon.out.log. INFO
    # gives us per-request access logging; override via the same
    # FULCRA_COLLECT_LOG_LEVEL env var the root logger honours.
    config = uvicorn.Config(
        app=app, host=host, port=port, log_level=_uvicorn_log_level(),
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
