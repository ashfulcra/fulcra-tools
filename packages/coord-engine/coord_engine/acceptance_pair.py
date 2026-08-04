"""Deterministic pairwise Bus/continuity acceptance orchestration.

The adapter owns production I/O.  This module is deliberately stdlib-only and
keeps the state machine independently testable: every hop either emits a timed
positive heartbeat or stops immediately with the raw failing evidence.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Protocol


@dataclass(frozen=True)
class HopResult:
    ok: bool
    detail: str
    raw: str = ""


class PairAdapter(Protocol):
    def prove_delivery(self, identity: str) -> HopResult: ...
    def tell(self) -> HopResult: ...
    def peer_reads_directive(self) -> HopResult: ...
    def peer_responds(self) -> HopResult: ...
    def agent_reads_response(self) -> HopResult: ...
    def peer_parks(self) -> HopResult: ...
    def agent_resumes_peer(self) -> HopResult: ...
    def final_join(self) -> HopResult: ...


def run_pair(
    adapter: PairAdapter,
    *,
    agent: str,
    peer: str,
    emit: Callable[[str], None] = print,
    clock: Callable[[], float] = time.monotonic,
) -> int:
    """Run the fail-fast acceptance state machine."""
    hops = [
        (f"{agent} doctor --delivery", lambda: adapter.prove_delivery(agent)),
        (f"{peer} doctor --delivery", lambda: adapter.prove_delivery(peer)),
        (f"{agent} tells {peer} nonce directive", adapter.tell),
        (f"{peer} queue reads and verifies directive nonce", adapter.peer_reads_directive),
        (f"{peer} responds with nonce", adapter.peer_responds),
        (f"{agent} queue reads and verifies response nonce", adapter.agent_reads_response),
        (f"{peer} parks nonce checkpoint and proves write", adapter.peer_parks),
        (f"{agent} resumes {peer} checkpoint under 5m", adapter.agent_resumes_peer),
        ("GET-ON-THE-BUS final join", adapter.final_join),
    ]
    suite_started = clock()
    for number, (name, operation) in enumerate(hops, 1):
        started = clock()
        try:
            result = operation()
        except Exception as exc:  # adapters must never turn a failure into a traceback
            result = HopResult(False, f"{type(exc).__name__}: {exc}")
        elapsed = clock() - started
        if not result.ok:
            emit(f"FAILED AT HOP {number} ({name}) after {elapsed:.1f}s: {result.detail}")
            if result.raw:
                emit(result.raw.rstrip())
            return 1
        emit(f"HOP {number} PASS ({elapsed:.1f}s) — {name}: {result.detail}")
    emit(f"PASS pair {agent}<->{peer} in {clock() - suite_started:.1f}s")
    return 0
