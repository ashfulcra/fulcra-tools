import json
from datetime import datetime, timezone
import pytest
from fulcra_prefs.cli import run
from fulcra_prefs.outbox import Outbox
from fulcra_prefs.store import FulcraStore, META_PATH, COMPILED_PATH
from test_schema import make_signal

NOW = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)

@pytest.fixture
def env(fake_api, tmp_path):
    """run(argv, api=..., outbox_dir=..., now=...) is the testable entrypoint;
    main() only adds real FulcraAPI + real clock."""
    store = FulcraStore(fake_api)
    store.write_json(META_PATH, {"definition_id": "def-123",
                                 "data_type": "MomentAnnotation/def-123", "v": 1})
    def call(*argv):
        return run(list(argv), api=fake_api, outbox_dir=tmp_path / "outbox", now=NOW)
    return call, fake_api, store

def test_capture_then_compile_then_get(env, capsys):
    call, fake_api, store = env
    assert call("capture", "--key", "dining.cuisine.thai", "--value",
                '{"liked": true}', "--strength", "0.8",
                "--platform", "claude-code") == 0
    assert len(fake_api.ingested) == 1
    # compile reads signals back; fake get-records: feed ingested through store
    assert call("compile") == 0
    compiled = store.read_json(COMPILED_PATH)
    assert "dining.cuisine.thai" in compiled["keys"]
    # M3: compile watermark — meta.json must carry last_compile after compile
    meta = store.read_json(META_PATH)
    assert meta.get("last_compile") == NOW.isoformat()
    assert call("get") == 0
    out = json.loads(capsys.readouterr().out)
    assert out["keys"]["dining.cuisine.thai"]["value"] == {"liked": True}

def test_get_for_audience_filters_and_logs_disclosure(env, capsys):
    call, fake_api, store = env
    call("capture", "--key", "dining.cuisine.thai", "--value", "true",
         "--strength", "0.8", "--platform", "claude-code")
    call("capture", "--key", "health.sleep.target", "--value", "8",
         "--strength", "1.0", "--platform", "claude-code")
    call("compile")
    call("consent", "grant", "--key-glob", "dining.*", "--audience", "ea")
    n_before = len(fake_api.ingested)
    assert call("get", "--for", "ea") == 0
    out = json.loads(capsys.readouterr().out)
    assert list(out["keys"]) == ["dining.cuisine.thai"]
    assert len(fake_api.ingested) == n_before + 1          # disclosure logged
    disclosure = json.loads(fake_api.ingested[-1]["data"])
    assert disclosure["kind"] == "consent"

def test_get_platform_falls_back_to_global_when_no_overlay(env, capsys):
    """BUG A: `get --platform X` for a platform with NO platform-scoped
    overrides must return the global compiled doc, not an empty doc. Compile
    only writes platforms/X.json when X has a platform:-scoped signal; for any
    other platform the view IS global. cmd_inject already falls back to global;
    cmd_get must match (and it's the consent-gated export path, so silently
    returning {} is worse than wrong)."""
    call, fake_api, store = env
    # A purely GLOBAL preference (no platform overlay anywhere).
    call("capture", "--key", "dining.cuisine.thai", "--value", "true",
         "--strength", "0.8", "--platform", "claude-code")  # platform = source, scope stays global
    call("compile")
    assert call("get", "--platform", "codex") == 0
    out = json.loads(capsys.readouterr().out)
    assert "dining.cuisine.thai" in out["keys"], \
        "get --platform with no overlay should fall back to global prefs"


def test_compile_sees_ingest_only_signal_without_a_shard(env, capsys):
    """Feature 1 — tier-2 capture visibility. A signal that was INGESTED but has
    NO cache shard (exactly what a shell-less tier-2 agent produces: one POST to
    /ingest, no file write) must still reach compile via the get-records read
    path. Pre-fix, compile read only shards, so tier-2 captures were invisible."""
    call, fake_api, store = env
    from fulcra_prefs.schema import Signal, temp_signal_id
    obs = "2026-06-10T11:00:00+00:00"
    key = "comms.tone.concise"
    sig = Signal(id=temp_signal_id(key, obs, "chatgpt"), kind="preference",
                 key=key, scope="global", value={"preferred": True}, strength=0.9,
                 confidence=1.0, half_life_days=90.0, observed_at=obs,
                 platform="chatgpt", agent=None, session=None, supersedes=None)
    store.ingest_signal(sig, data_type="MomentAnnotation/def-123")  # tier-2: ingest only
    assert call("compile") == 0
    assert call("get") == 0
    out = json.loads(capsys.readouterr().out)
    assert key in out["keys"], "ingest-only (tier-2) signal must be visible to compile"


