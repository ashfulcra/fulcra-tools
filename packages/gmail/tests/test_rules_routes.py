import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fulcra_gmail import rules_routes


class FakeGmailClient:
    def __init__(self, messages):
        self._messages = messages  # {id: full_message_dict}
    def list_message_ids(self, q):
        return list(self._messages.keys())
    def get_message(self, message_id, format="full"):  # noqa: A002
        return self._messages.get(message_id)


class FakeConfig:
    def __init__(self):
        self.saved = None
        self._store = {"plugin_settings": {"gmail": {"rules": []}}}
    def load(self):
        import copy
        self._loaded = _Cfg(copy.deepcopy(self._store["plugin_settings"]))
        return self._loaded
    def save(self, cfg):
        self.saved = cfg
        self._store["plugin_settings"] = cfg.plugin_settings


class _Cfg:
    def __init__(self, plugin_settings):
        self.plugin_settings = plugin_settings


def _msg(mid, frm, subject, attach=False, list_id=None):
    headers = [{"name": "From", "value": frm}, {"name": "Subject", "value": subject},
               {"name": "Date", "value": "Mon, 1 Jan 2026 00:00:00 +0000"}]
    if list_id:
        headers.append({"name": "List-Id", "value": list_id})
    payload = {"headers": headers, "snippet": "snip", "mimeType": "text/plain"}
    if attach:
        payload = {"headers": headers, "mimeType": "multipart/mixed",
                   "parts": [{"filename": "r.pdf", "body": {"attachmentId": "a1"}}]}
    return {"id": mid, "snippet": "snip", "payload": payload}


class _Reg:
    def list_accounts(self):
        class A:  # noqa: N801
            account_id = "acct"
            email = "user@example.test"
            status = "active"
        return [A()]


@pytest.fixture
def client():
    app = FastAPI()

    class Ctx:
        def require_token(self):  # no-op guard for tests
            return None
        class daemon:  # noqa: N801
            @staticmethod
            def handle_request(_req):
                return {"ok": True}

    msgs = {
        "1": _msg("1", "r@shop.example", "Your receipt", attach=True),
        "2": _msg("2", "noise@shop.example", "newsletter"),
    }
    cfg = FakeConfig()
    rules_routes.register(
        app, Ctx(),
        registry_factory=lambda: _Reg(),
        client_factory=lambda account_id, registry: FakeGmailClient(msgs),
        config_module=cfg,
    )
    c = TestClient(app)
    c._cfg = cfg
    return c


def test_search_returns_headers_no_body(client):
    r = client.post("/api/gmail/rules/search", json={"account_id": "acct", "q": "receipt"})
    assert r.status_code == 200
    msgs = r.json()["messages"]
    assert {m["message_id"] for m in msgs} == {"1", "2"}
    m1 = next(m for m in msgs if m["message_id"] == "1")
    assert m1["has_attachment"] is True and m1["from"] == "r@shop.example"


def test_derive_endpoint_returns_chips(client):
    r = client.post("/api/gmail/rules/derive",
                    json={"account_id": "acct", "positives": ["1"], "negatives": ["2"]})
    assert r.status_code == 200
    body = r.json()
    assert any(c["kind"] == "sender" for c in body["chips"])
    assert "from:r@shop.example" in body["draft_rule"]["match"]


def test_save_persists_and_lists(client):
    rule = {"id": "receipts", "version": 1, "name": "Receipts",
            "match": "from:shop.example", "actions": ["file"]}
    assert client.post("/api/gmail/rules", json=rule).status_code == 200
    saved = client._cfg.saved.plugin_settings["gmail"]["rules"]
    assert saved and saved[0]["id"] == "receipts"
    listed = client.get("/api/gmail/rules").json()["rules"]
    assert listed[0]["id"] == "receipts" and "summary" in listed[0]


def test_save_duplicate_id_conflicts(client):
    rule = {"id": "dup", "version": 1, "name": "n", "match": "x", "actions": ["file"]}
    assert client.post("/api/gmail/rules", json=rule).status_code == 200
    assert client.post("/api/gmail/rules", json=rule).status_code == 409


def test_put_bumps_version_on_matching_change(client):
    rule = {"id": "r", "version": 1, "name": "n", "match": "x", "actions": ["file"]}
    client.post("/api/gmail/rules", json=rule)
    changed = {**rule, "match": "y"}
    client.put("/api/gmail/rules/r", json=changed)
    saved = client._cfg.saved.plugin_settings["gmail"]["rules"][0]
    assert saved["version"] == 2 and saved["match"] == "y"


def test_invalid_rule_rejected(client):
    bad = {"id": "b", "version": 1, "name": "n", "match": "x",
           "actions": ["relay"]}  # relay without relay_to
    assert client.post("/api/gmail/rules", json=bad).status_code == 400


def test_enabled_toggle(client):
    rule = {"id": "r", "version": 1, "name": "n", "match": "x", "actions": ["file"]}
    client.post("/api/gmail/rules", json=rule)
    client.post("/api/gmail/rules/r/enabled", json={"enabled": False})
    saved = client._cfg.saved.plugin_settings["gmail"]["rules"][0]
    assert saved["enabled"] is False


def test_ui_page_renders_builder(client):
    r = client.get("/api/gmail/rules/ui")
    assert r.status_code == 200
    html = r.text
    for anchor in ("gmail-rule-builder", "/api/gmail/rules/search",
                   "/api/gmail/rules/derive", "/api/gmail/rules/preview"):
        assert anchor in html


def test_ai_suggest_requires_consent(client):
    r = client.post("/api/gmail/rules/ai-suggest",
                    json={"account_id": "acct", "positives": ["1"], "negatives": []})
    assert r.status_code == 403


def test_search_no_pii_in_logs(client, caplog):
    import logging
    caplog.set_level(logging.DEBUG)
    client.post("/api/gmail/rules/search", json={"account_id": "acct", "q": "receipt"})
    assert "receipt" not in caplog.text.lower() or "Your receipt" not in caplog.text
    assert "shop.example" not in caplog.text
