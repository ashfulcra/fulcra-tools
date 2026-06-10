import json
from datetime import datetime, timezone
from fulcra_prefs.capture import capture_signal
from fulcra_prefs.compileprefs import compile_signals
from fulcra_prefs.outbox import Outbox
from fulcra_prefs.schema import parse_record
from fulcra_prefs.store import FulcraStore

NOW = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)

def _capture(fake_api, tmp_path, **over):
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


# C2 regression: outage-captured signals must reach compile after recovery

def test_flush_backfills_signals_cache_shard(fake_api, tmp_path):
    """Regression for C2: after outage capture + flush, the shard exists in the
    file store so a compile over store.list_json sees the signal."""
    store = FulcraStore(fake_api)
    box = Outbox(tmp_path / "outbox")
    # Capture during outage: record is spooled locally but NOT in the file store.
    fake_api.fail_ingest = True
    sig = _capture(fake_api, tmp_path)
    assert len(fake_api.files) == 0          # nothing in the file store yet
    # Recovery: flush re-posts and back-fills the cache shard.
    fake_api.fail_ingest = False
    flushed = box.flush(store)
    assert flushed == 1
    # Shard must exist under /prefs/signals-cache/ (absolute path) with the temp id as the stem.
    shard_key = f"/prefs/signals-cache/{sig.id}.json"
    assert shard_key in fake_api.files, \
        f"expected shard {shard_key!r} in fake_api.files; got {list(fake_api.files)}"


def test_flush_backfill_visible_to_compile(fake_api, tmp_path):
    """End-to-end: capture during outage, flush after recovery, compile sees the
    signal and produces a compiled doc with the expected key."""
    store = FulcraStore(fake_api)
    box = Outbox(tmp_path / "outbox")
    fake_api.fail_ingest = True
    _capture(fake_api, tmp_path)
    fake_api.fail_ingest = False
    box.flush(store)
    # list_json picks up the back-filled shards; compile produces the key.
    shards = store.list_json("prefs/signals-cache")
    sigs = [parse_record(env) for env in shards]
    docs = compile_signals(sigs, NOW)
    assert "dining.cuisine.thai" in docs["global"]["keys"]


def test_flush_keeps_spool_when_backfill_cache_write_fails(fake_api, tmp_path):
    """If ingest recovers but the file-library cache write is still down,
    keep the spool. Otherwise v1 compile loses the signal."""
    store = FulcraStore(fake_api)
    box = Outbox(tmp_path / "outbox")
    fake_api.fail_ingest = True
    sig = _capture(fake_api, tmp_path)
    fake_api.fail_ingest = False
    fake_api.fail_upload = True
    assert box.flush(store) == 0
    assert len(box.pending()) == 1
    assert len(fake_api.ingested) == 1
    assert f"/prefs/signals-cache/{sig.id}.json" not in fake_api.files

    fake_api.fail_upload = False
    assert box.flush(store) == 1
    assert box.pending() == []
    assert f"/prefs/signals-cache/{sig.id}.json" in fake_api.files
