"""Live integration smoke test. Skipped unless FULCRA_PREFS_LIVE_SMOKE=1.

Run manually with:
    FULCRA_PREFS_LIVE_SMOKE=1 uv run --package fulcra-prefs pytest -k live_smoke -v

These tests hit the real Fulcra API with real credentials and are intentionally
excluded from CI. They verify the end-to-end wiring (credentials → FulcraAPI →
FulcraStore → read/write/ingest) works in production, not just against fakes.
"""
import os
import pytest
from datetime import datetime, timezone

pytestmark = pytest.mark.skipif(
    not os.environ.get("FULCRA_PREFS_LIVE_SMOKE"),
    reason="live smoke disabled (set FULCRA_PREFS_LIVE_SMOKE=1 to run)",
)


def _load_real_api():
    """Return a real FulcraAPI wired with persisted credentials, or None."""
    try:
        from fulcra_api.core import FulcraAPI
        from fulcra_api.cli import load_creds, save_creds
    except ImportError:
        return None
    creds = load_creds()
    if creds is None:
        return None
    return FulcraAPI(credentials=creds, refresh_callback=save_creds)


@pytest.fixture(scope="module")
def live_store():
    api = _load_real_api()
    if api is None:
        pytest.skip("no fulcra credentials found — run `fulcra auth login` first")
    from fulcra_prefs.store import FulcraStore
    return FulcraStore(api)


def test_live_file_roundtrip(live_store):
    """Write a timestamped value to prefs/smoke-test.json and read it back."""
    now = datetime.now(timezone.utc).isoformat()
    payload = {"smoke": True, "written_at": now}
    live_store.write_json("prefs/smoke-test.json", payload)
    read_back = live_store.read_json("prefs/smoke-test.json")
    assert read_back is not None, "read_json returned None after a successful write"
    assert read_back["written_at"] == now
    assert read_back["smoke"] is True


def test_live_capture_signal_no_exception(live_store):
    """If prefs/meta.json exists, capture a smoke signal and assert no exception.

    If meta.json is absent the user has not onboarded on this account, and we
    skip rather than fail — the roundtrip test above already exercised the full
    file API path.
    """
    meta = live_store.read_json("prefs/meta.json")
    if meta is None:
        pytest.skip("not onboarded on this account — run `fulcra-prefs onboard` first")
    now = datetime.now(timezone.utc)
    from fulcra_prefs.schema import Signal, temp_signal_id
    observed = now.isoformat()
    key = "fulcra-prefs.smoke.live-roundtrip"
    sig = Signal(
        id=temp_signal_id(key, observed, "live-smoke"),
        kind="preference", key=key, scope="global",
        value={"smoke": True},
        strength=0.1, confidence=1.0, half_life_days=1.0,
        observed_at=observed, platform="live-smoke",
        agent=None, session=None, supersedes=None,
    )
    # Must not raise — this is the end-to-end ingest path.
    live_store.ingest_signal(sig, data_type=meta["data_type"])


def test_live_ingested_signal_is_readable_via_get_records(live_store):
    """Pins the real get-records shape Feature 1 depends on: a signal ingested
    to our definition must come back through read_signal_records and parse. If
    this fails, the payload field assumption (data/note) in
    store.read_signal_records is wrong for the live API — fix it there, not by
    loosening the test. Tier-2 capture visibility rides on this round-trip."""
    meta = live_store.read_json("prefs/meta.json")
    if meta is None:
        pytest.skip("not onboarded on this account — run `fulcra-prefs onboard` first")
    sigs = live_store.read_signal_records(meta["definition_id"])
    # The smoke signal ingested above (or any prior real capture) should appear.
    assert any(s.key.startswith("fulcra-prefs.smoke.") or s.kind == "preference"
               for s in sigs), (
        "read_signal_records returned nothing parseable for our definition — "
        "check the get-records payload field mapping in store.read_signal_records")
