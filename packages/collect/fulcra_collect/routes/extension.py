"""Browser-extension routes.

* ``POST /api/plugin/attention-relay/pair`` — one-click extension pairing.
  Generates a fresh extension-token, stashes it in the user-level keychain,
  and returns it (plus the daemon URL) so the wizard can postMessage it
  straight to the Fulcra Attention browser extension via a content script.
* ``POST /api/extension/attention`` — receives browse-attention events from
  the Fulcra Attention browser extension. Replaces the old standalone
  ``fulcra-attention`` relay on port 8771; that process is gone and the
  extension now points at this daemon's stable port.
"""
from __future__ import annotations

import logging
import secrets

from fastapi import Depends, FastAPI, HTTPException, Request

from .. import config as _config
from ._deps import RouteContext


def register(app: FastAPI, ctx: RouteContext) -> None:
    daemon = ctx.daemon
    require_token = ctx.require_token

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
        from .. import credentials as _creds
        from .. import web as _web  # for _web_url_path
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
            url_file = _web._web_url_path()
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
        from .. import credentials as _creds
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
                from .. import state as _collect_state_mod
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
