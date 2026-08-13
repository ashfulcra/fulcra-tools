"""read_signal_records must always send an explicit time window to the
definition-scoped event endpoint.

Regression guard for the silent-degrade bug: compile called read_signal_records
with no window, which reached moment_annotations(None, None); the event endpoint
422s, and urllib.HTTPError is an OSError subclass, so the resilience `except`
swallowed it as [] on EVERY compile. The live read contributed nothing and
tier-2 (shell-less) captures were invisible — with no error surfaced.
"""
from urllib.error import HTTPError

from fulcra_prefs.store import FulcraStore, SIGNAL_HISTORY_FLOOR, _iso


def test_read_is_definition_scoped_with_explicit_window(fake_api):
    store = FulcraStore(fake_api)
    store.read_signal_records("def-123")
    q = fake_api.last_v1_query
    assert q is not None, "read_signal_records never called the event read endpoint"
    assert q["data_class"] == "event"
    # server-side definition scoping, not a base-type scan of every annotation
    assert q["data_type"] == "MomentAnnotation/def-123"
    # the exact bug: a start/end must be present, never None -> the 422 call
    assert q["params"]["start_time"], "start_time must be an explicit window bound"
    assert q["params"]["end_time"], "end_time must be an explicit window bound"


def test_default_window_spans_full_history(fake_api):
    store = FulcraStore(fake_api)
    store.read_signal_records("def-123")
    params = fake_api.last_v1_query["params"]
    # floor defaults to the signal-history floor when the caller passes none
    assert params["start_time"] == _iso(SIGNAL_HISTORY_FLOOR)


def test_http_error_is_surfaced_not_masked(fake_api, capsys):
    """A 4xx/5xx is a bug/misconfig, not an outage: it must be logged loudly and
    must NOT masquerade as an empty read (the original silent-degrade)."""
    def boom(*a, **k):
        raise HTTPError("http://x", 422, "Unprocessable Entity", {}, None)
    fake_api.fulcra_v1_api = boom
    store = FulcraStore(fake_api)
    out = store.read_signal_records("def-123")
    assert out == []                                 # still non-fatal: never crashes compile
    err = capsys.readouterr().err
    assert "422" in err and "shard cache" in err, "HTTP read error must be logged loudly"


def test_transport_error_is_quiet_cache_fallback(fake_api, capsys):
    """A genuine transport blip stays a silent [] (cache-only), no stderr noise."""
    fake_api.fail_read = True                         # fake raises ConnectionError
    store = FulcraStore(fake_api)
    assert store.read_signal_records("def-123") == []
    assert capsys.readouterr().err == ""


def test_empty_definition_id_reads_nothing(fake_api):
    store = FulcraStore(fake_api)
    assert store.read_signal_records(None) == []
    assert fake_api.last_v1_query is None, "must not hit the API without a definition"
