"""Tests for the daemon-side OAuth PKCE state machine."""
from __future__ import annotations


def test_start_returns_unique_state_each_call():
    from fulcra_collect.oauth import start_flow
    s1, v1, c1 = start_flow("trakt", "http://localhost/cb")
    s2, v2, c2 = start_flow("trakt", "http://localhost/cb")
    assert s1 != s2
    assert v1 != v2
    assert c1 != c2


def test_code_challenge_is_sha256_of_verifier():
    import base64
    import hashlib
    from fulcra_collect.oauth import start_flow
    state, verifier, challenge = start_flow("trakt", "http://localhost/cb")
    expected = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).rstrip(b"=").decode("ascii")
    assert challenge == expected


def test_complete_returns_pending_state_for_matching_state():
    from fulcra_collect.oauth import start_flow, complete_flow
    state, verifier, _ = start_flow("trakt", "http://localhost/cb")
    pending = complete_flow(state)
    assert pending is not None
    assert pending.plugin_id == "trakt"
    assert pending.code_verifier == verifier


def test_complete_returns_none_for_unknown_state():
    from fulcra_collect.oauth import complete_flow
    pending = complete_flow("never-issued")
    assert pending is None


def test_complete_consumes_state_so_replay_fails():
    from fulcra_collect.oauth import start_flow, complete_flow
    state, _, _ = start_flow("trakt", "http://localhost/cb")
    first = complete_flow(state)
    assert first is not None
    second = complete_flow(state)
    assert second is None  # state already consumed


def test_expired_state_returns_none(monkeypatch):
    import time
    from fulcra_collect.oauth import start_flow, complete_flow, _STATE_TTL_SECONDS
    state, _, _ = start_flow("trakt", "http://localhost/cb")
    # Fast-forward time past TTL
    real_monotonic = time.monotonic
    monkeypatch.setattr(time, "monotonic",
                        lambda: real_monotonic() + _STATE_TTL_SECONDS + 1)
    assert complete_flow(state) is None
