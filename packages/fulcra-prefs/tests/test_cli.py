import json
from datetime import datetime, timezone
import pytest
from fulcra_prefs.cli import run
from fulcra_prefs.outbox import Outbox
from fulcra_prefs.store import FulcraStore, META_PATH, COMPILED_PATH, CONSENT_PATH
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

def test_inject_prints_block_or_nothing(env, capsys):
    call, *_ = env
    assert call("inject", "--platform", "claude-code") == 0
    assert capsys.readouterr().out == ""                   # no compiled doc: silent
    call("capture", "--key", "k.a", "--value", "1", "--strength", "0.5",
         "--platform", "claude-code")
    call("compile")
    call("inject", "--platform", "claude-code")
    assert "# User preferences (fulcra-prefs)" in capsys.readouterr().out

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

def test_missing_meta_gives_actionable_error(fake_api, tmp_path, capsys):
    rc = run(["capture", "--key", "k", "--value", "1", "--strength", "0.5",
              "--platform", "x"], api=fake_api, outbox_dir=tmp_path, now=NOW)
    assert rc == 2
    assert "onboard" in capsys.readouterr().err

def test_signal_cache_shards_do_not_clobber_each_other(env):
    from fulcra_prefs.cli import _append_signal_cache, _load_cached_signals
    call, fake_api, store = env
    _append_signal_cache(store, make_signal(id="sig-a", key="k.a"))
    _append_signal_cache(store, make_signal(id="sig-b", key="k.b"))
    assert "prefs/signals-cache/sig-a.json" in fake_api.files
    assert "prefs/signals-cache/sig-b.json" in fake_api.files
    assert {s.id for s in _load_cached_signals(store)} == {"sig-a", "sig-b"}

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
