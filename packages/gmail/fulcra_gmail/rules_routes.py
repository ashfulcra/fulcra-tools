"""The Gmail rule-builder routes (search / derive / preview / CRUD + the UI).

A gmail-specific surface mounted into the collect daemon's FastAPI app (mirrors
``collect_routes.py``), because the generic wizard's setting kinds can't model an
inbox-backed builder. Rules persist to ``plugin_settings.gmail.rules`` — the same
store the engine reads via ``ctx.config.get("rules")`` — so the poll/file/relay
engine is unchanged. Read-only Gmail access; no email content is ever logged.
"""
from __future__ import annotations

import logging

from fastapi import Body, Depends, HTTPException

from . import convert, rules_derive, rules_preview
from . import rules as rules_mod
from .accounts import AccountRegistry
from .client import GmailClient

_log = logging.getLogger("fulcra_gmail.rules_routes")

PLUGIN_ID = "gmail"
_SEARCH_PAGE = 25


def _registry() -> AccountRegistry:
    return AccountRegistry()


def _default_call_model():
    """Build a ``call_model(prompt)->str`` from the Anthropic SDK, or None.

    Returns None when no key/SDK is available; the route then 400s and the
    deterministic path is unaffected.
    """
    import os
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None
    try:
        from anthropic import Anthropic
    except Exception:  # noqa: BLE001
        return None
    client = Anthropic(api_key=key)

    def _call(prompt: str) -> str:
        msg = client.messages.create(
            model="claude-sonnet-5", max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
    return _call


def _header_record(msg: dict) -> dict:
    payload = msg.get("payload", {})
    return {
        "message_id": msg.get("id", ""),
        "from": convert.get_header(payload, "From") or "",
        "subject": convert.get_header(payload, "Subject") or "",
        "date": convert.get_header(payload, "Date") or "",
        "snippet": msg.get("snippet", ""),
        "list_id": convert.get_header(payload, "List-Id"),
        "has_attachment": convert.has_attachment(payload),
    }


def _load_rules_list(config_module) -> list[dict]:
    cfg = config_module.load()
    return list(cfg.plugin_settings.get(PLUGIN_ID, {}).get("rules", []) or [])


def _save_rules_list(config_module, ctx, rule_dicts: list[dict]) -> None:
    cfg = config_module.load()
    cfg.plugin_settings.setdefault(PLUGIN_ID, {})["rules"] = rule_dicts
    config_module.save(cfg)
    try:
        ctx.daemon.handle_request({"cmd": "reload"})
    except Exception:  # noqa: BLE001 — reload best-effort; persistence already done
        _log.warning("gmail rules: daemon reload after save failed (non-fatal)")


def register(app, ctx, *, registry_factory=None, client_factory=None,
             config_module=None, call_model_factory=None) -> None:
    from .rules_ui import RULES_UI_HTML  # Task 6 provides this; import lazily

    make_registry = registry_factory or _registry
    make_client = client_factory or (lambda account_id, registry: GmailClient(
        account_id, registry=registry))
    if config_module is None:
        from fulcra_collect import config as config_module  # noqa: PLW0127
    make_model = call_model_factory or _default_call_model

    guard = [Depends(ctx.require_token)]

    def _fetch(account_id: str, ids: list[str]) -> list[dict]:
        registry = make_registry()
        client = make_client(account_id, registry)
        out = []
        for mid in ids[:_SEARCH_PAGE]:
            msg = client.get_message(mid, format="full")
            if msg is not None:
                out.append(msg)
        return out

    @app.get("/api/gmail/rules", dependencies=guard)
    def list_rules():
        result = []
        for raw in _load_rules_list(config_module):
            try:
                (rule,) = rules_mod.parse_rules([raw])
            except ValueError:
                continue
            result.append({
                "id": rule.id, "version": rule.version, "name": rule.name,
                "summary": rules_mod.rule_summary(rule), "actions": rule.actions,
                "relay_to": rule.relay_to, "enabled": rule.enabled,
            })
        return {"rules": result}

    @app.get("/api/gmail/rules/accounts", dependencies=guard)
    def accounts():
        reg = make_registry()
        return {"accounts": [{"account_id": a.account_id, "email": a.email,
                              "status": a.status} for a in reg.list_accounts()]}

    @app.post("/api/gmail/rules/search", dependencies=guard)
    def search(body: dict = Body(...)):
        account_id = body["account_id"]
        q = body.get("q", "")
        registry = make_registry()
        client = make_client(account_id, registry)
        ids = client.list_message_ids(q)[:_SEARCH_PAGE]
        messages = [_header_record(m) for m in _fetch(account_id, ids)]
        return {"messages": messages, "next_page_token": None}

    @app.post("/api/gmail/rules/derive", dependencies=guard)
    def derive(body: dict = Body(...)):
        account_id = body["account_id"]
        pos = _fetch(account_id, body.get("positives", []))
        neg = _fetch(account_id, body.get("negatives", []))
        res = rules_derive.derive([_header_record(m) for m in pos],
                                  [_header_record(m) for m in neg])
        return {
            "chips": [vars(c) for c in res.chips],
            "draft_rule": res.draft_rule,
            "needs_refinement": res.needs_refinement,
        }

    @app.post("/api/gmail/rules/preview", dependencies=guard)
    def preview(body: dict = Body(...)):
        account_id = body["account_id"]
        rule_dict = body["rule"]
        try:
            (rule,) = rules_mod.parse_rules([rule_dict])
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        registry = make_registry()
        client = make_client(account_id, registry)
        q = rules_mod.build_query(rule)
        ids = client.list_message_ids(q)[:_SEARCH_PAGE]
        candidates = _fetch(account_id, ids)
        res = rules_preview.preview(
            rule_dict, candidates, account_id,
            positives=set(body.get("positives", [])),
            negatives=set(body.get("negatives", [])),
        )
        return {
            "match_count": res.match_count, "sample": res.sample,
            "positives_caught": res.positives_caught,
            "negatives_caught": res.negatives_caught,
        }

    def _validate(rule_dict: dict):
        try:
            (rule,) = rules_mod.parse_rules([rule_dict])
            return rule
        except ValueError as e:
            raise HTTPException(400, str(e)) from e

    @app.post("/api/gmail/rules", dependencies=guard)
    def create(body: dict = Body(...)):
        _validate(body)
        existing = _load_rules_list(config_module)
        if any(r.get("id") == body["id"] for r in existing):
            raise HTTPException(409, f"rule id {body['id']!r} already exists")
        rule = _validate(body)
        existing.append(rules_mod.rule_to_config_dict(rule))
        _save_rules_list(config_module, ctx, existing)
        return {"ok": True, "id": rule.id}

    @app.put("/api/gmail/rules/{rule_id}", dependencies=guard)
    def update(rule_id: str, body: dict = Body(...)):
        existing = _load_rules_list(config_module)
        idx = next((i for i, r in enumerate(existing) if r.get("id") == rule_id), None)
        if idx is None:
            raise HTTPException(404, f"unknown rule {rule_id!r}")
        prev = existing[idx]
        # Bump version when the matching criteria change (fresh processed set).
        match_fields = ("match", "from_regex", "subject_regex", "has_attachment")
        changed = any(body.get(f) != prev.get(f) for f in match_fields)
        if changed:
            body = {**body, "version": int(prev.get("version", 1)) + 1}
        rule = _validate(body)
        existing[idx] = rules_mod.rule_to_config_dict(rule)
        _save_rules_list(config_module, ctx, existing)
        return {"ok": True, "version": rule.version}

    @app.delete("/api/gmail/rules/{rule_id}", dependencies=guard)
    def delete(rule_id: str):
        existing = _load_rules_list(config_module)
        remaining = [r for r in existing if r.get("id") != rule_id]
        if len(remaining) == len(existing):
            raise HTTPException(404, f"unknown rule {rule_id!r}")
        _save_rules_list(config_module, ctx, remaining)
        return {"ok": True}

    @app.post("/api/gmail/rules/{rule_id}/enabled", dependencies=guard)
    def set_enabled(rule_id: str, body: dict = Body(...)):
        existing = _load_rules_list(config_module)
        idx = next((i for i, r in enumerate(existing) if r.get("id") == rule_id), None)
        if idx is None:
            raise HTTPException(404, f"unknown rule {rule_id!r}")
        existing[idx] = {**existing[idx], "enabled": bool(body.get("enabled", True))}
        _save_rules_list(config_module, ctx, existing)
        return {"ok": True}

    from . import rules_ai

    @app.post("/api/gmail/rules/ai-suggest", dependencies=guard)
    def ai_suggest(body: dict = Body(...)):
        if body.get("consent") is not True:
            raise HTTPException(403, "AI suggestion requires explicit consent")
        call_model = make_model()
        if call_model is None:
            raise HTTPException(400, "no AI backend configured (set ANTHROPIC_API_KEY)")
        account_id = body["account_id"]
        pos = [_header_record(m) for m in _fetch(account_id, body.get("positives", []))]
        neg = [_header_record(m) for m in _fetch(account_id, body.get("negatives", []))]
        try:
            return rules_ai.suggest(pos, neg, call_model=call_model)
        except ValueError as e:
            raise HTTPException(502, f"AI suggestion failed: {e}") from e

    @app.get("/api/gmail/rules/ui")
    def ui():
        from fastapi.responses import HTMLResponse
        return HTMLResponse(RULES_UI_HTML)
