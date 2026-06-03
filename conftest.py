"""Workspace-wide pytest fixtures (monorepo root).

The macOS CI suite is required to run hermetically — "Must NOT hit the network"
(see .github/workflows/macos.yml). Several packages' tests construct a Fulcra
client whose auth path shells out to ``fulcra auth print-access-token`` whenever
``FULCRA_ACCESS_TOKEN`` is unset. That subprocess escapes httpx's MockTransport
and needs real credentials, so the tests pass locally for an authed developer
but fail on a clean CI runner ("No credentials found"). The new macOS CI
surfaced this latent, cross-package debt.

This autouse fixture defaults a dummy access token for every test in the
workspace, so the auth path is satisfied without ever shelling out. It only sets
a test-env default — it changes no package logic. Tests that specifically
exercise the token-fetch / shell-out path (e.g. fulcra-common's
``test_get_token_*``) ``delenv`` or override it within their own body; because
``monkeypatch`` is function-scoped and applies after this autouse fixture, those
tests still see exactly the environment they intend.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _default_fulcra_access_token(monkeypatch):
    monkeypatch.setenv("FULCRA_ACCESS_TOKEN", "test-access-token-ci")
