"""Local spool for records that failed to ingest (tier-1 resilience: a capture
never loses data because the network blinked). One JSON file per record;
flush() re-posts and deletes on success, keeps on failure."""
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
        flushed = 0
        for p in sorted(self.root.glob("*.json")):
            record = json.loads(p.read_text())
            try:
                store._api.fulcra_api("/ingest/v1/record", data=record,
                                      method="POST")
            except (OSError, ConnectionError, TimeoutError):
                continue                     # keep spooled; retry next flush
            p.unlink()
            flushed += 1
        return flushed
