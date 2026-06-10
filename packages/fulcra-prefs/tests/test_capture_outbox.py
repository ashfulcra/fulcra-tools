import json
from datetime import datetime, timezone
from fulcra_prefs.capture import capture_signal
from fulcra_prefs.outbox import Outbox
from fulcra_prefs.schema import parse_record

NOW = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)

def _capture(fake_api, tmp_path, **over):
    from fulcra_prefs.store import FulcraStore
    args = dict(key="dining.cuisine.thai", value={"liked": True}, strength=0.8,
                kind="preference", scope="global", confidence=0.9,
                half_life_days=90.0, platform="claude-code", agent=None,
                session=None, supersedes=None)
    args.update(over)
    return capture_signal(FulcraStore(fake_api), Outbox(tmp_path / "outbox"),
                          data_type="MomentAnnotation/def-123", now=NOW, **args)

def test_capture_ingests_one_record(fake_api, tmp_path):
    sig = _capture(fake_api, tmp_path)
    assert len(fake_api.ingested) == 1
    assert sig.key == "dining.cuisine.thai"
    assert sig.observed_at == "2026-06-10T12:00:00+00:00"

def test_capture_spools_to_outbox_on_failure(fake_api, tmp_path):
    fake_api.fail_ingest = True
    _capture(fake_api, tmp_path)
    box = Outbox(tmp_path / "outbox")
    assert len(box.pending()) == 1
    spooled = box.pending()[0]
    assert json.loads(spooled["data"])["key"] == "dining.cuisine.thai"

def test_outbox_flush_retries_and_clears(fake_api, tmp_path):
    fake_api.fail_ingest = True
    _capture(fake_api, tmp_path)
    fake_api.fail_ingest = False
    from fulcra_prefs.store import FulcraStore
    box = Outbox(tmp_path / "outbox")
    flushed = box.flush(FulcraStore(fake_api))
    assert flushed == 1
    assert box.pending() == []
    assert len(fake_api.ingested) == 1

def test_spooled_record_parses_back_to_signal_with_temp_id(fake_api, tmp_path):
    fake_api.fail_ingest = True
    _capture(fake_api, tmp_path)
    spooled = Outbox(tmp_path / "outbox").pending()[0]
    env = {"id": None, "recorded_at": spooled["metadata"]["recorded_at"],
           "sources": spooled["metadata"]["source"], "data": spooled["data"]}
    assert parse_record(env).id.startswith("com.fulcra-prefs.sig.")
