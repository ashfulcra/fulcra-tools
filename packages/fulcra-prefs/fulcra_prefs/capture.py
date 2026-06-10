"""Build + ingest one signal. On ingest failure the fully-formed record is
spooled to the outbox — the temp signal id in metadata.source survives, so
supersedes references stay valid after a later flush (SPEC.md, db46fb5)."""
from __future__ import annotations
from datetime import datetime
from .outbox import Outbox
from .schema import Signal, temp_signal_id
from .store import FulcraStore, build_record


def capture_signal(store: FulcraStore, outbox: Outbox, *, data_type: str,
                   now: datetime, key: str, value, strength: float,
                   kind: str = "preference", scope: str = "global",
                   confidence: float = 1.0, half_life_days: float | None = 90.0,
                   platform: str = "unknown", agent: str | None = None,
                   session: str | None = None,
                   supersedes: str | None = None) -> Signal:
    observed = now.isoformat()
    sig = Signal(id=temp_signal_id(key, observed, platform), kind=kind, key=key,
                 scope=scope, value=value, strength=strength,
                 confidence=confidence, half_life_days=half_life_days,
                 observed_at=observed, platform=platform, agent=agent,
                 session=session, supersedes=supersedes)
    # Only transport failures spool; programming errors must propagate loudly.
    try:
        store.ingest_signal(sig, data_type=data_type)
    except (OSError, ConnectionError, TimeoutError):
        outbox.spool(build_record(sig, data_type))
    return sig
