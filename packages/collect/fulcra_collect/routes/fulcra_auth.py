"""Fulcra account authentication routes.

Three flows live here: the paste-token flow (POST/DELETE
/api/fulcra/auth/token, GET /api/fulcra/auth/status), the classic
CLI-delegated flow that shells out to a blocking ``fulcra auth login``
(GET /api/fulcra/auth/cli_status, POST /api/fulcra/auth/cli_login), and
the non-interactive split flow added for fulcra-api >= 0.1.35
(POST /api/fulcra/auth/cli_login_start + /cli_login_poll) where the web
UI renders the auth URL as a clickable link instead of relying on the
daemon subprocess opening a browser tab — which cannot work when the
daemon runs under launchd, over SSH, or headless.

Note on httpx access: this module reaches httpx via ``fulcra_collect.web``
so that test code can monkeypatch ``fulcra_collect.web.httpx`` and have
those patches take effect here too — that pattern predates this refactor
and a lot of test_web.py relies on it.
"""
from __future__ import annotations

import logging

from fastapi import Depends, FastAPI, HTTPException

from ._deps import CliDeviceCodeBody, FulcraTokenBody, RouteContext


def _mask_code(code: str) -> str:
    """Render a device/auth code safe for logs: first 4 chars + length."""
    return f"{code[:4]}…({len(code)} chars)"


def _redact_device_code(text: str, device_code: str) -> str:
    """Remove a device code if a CLI error echoes it back."""
    if not text or not device_code:
        return text
    return text.replace(device_code, _mask_code(device_code))


def _parse_get_auth_url_output(stdout: str) -> dict[str, str] | None:
    """Parse ``fulcra auth login --get-auth-url`` stdout.

    Live-verified shape (fulcra-api 0.1.35, 2026-07-06)::

        Open the web auth URL in a browser, verify the web auth code, ...

        Web auth URL: https://fulcra.us.auth0.com/activate?user_code=XXXX-YYYY
        - Web auth code: XXXX-YYYY
        - Device code: <opaque token>

        After finishing the web auth flow, complete authentication with ...

    Returns ``{auth_url, web_auth_code, device_code}`` or None when the
    output doesn't carry both an URL and a device code (the caller then
    degrades to the classic blocking flow rather than guessing).
    """
    auth_url = web_auth_code = device_code = None
    for raw in stdout.splitlines():
        line = raw.strip()
        if line.startswith("-"):
            line = line.lstrip("-").strip()
        lowered = line.lower()
        if lowered.startswith("web auth url:"):
            auth_url = line.split(":", 1)[1].strip()
        elif lowered.startswith("web auth code:"):
            web_auth_code = line.split(":", 1)[1].strip()
        elif lowered.startswith("device code:"):
            device_code = line.split(":", 1)[1].strip()
    if not auth_url or not device_code:
        return None
    return {
        "auth_url": auth_url,
        "web_auth_code": web_auth_code or "",
        "device_code": device_code,
    }


