"""Daemon UDS command ``delete_definition`` — mirrors the HTTP route
behaviour (clears plugin state, prunes favorites) but is reachable
via the local UDS socket so the menubar can call it.

Why this test exists: the menubar's preferences/annotations_tab.py
(SP2 Task 3) and popover quick-record "…" menu (SP2 Task 4) both
trigger soft-delete from non-HTTP surfaces. The shared Daemon method
introduced in this task is the single business-logic site; the HTTP
route delegates to it (so the existing HTTP-side tests in
``test_web.py`` continue to pass without modification).

Fixture pattern: the existing HTTP-route tests in
``test_web.py:_patch_fulcra_delete`` monkeypatch
``fulcra_collect.web.httpx`` so the route's late-imported ``_web``
reference resolves to the stub. The new daemon method reaches Fulcra
via the same ``fulcra_collect.web.httpx`` module attribute, so this
test file reuses that exact patching style.
"""
from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# Fixture: a Daemon wired up against a fake Fulcra
# ---------------------------------------------------------------------------

class _FakeFulcra:
    """Stub of the Fulcra HTTP surface the daemon method touches.

    Installed by ``daemon_with_fake_fulcra`` via monkeypatching
    ``fulcra_collect.web.httpx`` — the same site the existing HTTP
    route tests patch. Exposes ``set_delete_response(status_code)`` so
    each test can choose how Fulcra responds to the DELETE.
    """

    def __init__(self) -> None:
        self._delete_status = 204

    def set_delete_response(self, status_code: int) -> None:
        self._delete_status = status_code


@pytest.fixture
def daemon_with_fake_fulcra(collect_home, _in_memory_keyring, monkeypatch):
    """Build a Daemon against a temp ``collect_home`` with a fake Fulcra.

    Returns ``(daemon, fake_fulcra)``. The fake intercepts the daemon's
    HTTP DELETE call via the shared ``fulcra_collect.web.httpx``
    patching site so the daemon never reaches the network.
    """
    import fulcra_collect.credentials as _creds_mod
    import fulcra_collect.web as web_mod
    from fulcra_collect.daemon import Config, Daemon
    from fulcra_collect.registry import RegistryResult

    # The daemon's _delete_definition requires a stored bearer token
    # to proceed past the "not signed in" guard.
    _creds_mod.set_user_secret("bearer-token", "valid-token")

    fake = _FakeFulcra()

    class _FakeResponse:
        def __init__(self, code: int) -> None:
            self.status_code = code

        def raise_for_status(self) -> None:
            if self.status_code >= 400:
                import httpx as _h
                req = _h.Request("DELETE", "http://test")
                raise _h.HTTPStatusError(
                    f"{self.status_code}",
                    request=req,
                    response=_h.Response(self.status_code, request=req),
                )

    class _FakeClient:
        def __init__(self, **kw) -> None:  # noqa: ARG002
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a) -> None:  # noqa: ARG002
            pass

        def delete(self, path, **kw):  # noqa: ARG002
            return _FakeResponse(fake._delete_status)

    # Mirror _patch_fulcra_delete's shape — preserve the exception
    # types the daemon catches so the except clauses still bind.
    import httpx as _real_httpx
    monkeypatch.setattr(
        web_mod, "httpx",
        type("httpx", (), {
            "Client": _FakeClient,
            "HTTPStatusError": _real_httpx.HTTPStatusError,
            "ConnectError": _real_httpx.ConnectError,
            "ConnectTimeout": _real_httpx.ConnectTimeout,
            "TimeoutException": _real_httpx.TimeoutException,
            "HTTPError": _real_httpx.HTTPError,
        })(),
    )

    daemon = Daemon(registry=RegistryResult(plugins={}), config=Config())
    return daemon, fake


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_delete_definition_uds_command(daemon_with_fake_fulcra) -> None:
    """The handle_request branch routes to _delete_definition."""
    daemon, fake_fulcra = daemon_with_fake_fulcra
    fake_fulcra.set_delete_response(204)
    response = daemon.handle_request({
        "cmd": "delete_definition",
        "def_id": "fake-uuid-1234",
    })
    assert response.get("ok") is True
    assert "cleared_plugins" in response


def test_delete_definition_uds_command_missing_def_id(
    daemon_with_fake_fulcra,
) -> None:
    """Missing def_id is a client-side error, returned as ok=False."""
    daemon, _ = daemon_with_fake_fulcra
    response = daemon.handle_request({"cmd": "delete_definition"})
    assert response.get("ok") is False
    assert response.get("code") == "bad_request"
    assert "def_id" in response.get("error", "").lower()


def test_delete_definition_uds_command_fulcra_404(
    daemon_with_fake_fulcra,
) -> None:
    """When Fulcra returns 404, the UDS response surfaces it gracefully."""
    daemon, fake_fulcra = daemon_with_fake_fulcra
    fake_fulcra.set_delete_response(404)
    response = daemon.handle_request({
        "cmd": "delete_definition",
        "def_id": "nonexistent-uuid",
    })
    assert response.get("ok") is False
    assert response.get("code") == "not_found"
    assert "not found" in response.get("error", "").lower()
