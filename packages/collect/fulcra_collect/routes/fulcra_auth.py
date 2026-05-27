"""Fulcra account authentication routes.

Two flows live here: the paste-token flow (POST/DELETE /api/fulcra/auth/token,
GET /api/fulcra/auth/status), and the CLI-delegated flow that shells out to
``fulcra auth login`` (GET /api/fulcra/auth/cli_status, POST
/api/fulcra/auth/cli_login).

Note on httpx access: this module reaches httpx via ``fulcra_collect.web``
so that test code can monkeypatch ``fulcra_collect.web.httpx`` and have
those patches take effect here too — that pattern predates this refactor
and a lot of test_web.py relies on it.
"""
from __future__ import annotations

import logging

from fastapi import Depends, FastAPI, HTTPException

from ._deps import FulcraTokenBody, RouteContext


def register(app: FastAPI, ctx: RouteContext) -> None:
    require_token = ctx.require_token

    @app.get("/api/fulcra/auth/status", dependencies=[Depends(require_token)])
    def fulcra_auth_status():
        from .. import credentials as _creds
        return {"authenticated": _creds.has_user_secret("bearer-token")}

    @app.post("/api/fulcra/auth/token", dependencies=[Depends(require_token)])
    def fulcra_auth_set(body: FulcraTokenBody):
        from .. import credentials as _creds
        from .. import web as _web  # late import so tests can monkeypatch web.httpx
        token = body.token.strip()
        if not token:
            raise HTTPException(400, "token is empty")
        # Validate against Fulcra before storing so typos are caught at
        # the door, not on first plugin run.
        _log = logging.getLogger("fulcra_collect.web")
        try:
            with _web.httpx.Client(timeout=10.0) as client:
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
        except _web.httpx.TimeoutException:
            raise HTTPException(
                504,
                "Fulcra didn't respond in time. Check your internet, then try again.",
            )
        except _web.httpx.HTTPError as exc:
            _log.exception("Fulcra token validation failed: %s", exc)
            raise HTTPException(502, f"Could not reach Fulcra: {type(exc).__name__}")
        _creds.set_user_secret("bearer-token", token)
        return {"ok": True}

    @app.delete("/api/fulcra/auth/token", dependencies=[Depends(require_token)])
    def fulcra_auth_clear():
        from .. import credentials as _creds
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
        from .. import credentials as _creds
        from .. import web as _web  # late import so tests can monkeypatch web.httpx

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
            with _web.httpx.Client(timeout=10.0) as client:
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
        except _web.httpx.TimeoutException:
            raise HTTPException(
                504,
                "Fulcra didn't respond in time during token validation.",
            )
        except _web.httpx.HTTPStatusError as exc:
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
        except _web.httpx.HTTPError as exc:
            _log.exception("Fulcra token validation (CLI path) failed: %s", exc)
            raise HTTPException(502, f"Could not reach Fulcra: {type(exc).__name__}")

        _creds.set_user_secret("bearer-token", token)
        return {"ok": True}