def _capture_validate_store_cli_token(cli_path: str) -> None:
    """Read the CLI's freshly-minted token, validate it against Fulcra,
    and store it in the same keychain slot the paste-token path uses.

    Shared tail of both CLI sign-in completions (classic blocking
    ``cli_login`` and the split ``cli_login_poll``) so their post-login
    behavior can never drift. Raises HTTPException on every failure.
    """
    import subprocess
    from .. import credentials as _creds
    from .. import web as _web  # late import so tests can monkeypatch web.httpx

    _log = logging.getLogger("fulcra_collect.web")

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
    # Same DEFAULT_BASE_URL routing as every other caller (fulcra_common
    # .client, web._RetryingClient) — this site used to hardcode prod
    # (P3 #18). Late import so tests can monkeypatch it.
    from fulcra_common import DEFAULT_BASE_URL
    try:
        with _web.httpx.Client(timeout=10.0) as client:
            r = client.get(
                f"{DEFAULT_BASE_URL}/user/v1alpha1/annotation",
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
    # SP5 task 1: clear any stale refresh-failed flag from before the
    # user signed back in. See the paste-token POST for rationale.
    _creds.clear_refresh_failed()


def register(app: FastAPI, ctx: RouteContext) -> None:
    require_token = ctx.require_token

    @app.get("/api/fulcra/auth/status", dependencies=[Depends(require_token)])
    def fulcra_auth_status():
        # ``refresh_failed`` (SP5 task 1) signals that the daemon tried to
        # mint a fresh access token via ``fulcra auth print-access-token``
        # and the CLI itself couldn't — typically because the CLI's
        # refresh token has also expired or the user is signed out of the
        # CLI. The web UI surfaces this as a "Reconnect to Fulcra" banner
        # in Settings (SP5 task 3) so the user knows synchronous
        # management calls (list/soft-delete annotation defs) will keep
        # failing until they re-sign-in.
        from .. import credentials as _creds
        return {
            "authenticated": _creds.has_user_secret("bearer-token"),
            "refresh_failed": _creds.is_refresh_failed(),
        }

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
        # DEFAULT_BASE_URL honors the FULCRA_API_BASE override (P3 #18).
        from fulcra_common import DEFAULT_BASE_URL
        try:
            with _web.httpx.Client(timeout=10.0) as client:
                r = client.get(
                    f"{DEFAULT_BASE_URL}/user/v1alpha1/annotation",
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
        # SP5 task 1: a successful interactive sign-in dismisses any
        # prior "refresh exhausted" state so the Settings banner goes
        # away the moment the user re-auths.
        _creds.clear_refresh_failed()
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
        import subprocess
        from .. import credentials as _creds

        # Resolve via _find_fulcra_cli (not bare shutil.which): the launchd
        # daemon's PATH excludes ~/.local/bin where `uv tool install` puts the
        # CLI, so which() alone reports it missing even when installed.
        cli_path = _creds._find_fulcra_cli()
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
        import subprocess
        from .. import credentials as _creds

        _log = logging.getLogger("fulcra_collect.web")

        # Resolve via _find_fulcra_cli (not bare shutil.which) — see cli_status.
        cli_path = _creds._find_fulcra_cli()
        if not cli_path:
            raise HTTPException(
                424,
                "The fulcra CLI is not on PATH. Install it with "
                "`uv tool install fulcra-api`, or sign in with a token instead.",
            )

        # `fulcra auth login` polls up to 120 s by default; give ourselves a
        # little headroom for browser startup + the user clicking through.
        _log.info("cli_login: starting classic blocking `fulcra auth login`")
        try:
            login = subprocess.run(
                [cli_path, "auth", "login"],
                capture_output=True, text=True, timeout=150,
            )
        except subprocess.TimeoutExpired:
            _log.warning("cli_login: `fulcra auth login` hit the 150 s timeout")
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
            _log.warning("cli_login: login failed rc=%d: %s", login.returncode, msg)
            raise HTTPException(400, f"Fulcra sign-in failed: {msg}")

        # Login succeeded — capture the token via print-access-token,
        # validate it, and store it (shared with cli_login_poll).
        _capture_validate_store_cli_token(cli_path)
        _log.info("cli_login: sign-in complete, token stored")
        return {"ok": True}

    # ------------------------------------------------------------------
    # Non-interactive split flow (fulcra-api >= 0.1.35).
    #
    # cli_login_start runs `fulcra auth login --get-auth-url`, which
    # prints the web-auth URL + verification code + device code and
    # exits immediately — no browser is opened by the daemon, so this
    # works under launchd / SSH / headless where the classic flow's
    # "the subprocess will open a tab" assumption silently fails. The
    # web UI renders the URL as a real link the *user's* browser opens.
    #
    # cli_login_poll completes the flow with `--device-code <code>`,
    # which polls (like the interactive login) until the user approves
    # in the browser, then stores the token via the exact same path as
    # the classic cli_login.
    # ------------------------------------------------------------------

    @app.post("/api/fulcra/auth/cli_login_start",
              dependencies=[Depends(require_token)])
    def fulcra_auth_cli_login_start():
        import subprocess
        from .. import credentials as _creds

        _log = logging.getLogger("fulcra_collect.web")

        cli_path = _creds._find_fulcra_cli()
        if not cli_path:
            raise HTTPException(
                424,
                "The fulcra CLI is not on PATH. Install it with "
                "`uv tool install fulcra-api`, or sign in with a token instead.",
            )

        # --get-auth-url exits immediately (no polling), so a short
        # timeout is enough — it only needs one round-trip to Auth0.
        try:
            r = subprocess.run(
                [cli_path, "auth", "login", "--get-auth-url"],
                capture_output=True, text=True, timeout=30,
            )
        except subprocess.TimeoutExpired:
            _log.warning("cli_login_start: --get-auth-url timed out")
            raise HTTPException(
                504, "The fulcra CLI didn't return a sign-in URL in time.",
            )

        if r.returncode != 0:
            blob = ((r.stderr or "") + "\n" + (r.stdout or "")).lower()
            # Feature-detect a pre-0.1.35 CLI: click rejects unknown flags
            # with "No such option"; argparse says "unrecognized arguments".
            if "no such option" in blob or "unrecognized argument" in blob:
                _log.info(
                    "cli_login_start: CLI lacks --get-auth-url (rc=%d); "
                    "signalling fallback to classic cli_login",
                    r.returncode,
                )
                raise HTTPException(
                    409,
                    "This fulcra CLI does not support --get-auth-url; "
                    "use the classic sign-in flow as a fallback. "
                    "(Upgrade with `uv tool upgrade fulcra-api`.)",
                )
            tail = (r.stderr or r.stdout or "").strip().splitlines()
            msg = tail[-1] if tail else f"fulcra auth login exit {r.returncode}"
            _log.warning(
                "cli_login_start: --get-auth-url failed rc=%d: %s",
                r.returncode, msg,
            )
            raise HTTPException(400, f"Could not start Fulcra sign-in: {msg}")

        parsed = _parse_get_auth_url_output(r.stdout)
        if parsed is None:
            # Exit 0 but output we can't parse — a future CLI may have
            # reshaped the text. Degrade to the classic flow instead of
            # guessing at codes.
            _log.warning(
                "cli_login_start: could not parse --get-auth-url output "
                "(%d bytes); signalling fallback to classic cli_login",
                len(r.stdout or ""),
            )
            raise HTTPException(
                409,
                "The fulcra CLI's sign-in output could not be understood; "
                "use the classic sign-in flow as a fallback.",
            )

        # Never log the device code (bearer-equivalent pre-approval) or
        # the full URL (embeds the web-auth code) above DEBUG-safe masks.
        _log.info(
            "cli_login_start: issued device authorization (device_code=%s)",
            _mask_code(parsed["device_code"]),
        )
        return {
            "auth_url": parsed["auth_url"],
            "web_auth_code": parsed["web_auth_code"],
            "device_code": parsed["device_code"],
            # The CLI doesn't print an expiry; Auth0 device codes are
            # short-lived, so give the UI a human hint rather than a
            # fabricated number.
            "expires_hint": (
                "The code is short-lived — if sign-in stalls, start again."
            ),
        }

    @app.post("/api/fulcra/auth/cli_login_poll",
              dependencies=[Depends(require_token)])
    def fulcra_auth_cli_login_poll(body: CliDeviceCodeBody):
        import subprocess
        from .. import credentials as _creds

        _log = logging.getLogger("fulcra_collect.web")

        cli_path = _creds._find_fulcra_cli()
        if not cli_path:
            raise HTTPException(
                424,
                "The fulcra CLI is not on PATH. Install it with "
                "`uv tool install fulcra-api`, or sign in with a token instead.",
            )

        device_code = body.device_code.strip()
        if not device_code:
            raise HTTPException(400, "device_code is empty")

        # `--device-code` polls (default ~120 s) until the browser flow is
        # approved — same posture as the classic blocking login, so reuse
        # its 150 s timeout. FastAPI runs this sync handler in the thread
        # pool, so the daemon's event loop is not blocked while we wait.
        _log.info(
            "cli_login_poll: completing sign-in (device_code=%s)",
            _mask_code(device_code),
        )
        try:
            login = subprocess.run(
                [cli_path, "auth", "login", "--device-code", device_code],
                capture_output=True, text=True, timeout=150,
            )
        except subprocess.TimeoutExpired:
            _log.warning("cli_login_poll: completion hit the 150 s timeout")
            raise HTTPException(
                504,
                "Sign-in didn't complete within 2 minutes. "
                "Try again, or sign in with a token.",
            )
        if login.returncode != 0:
            tail = (login.stderr or login.stdout or "").strip().splitlines()
            msg = tail[-1] if tail else f"fulcra auth login exit {login.returncode}"
            msg = _redact_device_code(msg, device_code)
            _log.warning(
                "cli_login_poll: completion failed rc=%d: %s",
                login.returncode, msg,
            )
            raise HTTPException(400, f"Fulcra sign-in failed: {msg}")

        _capture_validate_store_cli_token(cli_path)
        _log.info("cli_login_poll: sign-in complete, token stored")
        return {"ok": True}
