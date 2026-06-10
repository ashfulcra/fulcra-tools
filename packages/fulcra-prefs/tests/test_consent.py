from datetime import datetime, timezone
from fulcra_prefs.consent import filter_for_audience, disclosure_signal

NOW = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)

DOC = {"v": 1, "compiled_at": "2026-06-10T12:00:00+00:00",
       "keys": {"dining.cuisine.thai": {"value": True, "weight": 0.8},
                "health.sleep.target": {"value": 8, "weight": 1.0},
                "dining.noise.quiet":  {"value": True, "weight": 0.5}}}

def grant(glob="dining.*", audience="ea-agent", level="read", expires=None):
    return {"key_glob": glob, "audience": audience, "level": level,
            "granted_at": "2026-06-01T00:00:00+00:00", "expires": expires}

def test_filter_keeps_only_granted_keys():
    out = filter_for_audience(DOC, [grant()], "ea-agent", NOW)
    assert sorted(out["keys"]) == ["dining.cuisine.thai", "dining.noise.quiet"]

def test_no_grants_means_empty_doc():
    out = filter_for_audience(DOC, [], "ea-agent", NOW)
    assert out["keys"] == {}

def test_wrong_audience_gets_nothing():
    out = filter_for_audience(DOC, [grant(audience="other")], "ea-agent", NOW)
    assert out["keys"] == {}

def test_expired_grant_ignored():
    g = grant(expires="2026-06-09T00:00:00+00:00")
    assert filter_for_audience(DOC, [g], "ea-agent", NOW)["keys"] == {}

def test_disclosure_signal_records_what_was_shared():
    sig = disclosure_signal(["dining.cuisine.thai"], "ea-agent",
                            platform="claude-code", now=NOW)
    assert sig.kind == "consent"
    assert sig.key == "consent.disclosure.ea-agent"
    assert sig.value == {"keys": ["dining.cuisine.thai"], "audience": "ea-agent"}
    assert sig.observed_at == "2026-06-10T12:00:00+00:00"
