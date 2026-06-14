"""One corrupt spool file must not wedge the whole outbox.

pending()/flush() parsed every spool file with an unguarded json.loads, so a
single truncated file (a crash mid-spool) made the entire flush raise — every
good record behind it never re-posted. spool() also wrote in place, which is
how a partial file appears in the first place.
"""
import json

from fulcra_prefs.outbox import Outbox
from fulcra_prefs.store import FulcraStore


def _good_record():
    return {
        "data": json.dumps({"kind": "preference", "key": "k", "scope": "global",
                            "value": True, "strength": 0.5, "confidence": 1.0,
                            "half_life_days": 90.0, "source": {"platform": "codex"}}),
        "metadata": {"content_type": "application/json", "data_type": "MomentAnnotation",
                     "recorded_at": "2026-06-10T12:00:00+00:00",
                     "source": ["com.fulcra-prefs.sig.deadbeef",
                                "com.fulcra-prefs.capture.codex"]},
        "specversion": 1,
    }


def test_pending_skips_corrupt_file(tmp_path):
    box = Outbox(tmp_path / "outbox")
    (box.root / "broken.json").write_text('{"metadata": ')   # truncated
    (box.root / "ok.json").write_text(json.dumps(_good_record(), sort_keys=True))
    pend = box.pending()
    assert len(pend) == 1   # corrupt skipped, good returned, no crash


def test_flush_skips_corrupt_and_flushes_good(fake_api, tmp_path):
    box = Outbox(tmp_path / "outbox")
    # "broken.json" sorts before "good.json" — pre-fix it crashes the whole flush
    (box.root / "broken.json").write_text('{"metadata": ')
    (box.root / "good.json").write_text(json.dumps(_good_record(), sort_keys=True))
    flushed = box.flush(FulcraStore(fake_api))
    assert flushed == 1
    assert not (box.root / "good.json").exists()   # good re-posted + removed
    assert (box.root / "broken.json").exists()      # corrupt left for inspection
