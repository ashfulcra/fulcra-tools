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

def test_default_purpose_is_read_includes_read_grant():
    # Back-compat: no purpose arg behaves as read; a read grant exposes keys.
    out = filter_for_audience(DOC, [grant(level="read")], "ea-agent", NOW)
    assert sorted(out["keys"]) == ["dining.cuisine.thai", "dining.noise.quiet"]

def test_read_purpose_includes_solve_grant():
    # solve >= read: a solve-level grant is also readable.
    out = filter_for_audience(DOC, [grant(level="solve")], "ea-agent", NOW,
                              purpose="read")
    assert sorted(out["keys"]) == ["dining.cuisine.thai", "dining.noise.quiet"]

def test_solve_purpose_includes_solve_grant():
    out = filter_for_audience(DOC, [grant(level="solve")], "ea-agent", NOW,
                              purpose="solve")
    assert sorted(out["keys"]) == ["dining.cuisine.thai", "dining.noise.quiet"]

def test_solve_purpose_excludes_read_only_grant():
    # A read grant must NOT feed the solver.
    out = filter_for_audience(DOC, [grant(level="read")], "ea-agent", NOW,
                              purpose="solve")
    assert out["keys"] == {}

def test_solve_purpose_excludes_grant_missing_level():
    # Legacy/partial grant without 'level' defaults to read -> no solve capability.
    g = {"key_glob": "dining.*", "audience": "ea-agent",
         "granted_at": "2026-06-01T00:00:00+00:00", "expires": None}
    out = filter_for_audience(DOC, [g], "ea-agent", NOW, purpose="solve")
    assert out["keys"] == {}

def test_invalid_purpose_raises():
    import pytest
    with pytest.raises(ValueError):
        filter_for_audience(DOC, [grant()], "ea-agent", NOW, purpose="bogus")

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

def test_disclosure_signal_disambiguates_same_instant_different_keys():
    # Two disclosures to the same audience/platform at the same instant but
    # covering different keys are distinct Privacy Ledger entries. Their signal
    # ids (the record identity) must differ, or the store collapses one away.
    a = disclosure_signal(["dining.cuisine.thai"], "ea-agent", "ios", NOW)
    b = disclosure_signal(["health.weight", "health.steps"], "ea-agent", "ios", NOW)
    assert a.id != b.id

def test_disclosure_signal_id_stable_for_same_disclosure():
    a = disclosure_signal(["a", "b"], "ea-agent", "ios", NOW)
    b = disclosure_signal(["b", "a"], "ea-agent", "ios", NOW)  # value keys sorted
    assert a.id == b.id

def test_naive_expires_string_treated_as_utc_not_crash():
    live = grant(expires="2099-01-01T00:00:00")      # naive, far future
    dead = grant(expires="2020-01-01T00:00:00")      # naive, past
    assert filter_for_audience(DOC, [live], "ea-agent", NOW)["keys"] != {}
    assert filter_for_audience(DOC, [dead], "ea-agent", NOW)["keys"] == {}
