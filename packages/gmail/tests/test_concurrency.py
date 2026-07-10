"""Cross-instance atomicity of the B4 nonce consume + registry writes.

These tests exercise the DURABLE store's ``fcntl.flock`` (via
:class:`fulcra_gmail.accounts.JsonFileStore`), NOT the in-memory fake — a real
file on ``tmp_path`` shared by two SEPARATE ``AccountRegistry`` instances, each
with its OWN ``threading.Lock``. Because the two instances share nothing
in-process, the ONLY thing that can serialize them is the file lock; if it were
removed, both would consume the same nonce.

Why threads genuinely test cross-process exclusion here: BSD ``fcntl.flock``
locks the OPEN FILE DESCRIPTION, and ``JsonFileStore.transaction()`` ``open()``s
a fresh fd per call, so two threads holding two distinct fds block each other
exactly as two processes would — unlike ``fcntl.lockf``/``F_SETLK`` (keyed by
``(process, inode)``), which would NOT serialize threads and would make this
test pass vacuously. To keep the race window wide enough that a MISSING lock
reliably loses (both readers see the un-popped nonce), the store's ``read()`` is
slowed by ~40 ms; with the lock present that read runs strictly serialized.
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx

from fulcra_gmail.accounts import AccountRegistry, JsonFileStore

_CLIENT_ID = "synthetic-client.apps.example.test"
_CLIENT_SECRET = "synthetic-secret-XYZ"  # noqa: S105 — fake
_REDIRECT = "http://127.0.0.1:9292/api/oauth/callback"


class SlowJsonFileStore(JsonFileStore):
    """A real (flock-backed) store whose read is delayed to widen the race
    window inside the critical section — so a regression that dropped the lock
    would let both threads read the same un-consumed nonce and fail the test."""

    def read(self) -> dict:
        time.sleep(0.04)
        return super().read()


def _counting_transport(email: str, refresh_token: str):
    """MockTransport that thread-safely counts code→token exchanges."""
    counter = {"exchanges": 0}
    lock = threading.Lock()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            with lock:
                counter["exchanges"] += 1
            return httpx.Response(200, json={
                "access_token": "at-synthetic",
                "refresh_token": refresh_token,
            })
        if request.url.path.endswith("/users/me/profile"):
            return httpx.Response(200, json={"emailAddress": email})
        return httpx.Response(404, json={"error": "unexpected"})  # pragma: no cover

    return httpx.MockTransport(handler), counter


def test_concurrent_complete_consumes_nonce_exactly_once(tmp_path, keychain):
    """Two separate registries over ONE shared file, handed the SAME nonce+code
    concurrently: exactly one exchanges the code, one account row lands, the
    other is rejected as already-consumed."""
    path = tmp_path / "registry.json"
    transport, counter = _counting_transport(
        email="shared@example.test", refresh_token="rt-shared"
    )

    # Shared keychain mirrors the single OS keychain both processes would see.
    keychain.set("client:client_id", _CLIENT_ID)
    keychain.set("client:client_secret", _CLIENT_SECRET)

    # Mint the nonce once, into the shared file.
    setup = AccountRegistry(store=SlowJsonFileStore(path), keychain=keychain,
                            transport=transport)
    session = setup.begin_add_account(_REDIRECT)

    # Two independent instances (independent self._lock) share the file.
    reg_a = AccountRegistry(store=SlowJsonFileStore(path), keychain=keychain,
                            transport=transport)
    reg_b = AccountRegistry(store=SlowJsonFileStore(path), keychain=keychain,
                            transport=transport)

    start = threading.Barrier(2)

    def run(reg):
        start.wait()
        return reg.complete_add_account(session.state, "auth-code")

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [f.result() for f in
                   [pool.submit(run, reg_a), pool.submit(run, reg_b)]]

    oks = [r for r in results if r.ok]
    rejected = [r for r in results if not r.ok]
    assert len(oks) == 1, "nonce must be single-use across instances"
    assert len(rejected) == 1
    assert rejected[0].reason == "invalid_nonce"
    # The code→token exchange ran for the winner only.
    assert counter["exchanges"] == 1
    # Exactly one account row, one keychain token.
    assert len(reg_a.list_accounts()) == 1
    assert len([k for k in keychain.store if k.startswith("account:")]) == 1


def test_concurrent_begin_and_complete_lose_no_write(tmp_path, keychain):
    """A complete (consumes N1, adds an account row) racing a begin (adds a
    fresh nonce N2) must leave BOTH effects — the file lock prevents either
    read-modify-write from clobbering the other."""
    path = tmp_path / "registry.json"
    transport, _ = _counting_transport(
        email="dup@example.test", refresh_token="rt-1"
    )
    keychain.set("client:client_id", _CLIENT_ID)
    keychain.set("client:client_secret", _CLIENT_SECRET)

    setup = AccountRegistry(store=SlowJsonFileStore(path), keychain=keychain,
                            transport=transport)
    session1 = setup.begin_add_account(_REDIRECT)

    reg_complete = AccountRegistry(store=SlowJsonFileStore(path),
                                   keychain=keychain, transport=transport)
    reg_begin = AccountRegistry(store=SlowJsonFileStore(path),
                                keychain=keychain, transport=transport)

    start = threading.Barrier(2)
    box: dict = {}

    def do_complete():
        start.wait()
        box["complete"] = reg_complete.complete_add_account(session1.state, "code")

    def do_begin():
        start.wait()
        box["begin"] = reg_begin.begin_add_account(_REDIRECT)

    threads = [threading.Thread(target=do_complete),
               threading.Thread(target=do_begin)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Complete succeeded → one account row survived.
    assert box["complete"].ok is True
    accounts = reg_complete.list_accounts()
    assert len(accounts) == 1
    assert accounts[0].email == "dup@example.test"

    # Begin's fresh nonce N2 survived too (not clobbered by complete's writes),
    # and the consumed N1 is gone.
    final = JsonFileStore(path).read()
    nonces = final.get("nonces", {})
    assert session1.state not in nonces        # N1 consumed
    assert box["begin"].state in nonces        # N2 preserved
