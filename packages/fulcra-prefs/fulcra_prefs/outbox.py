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
        # Atomic write: a crash mid-write leaves the .tmp (ignored by *.json
        # globs), never a truncated spool file that wedges later flushes.
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(json.dumps(record, sort_keys=True))
        tmp.replace(p)
        return p

    def pending(self) -> list[dict]:
        out: list[dict] = []
        for p in sorted(self.root.glob("*.json")):
            try:
                out.append(json.loads(p.read_text()))
            except ValueError:
                continue   # skip a corrupt/partial spool file, don't crash
        return out

    def flush(self, store) -> int:
        from .store import SIGNALS_CACHE_PREFIX, post_typed_record
        flushed = 0
        for p in sorted(self.root.glob("*.json")):
            try:
                record = json.loads(p.read_text())
            except ValueError:
                continue   # corrupt spool file: skip it, don't wedge the flush
            try:
                # Spool holds the canonical build_record envelope; post it via the
                # same typed helper as store.ingest_signal (record_data_type,
                # raw-POST fallback).
                post_typed_record(store._api, record)
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
