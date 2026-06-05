"""Plugin operations — credentials, settings, contract, enable/disable,
run, health_check, check_permission, upload."""
from __future__ import annotations

import ipaddress
import logging
import os
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile

from .. import config as _config
from ._deps import RouteContext, SecretBody


# URL-kind settings (e.g. generic-rss `feed_url`) are user-typed strings
# that the daemon's plugins later fetch via httpx with redirects on. A
# raw `file://`, `gopher://`, or `http://169.254.169.254/...` would
# happily land in plugin code. The user owns the daemon, so this is
# self-SSRF in the strict threat model — but a malicious browser tab
# that's slipped past CSRF (which it shouldn't, given Bearer-auth) could
# in principle set the URL. Cheap belt-and-braces: reject anything but
# http/https schemes and reject private-range / link-local / loopback
# hosts at write time.
_ALLOWED_URL_SCHEMES = ("http", "https")


def _validate_url_setting(key: str, value: object) -> None:
    """Raise HTTPException(400) if `value` is not an http(s) URL pointing
    to a public host. Called only for settings whose `kind == "url"`."""
    if not isinstance(value, str) or not value:
        raise HTTPException(400, f"setting {key!r}: must be a non-empty URL string")
    try:
        parts = urlsplit(value)
    except ValueError as exc:
        raise HTTPException(400, f"setting {key!r}: malformed URL ({exc})") from exc
    if parts.scheme not in _ALLOWED_URL_SCHEMES:
        raise HTTPException(
            400,
            f"setting {key!r}: scheme {parts.scheme!r} not allowed "
            f"(must be one of {_ALLOWED_URL_SCHEMES})",
        )
    host = parts.hostname or ""
    if not host:
        raise HTTPException(400, f"setting {key!r}: URL missing host")
    # Reject loopback, link-local, and RFC1918 private hosts at write
    # time. Hosts that look like IPs but parse cleanly get inspected;
    # bare hostnames pass through (we can't resolve here without
    # blocking, and resolution-on-read is the plugin's job).
    try:
        ip = ipaddress.ip_address(host)
        if (ip.is_loopback or ip.is_link_local
                or ip.is_private or ip.is_reserved
                or ip.is_multicast):
            raise HTTPException(
                400,
                f"setting {key!r}: host {host!r} is not a public address",
            )
    except ValueError:
        # Not an IP literal — bare hostname, allowed. DNS-based SSRF
        # remains a residual risk if the user types e.g. "localtest.me"
        # but that's the user shooting themselves in the foot, not an
        # attack surface.
        pass


# Cap for /api/plugin/{id}/upload — generous because some takeouts (e.g.
# Spotify Extended Streaming History) can be multiple GB, but bounded so
# a buggy client can't fill the disk even though the route is loopback-only.
_UPLOAD_MAX_BYTES = 10 * 1024 * 1024 * 1024  # 10 GB

# Chunk size for streaming uploads to disk. Big enough to keep syscall
# overhead low, small enough that the per-request memory footprint stays flat.
_UPLOAD_CHUNK_BYTES = 64 * 1024


def register(app: FastAPI, ctx: RouteContext) -> None:
    daemon = ctx.daemon
    require_token = ctx.require_token
    require_plugin = ctx.require_plugin

    @app.post("/api/plugin/{plugin_id}/run", dependencies=[Depends(require_token)])
    def plugin_run(plugin_id: str):
        require_plugin(plugin_id)
        return daemon.handle_request({"cmd": "run", "plugin": plugin_id})

    @app.get("/api/plugin/{plugin_id}/credentials", dependencies=[Depends(require_token)])
    def plugin_credentials(plugin_id: str):
        require_plugin(plugin_id)
        return daemon.handle_request({"cmd": "credential_status", "plugin": plugin_id})

    @app.put("/api/plugin/{plugin_id}/credential/{key}", dependencies=[Depends(require_token)])
    def plugin_set_credential(plugin_id: str, key: str, body: SecretBody):
        require_plugin(plugin_id)
        return daemon.handle_request({
            "cmd": "set_credential",
            "plugin": plugin_id,
            "key": key,
            "secret": body.secret,
        })

    @app.delete("/api/plugin/{plugin_id}/credential/{key}", dependencies=[Depends(require_token)])
    def plugin_delete_credential(plugin_id: str, key: str):
        require_plugin(plugin_id)
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
        # Validate enum values for enum-kind settings + URL scheme/host
        # for url-kind settings. Enum gates user picks from a list;
        # URL gates self-SSRF — see _validate_url_setting above.
        for k, v in body.items():
            s = declared[k]
            if s.kind == "enum" and s.enum_values and v not in s.enum_values:
                raise HTTPException(400, f"setting {k!r}: value {v!r} not in {s.enum_values}")
            if s.kind == "url":
                _validate_url_setting(k, v)
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
            # Surface the canonical definition name so the wizard's
            # definition_picker can show "Create a new 'Journal' annotation
            # instead" instead of the generic "Create a new annotation".
            # Lets first-time users (whose account has no def of this
            # plugin's canonical name yet) see what they'll get.
            "canonical_definition_name": plugin.canonical_definition_name,
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
                    "annotation_type": s.annotation_type,
                    "condition": (
                        {k: list(v) for k, v in s.condition.items()}
                        if s.condition else None
                    ),
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
        from .. import credentials as _creds
        from .. import state as _state_mod
        from ..plugin import RunContext
        ctx_credentials = {}
        for c in plugin.required_credentials:
            # Read each credential from the SAME scope the plugin actually
            # uses: user-level ("fulcra-collect:user") for user_level creds,
            # plugin-level otherwise. Mirrors _credential_status / the
            # set/delete routes so a health_check that reads a user_level
            # credential (e.g. a shared account token) sees the real value
            # instead of an always-empty plugin-scoped read.
            val = (
                _creds.get_user_secret(c.key)
                if getattr(c, "user_level", False)
                else _creds.get_secret(plugin_id, c.key)
            )
            if val is not None:
                ctx_credentials[c.key] = val
        ctx_config = dict(_config.load().plugin_settings.get(plugin_id, {}))
        run_ctx = RunContext(
            plugin_id=plugin_id,
            config=ctx_config,
            credentials=ctx_credentials,
            state=_state_mod.load(plugin_id),
            log=logging.getLogger(f"fulcra_collect.health.{plugin_id}"),
            _emit=lambda evt: None,
        )
        try:
            result = plugin.health_check(run_ctx)
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
        from .. import credentials as _creds
        from .. import state as _state_mod
        from ..plugin import RunContext
        ctx_credentials = {}
        for c in plugin.required_credentials:
            # Read each credential from the scope the plugin uses — see the
            # health_check route above for the rationale.
            val = (
                _creds.get_user_secret(c.key)
                if getattr(c, "user_level", False)
                else _creds.get_secret(plugin_id, c.key)
            )
            if val is not None:
                ctx_credentials[c.key] = val
        cfg = _config.load()
        run_ctx = RunContext(
            plugin_id=plugin_id,
            config=cfg.plugin_settings.get(plugin_id, {}),
            credentials=ctx_credentials,
            state=_state_mod.load(plugin_id),
            log=logging.getLogger(f"fulcra_collect.permission.{plugin_id}"),
            _emit=lambda evt: None,
        )
        try:
            result = plugin.permission_check(run_ctx)
            granted = bool(result.get("granted", False))
            hint = result.get("hint")
            return {"granted": granted, "hint": hint}
        except Exception as exc:
            return {
                "granted": False,
                "hint": f"{type(exc).__name__}: {exc}",
            }