def test_compile_gcs_confirmed_shards_keeps_unconfirmed(env, capsys):
    """Feature 2 — cache GC. After compile, a shard whose signal is confirmed in
    get-records is pruned (the authoritative source has it; the write-through
    shard is dead weight that would be re-downloaded every compile). A shard
    NOT yet confirmed (read down / indexing lag) is kept as the safety net."""
    call, fake_api, store = env
    # Capture writes a shard AND ingests (so the record is readable = confirmed).
    call("capture", "--key", "dining.cuisine.thai", "--value", "true",
         "--strength", "0.8", "--platform", "claude-code")
    shard_paths = [p for p in fake_api.files if "signals-cache" in p]
    assert len(shard_paths) == 1                      # shard present pre-compile
    assert call("compile") == 0
    assert [p for p in fake_api.files if "signals-cache" in p] == [], \
        "confirmed shard should be GC'd after compile"
    # get still works — compile read the record authoritatively
    assert call("get") == 0
    assert "dining.cuisine.thai" in json.loads(capsys.readouterr().out)["keys"]


def test_compile_does_not_fail_when_cache_gc_delete_fails(env, capsys):
    call, fake_api, _store = env
    call("capture", "--key", "dining.cuisine.thai", "--value", "true",
         "--strength", "0.8", "--platform", "claude-code")

    def fail_delete(file_id):
        raise Exception("simulated delete outage")

    fake_api.delete_file = fail_delete
    assert call("compile") == 0
    assert [p for p in fake_api.files if "signals-cache" in p] != [], \
        "failed GC delete should leave shard for a later retry"


def test_compile_keeps_shard_when_record_unconfirmed(env, capsys):
    """A shard whose record isn't visible in get-records (read outage / lag)
    must be KEPT — pruning it would lose the only copy of that signal."""
    call, fake_api, store = env
    call("capture", "--key", "dining.cuisine.thai", "--value", "true",
         "--strength", "0.8", "--platform", "claude-code")
    fake_api.fail_read = True                          # record read unavailable
    assert call("compile") == 0
    assert [p for p in fake_api.files if "signals-cache" in p] != [], \
        "unconfirmed shard must be kept as the safety net"


def test_compile_degrades_to_shards_when_record_read_fails(env, capsys):
    """If the get-records read is unreachable, compile must still proceed from
    the shard cache (never worse than the cache-only path it replaced)."""
    call, fake_api, store = env
    call("capture", "--key", "dining.cuisine.thai", "--value", "true",
         "--strength", "0.8", "--platform", "claude-code")   # writes a shard
    fake_api.fail_read = True                                 # get-records down
    assert call("compile") == 0
    assert call("get") == 0
    out = json.loads(capsys.readouterr().out)
    assert "dining.cuisine.thai" in out["keys"]


def test_inject_prints_block_or_nothing(env, capsys):
    call, *_ = env
    assert call("inject", "--platform", "claude-code") == 0
    assert capsys.readouterr().out == ""                   # no compiled doc: silent
    call("capture", "--key", "k.a", "--value", "1", "--strength", "0.5",
         "--platform", "claude-code")
    call("compile")
    call("inject", "--platform", "claude-code")
    assert "# User preferences (fulcra-prefs)" in capsys.readouterr().out


def test_inject_never_crashes_on_store_error(fake_api, tmp_path, capsys):
    """M7: cmd_inject must never propagate exceptions; it must return 0 and
    produce no stdout even when the store raises (e.g. ingest/network outage).
    The session bootstrap hook relies on this contract."""
    # Simulate a store that blows up on read_json
    class BrokenAPI:
        def resolve_filepath(self, path, **kw):
            raise OSError("simulated network failure")
    rc = run(["inject", "--platform", "claude-code"],
             api=BrokenAPI(), outbox_dir=tmp_path / "outbox", now=NOW)
    assert rc == 0
    captured = capsys.readouterr()
    assert captured.out == ""          # nothing to stdout — session unharmed
    assert "inject warning" in captured.err

def test_solve_from_files(env, tmp_path, capsys):
    call, *_ = env
    options = [{"id": "thai", "keys": ["dining.cuisine.thai"]},
               {"id": "bbq", "keys": ["dining.cuisine.bbq"]}]
    docs = {"alice": {"v": 1, "compiled_at": "x",
                      "keys": {"dining.cuisine.thai": {"weight": 0.9, "value": True}}}}
    (tmp_path / "options.json").write_text(json.dumps(options))
    (tmp_path / "docs.json").write_text(json.dumps(docs))
    assert call("solve", "--options", str(tmp_path / "options.json"),
                "--participants", str(tmp_path / "docs.json")) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ranked"][0]["id"] == "thai"
    assert out["trace"]

