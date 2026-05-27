"""Static-asset serving — confirms cache headers force revalidation.

Why this test exists: FastAPI's StaticFiles defaults emit ETag and
last-modified but no Cache-Control, so Chrome happily serves the
cached body on conditional GETs even after the disk file changes. We
shipped a few sessions where frontend edits silently weren't visible
to a returning browser until the user did a hard reload. Adding
Cache-Control: no-cache makes the browser revalidate on every request.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from fulcra_collect.daemon import Daemon, Config
from fulcra_collect.registry import RegistryResult
from fulcra_collect.web import build_app, _ensure_token


@pytest.fixture
def web_app_client(collect_home) -> TestClient:
    """Mirror the inline ``_client`` helper from test_web.py.

    Built as a fixture here because the static-asset test only needs a
    plain client — no daemon registry munging, no auth header required
    (static mount is unauthenticated). Reusing collect_home so the
    daemon's on-disk state lands in a temp dir like every other test.
    """
    _ensure_token()
    daemon = Daemon(registry=RegistryResult(plugins={}), config=Config())
    app = build_app(daemon)
    return TestClient(app)


def test_static_asset_has_no_cache_header(web_app_client: TestClient) -> None:
    response = web_app_client.get("/static/wizard.js")
    assert response.status_code == 200
    assert response.headers.get("cache-control") == "no-cache"
