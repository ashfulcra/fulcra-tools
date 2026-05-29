"""Shared fake-httpx seam for the daemon / web tests.

Both test_daemon.py and test_web.py exercise the daemon's Fulcra-touching
methods (``_quick_record_list``, ``_record_annotation``, ``_delete_definition``).
Those route their requests through ``web._RetryingClient`` (the refresh-on-401
wrapper), so the fake httpx must be installed on BOTH module seams. This module
is the single source of truth they import.

It lives in a dedicated, uniquely-named module rather than in ``conftest.py``:
a bare ``from conftest import ...`` resolves ambiguously across the monorepo's
several per-package ``conftest.py`` files, so running the whole repo
(``uv run pytest packages/``) picked up the WRONG package's conftest and failed
to collect. A unique module name on the tests dir's sys.path avoids that.
"""
from __future__ import annotations


class FakeHttpxResponse:
    """Minimal stand-in for ``httpx.Response``: a fixed JSON body, a settable
    status code, and a no-op ``raise_for_status`` (the daemon checks the body,
    not HTTP status, on the happy path)."""

    status_code = 200

    def __init__(self, data):
        self._data = data

    def raise_for_status(self):  # noqa: D401 - mirrors httpx.Response API
        pass

    def json(self):
        return self._data


class FakeHttpxClient:
    """``httpx.Client`` stub that records calls and returns preset responses.

    Records every request into ``.requests`` (so tests can assert request count
    / dedupe behaviour), returns ``get_data`` for GETs, and ``{"ok": True}`` for
    POSTs. ``post_status`` sets the POST response's status code; ``post_exc``,
    if given, is raised from ``post`` instead (to drive the POST-error path).
    This is the superset of what test_daemon.py and test_web.py each needed."""

    def __init__(self, *, get_data=None, post_status=200, post_exc=None):
        self._get_data = get_data or []
        self._post_status = post_status
        self._post_exc = post_exc
        self.requests: list[dict] = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def get(self, url, **kw):
        self.requests.append({"method": "GET", "url": url, **kw})
        return FakeHttpxResponse(self._get_data)

    def post(self, url, **kw):
        self.requests.append({"method": "POST", "url": url, **kw})
        if self._post_exc is not None:
            raise self._post_exc
        resp = FakeHttpxResponse({"ok": True})
        resp.status_code = self._post_status
        return resp


def install_fake_httpx(monkeypatch, client=None, *, get_data=None,
                       post_exc=None, post_status=200):
    """Substitute a fake ``httpx`` onto BOTH the daemon and the web module
    seams, with non-``Client`` attributes falling through to the real ``httpx``.

    Pass an explicit ``client`` to introspect its ``.requests`` (or to use a
    bespoke client that raises on a specific verb); otherwise a
    :class:`FakeHttpxClient` is built from ``get_data`` / ``post_exc`` /
    ``post_status``. The (possibly freshly built) client is returned.

    Why both seams: the Fulcra-touching daemon methods (``_quick_record_list``,
    ``_record_annotation``, ``_delete_definition``) route their requests
    through ``web._RetryingClient`` (the refresh-on-401 wrapper), which resolves
    ``httpx`` via ``fulcra_collect.web.httpx`` — NOT the daemon's own top-level
    ``httpx`` import. A test that patched only ``daemon.httpx`` left ``web.httpx``
    pointing at the real library: with credentials mocked to a dummy token the
    inner client 401'd, ``_RetryingClient`` refreshed via the live ``fulcra``
    CLI, and the retry returned the developer's REAL account definitions. That
    broke data-specific assertions AND silently exercised the production API
    from the unit suite. Patching both seams keeps the fake in force wherever
    the call routes.

    Why fall-through: ``_delete_definition`` catches ``_web.httpx.HTTPStatusError``
    / ``ConnectError`` / ``ConnectTimeout`` / ``TimeoutException``. Those except
    clauses evaluate the attribute on the patched module, so the fake must
    expose the real exception classes or the handler itself raises
    ``AttributeError``.
    """
    if client is None:
        client = FakeHttpxClient(get_data=get_data, post_exc=post_exc,
                                 post_status=post_status)

    import httpx as _real_httpx
    import fulcra_collect.daemon as daemon_mod
    import fulcra_collect.web as web_mod

    class _Factory:
        # A class assigned as a class attribute is not a descriptor, so
        # ``fake.Client`` returns this class itself (no method binding) — exactly
        # what ``httpx.Client(...)`` call sites expect. ``__new__`` ignores the
        # kwargs the real client takes and always hands back our stub.
        def __new__(cls, **kw):
            return client

    class _FakeHttpx:
        Client = _Factory

        def __getattr__(self, name):
            return getattr(_real_httpx, name)

    fake = _FakeHttpx()
    monkeypatch.setattr(daemon_mod, "httpx", fake)
    monkeypatch.setattr(web_mod, "httpx", fake)
    return client