def test_capture_batch_ingests_all_items(env, tmp_path, capsys):
    """Feature 3 — auto-capture mechanism. An agent that noticed several
    preferences in a session records them in ONE consented call. Per-item
    confidence is honored (inferred items can be marked lower-confidence)."""
    call, fake_api, store = env
    batch = [
        {"key": "dining.cuisine.thai", "value": True, "strength": 0.9, "confidence": 1.0},
        {"key": "comms.tone.concise", "value": True, "strength": 0.7, "confidence": 0.5,
         "kind": "preference"},
    ]
    f = tmp_path / "batch.json"
    f.write_text(json.dumps(batch))
    assert call("capture-batch", "--file", str(f), "--platform", "chatgpt") == 0
    assert len(fake_api.ingested) == 2
    assert call("compile") == 0
    assert call("get") == 0
    out = json.loads(capsys.readouterr().out)
    assert "dining.cuisine.thai" in out["keys"]
    assert "comms.tone.concise" in out["keys"]


def test_notice_queues_candidate_for_lifecycle_drain(env, tmp_path, capsys):
    call, _fake_api, _store = env

    assert call(
        "notice",
        "--platform", "codex",
        "--session", "sess-1",
        "--candidate-dir", str(tmp_path / "candidates"),
        "--key", "docs.style.human_agent_quality",
        "--value", '{"preferred": true}',
        "--strength", "1.0",
        "--confidence", "1.0",
        "--half-life", "365",
        "--agent", "codex-prefs",
    ) == 0

    path = tmp_path / "candidates" / "codex" / "sess-1.json"
    queued = json.loads(path.read_text())
    assert queued == [{
        "key": "docs.style.human_agent_quality",
        "value": {"preferred": True},
        "strength": 1.0,
        "kind": "preference",
        "scope": "global",
        "confidence": 1.0,
        "half_life_days": 365.0,
        "platform": "codex",
        "agent": "codex-prefs",
        "session": "sess-1",
        "supersedes": None,
    }]
    assert "queued 1 candidate" in capsys.readouterr().err


def test_candidate_path_prints_queue_path(fake_api, tmp_path, capsys):
    rc = run([
        "candidate-path",
        "--platform", "codex",
        "--session", "sess-1",
        "--candidate-dir", str(tmp_path),
    ], api=fake_api, outbox_dir=tmp_path / "outbox", now=NOW)

    assert rc == 0
    assert capsys.readouterr().out.strip() == str(tmp_path / "codex" / "sess-1.json")


def test_drain_candidates_captures_and_marks_file(env, tmp_path, capsys):
    call, fake_api, store = env
    candidate_dir = tmp_path / "candidates"
    assert call(
        "notice",
        "--platform", "codex",
        "--session", "sess-1",
        "--candidate-dir", str(candidate_dir),
        "--key", "comms.tone.concise",
        "--value", '{"preferred": true}',
        "--strength", "0.8",
    ) == 0

    assert call(
        "drain-candidates",
        "--platform", "codex",
        "--session", "sess-1",
        "--candidate-dir", str(candidate_dir),
    ) == 0

    assert len(fake_api.ingested) == 1
    assert not (candidate_dir / "codex" / "sess-1.json").exists()
    assert (candidate_dir / "codex" / "sess-1.json.captured").exists()
    assert call("compile") == 0
    compiled = store.read_json(COMPILED_PATH)
    assert "comms.tone.concise" in compiled["keys"]


def test_extract_candidates_prints_json_without_writing(env, tmp_path, capsys):
    call, _fake_api, _store = env

    assert call(
        "extract-candidates",
        "--platform", "codex",
        "--session", "sess-1",
        "--candidate-dir", str(tmp_path / "candidates"),
        "--text", "I prefer concise tone in updates.",
    ) == 0

    out = json.loads(capsys.readouterr().out)
    assert out[0]["key"] == "comms.tone"
    assert not (tmp_path / "candidates").exists()


def test_extract_candidates_write_appends_to_queue(env, tmp_path, capsys):
    call, _fake_api, _store = env

    assert call(
        "extract-candidates",
        "--platform", "codex",
        "--session", "sess-1",
        "--candidate-dir", str(tmp_path / "candidates"),
        "--write",
        "--text", "I want documentation for humans and agents.",
    ) == 0

    path = tmp_path / "candidates" / "codex" / "sess-1.json"
    queued = json.loads(path.read_text())
    assert queued[0]["key"] == "docs.style.human_agent_quality"
    assert "queued 1 extracted candidate" in capsys.readouterr().err


def test_capture_batch_rejects_non_array(env, tmp_path, capsys):
    call, fake_api, store = env
    f = tmp_path / "bad.json"
    f.write_text(json.dumps({"key": "x"}))      # object, not array
    assert call("capture-batch", "--file", str(f), "--platform", "chatgpt") == 2
    assert "array" in capsys.readouterr().err


