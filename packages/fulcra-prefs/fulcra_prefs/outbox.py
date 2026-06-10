"""Local spool for records that failed to ingest (tier-1 resilience: a capture
never loses data because the network blinked). One JSON file per record;
flush() re-posts and deletes on success, keeps on failure.

On a successful re-POST, flush() also back-fills the signals-cache shard so
that a subsequent compile sees the signal without needing a live get-records
call. The shard naming mirrors cli._append_signal_cache exactly: the full
temp-id (record["metadata"]["source"][0]) is the filename stem, matching the
path written at capture time. Consent-kind records (disclosure logs) are not
back-filled — they have no cache shard by design."""
from __future__ import annotations
import json
from pathlib import Path


class Outbox:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def spool(self, record: dict) -> Path:
        # Same key+observed_at+platform => same temp id => same filename: identical re-captures dedup by overwrite (intentional).
        sid = record["metadata"]["source"][0].rsplit(".", 1)[-1]
        p = self.root / f"{sid}.json"
        p.write_text(json.dumps(record, sort_keys=True))
        return p

    def pending(self) -> list[dict]:
        return [json.loads(p.read_text())
                for p in sorted(self.root.glob("*.json"))]

    def flush(self, store) -> int:
        from .store import SIGNALS_CACHE_PREFIX
        flushed = 0
        for p in sorted(self.root.glob("*.json")):
            record = json.loads(p.read_text())
            try:
                store._api.fulcra_api("/ingest/v1/record", data=record,
                                      method="POST")
            except (OSError, ConnectionError, TimeoutError):
                continue                     # keep spooled; retry next flush
            # Back-fill the signals-cache shard so a subsequent compile sees
            # this signal. Mirrors cli._append_signal_cache naming exactly:
            # the full source[0] id is the filename stem. Skip consent records
            # (disclosure logs) — they have no shard by design.
            sources = record["metadata"]["source"]
            temp_id = sources[0]
            try:
                payload = json.loads(record["data"])
            except (ValueError, KeyError):
                continue
            if payload.get("kind") == "consent":
                p.unlink()
                flushed += 1
                continue
            shard_env = {
                "id": None,
                "recorded_at": record["metadata"]["recorded_at"],
                "sources": sources,
                "data": record["data"],
            }
            try:
                store.write_json(f"{SIGNALS_CACHE_PREFIX}/{temp_id}.json", shard_env)
            except (OSError, ConnectionError, TimeoutError):
                continue                 # keep spooled; retry back-fill later
            p.unlink()
            flushed += 1
        return flushed