def test_capture_batch_rejects_non_object_item_without_ingest(env, tmp_path, capsys):
    call, fake_api, _store = env
    f = tmp_path / "bad.json"
    f.write_text(json.dumps([1]))
    assert call("capture-batch", "--file", str(f), "--platform", "chatgpt") == 2
    assert len(fake_api.ingested) == 0
    assert "item 1" in capsys.readouterr().err


def test_capture_batch_rejects_bad_item_before_any_ingest(env, tmp_path, capsys):
    call, fake_api, _store = env
    f = tmp_path / "bad.json"
    f.write_text(json.dumps([
        {"key": "dining.cuisine.thai", "value": True, "strength": 0.9},
        {"key": "comms.tone.concise", "value": True},
    ]))
    assert call("capture-batch", "--file", str(f), "--platform", "chatgpt") == 2
    assert len(fake_api.ingested) == 0
    err = capsys.readouterr().err
    assert "item 2" in err
    assert "strength" in err


def test_missing_meta_gives_actionable_error(fake_api, tmp_path, capsys):
    rc = run(["capture", "--key", "k", "--value", "1", "--strength", "0.5",
              "--platform", "x"], api=fake_api, outbox_dir=tmp_path, now=NOW)
    assert rc == 2
    assert "onboard" in capsys.readouterr().err

def test_signal_cache_shards_do_not_clobber_each_other(env):
    from fulcra_prefs.cli import _append_signal_cache, _gather_signals
    call, fake_api, store = env
    _append_signal_cache(store, make_signal(id="sig-a", key="k.a"))
    _append_signal_cache(store, make_signal(id="sig-b", key="k.b"))
    assert "/prefs/signals-cache/sig-a.json" in fake_api.files
    assert "/prefs/signals-cache/sig-b.json" in fake_api.files
    signals, _confirmed = _gather_signals(store)   # no meta -> shards only
    assert {s.id for s in signals} == {"sig-a", "sig-b"}

def test_get_for_audience_spools_disclosure_when_ingest_down(env, tmp_path, capsys):
    call, fake_api, store = env
    call("capture", "--key", "dining.cuisine.thai", "--value", "true",
         "--strength", "0.8", "--platform", "claude-code")
    call("compile")
    call("consent", "grant", "--key-glob", "dining.*", "--audience", "ea")
    fake_api.fail_ingest = True
    assert call("get", "--for", "ea") == 0          # doc still printed
    out = json.loads(capsys.readouterr().out)
    assert list(out["keys"]) == ["dining.cuisine.thai"]
    assert len(Outbox(tmp_path / "outbox").pending()) == 1


# C2 end-to-end: capture during outage (no traceback, exit 0), then compile
# after recovery (flush runs inside cmd_compile) → get shows the key.

def test_capture_during_outage_compile_after_recovery(env, capsys):
    """C2 regression: a signal captured while ingest is down must survive
    through outbox flush → signals-cache back-fill → compile → get."""
    call, fake_api, store = env
    # Capture while ingest is down: should exit 0 with no traceback.
    fake_api.fail_ingest = True
    assert call("capture", "--key", "dining.cuisine.thai", "--value",
                '{"liked": true}', "--strength", "0.8",
                "--platform", "claude-code") == 0
    # No record ingested yet; outbox has one entry.
    assert len(fake_api.ingested) == 0
    capsys.readouterr()   # discard capture stderr
    # Recovery: ingest is back up; compile flushes outbox first.
    fake_api.fail_ingest = False
    assert call("compile") == 0
    capsys.readouterr()
    # get must show the key.
    assert call("get") == 0
    out = json.loads(capsys.readouterr().out)
    assert "dining.cuisine.thai" in out["keys"]


def test_successful_capture_spools_when_cache_write_fails(fake_api, tmp_path, capsys):
    """Ingest success plus file-cache outage must not lose the compile-visible
    signal. Capture spools the record so a later compile can back-fill."""
    store = FulcraStore(fake_api)
    store.write_json(META_PATH, {"definition_id": "def-123",
                                 "data_type": "MomentAnnotation/def-123", "v": 1})
    outbox_dir = tmp_path / "outbox"
    def call(*argv):
        return run(list(argv), api=fake_api, outbox_dir=outbox_dir, now=NOW)

    fake_api.fail_upload = True
    assert call("capture", "--key", "dining.cuisine.thai", "--value",
                '{"liked": true}', "--strength", "0.8",
                "--platform", "claude-code") == 0
    assert len(fake_api.ingested) == 1
    assert len(Outbox(outbox_dir).pending()) == 1

    fake_api.fail_upload = False
    assert call("compile") == 0
    assert call("get") == 0
    out = json.loads(capsys.readouterr().out)
    assert "dining.cuisine.thai" in out["keys"]
    assert Outbox(outbox_dir).pending() == []
