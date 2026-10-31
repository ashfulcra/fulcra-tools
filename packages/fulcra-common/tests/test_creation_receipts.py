"""Known creation UUIDs survive failed proof without becoming write authority."""
import base64
import json
import sqlite3

import httpx
import pytest

from fulcra_common.client import BaseFulcraClient
from fulcra_common.data_types import CreationJournal, DataTypeSpec, resolve_data_type

ACCOUNT = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
OTHER_ACCOUNT = "aaaaaaaa-bbbb-4ccc-8ddd-ffffffffffff"
TYPE_ID = "Event/11111111-2222-4333-8444-555555555555"
SECOND_ID = "Event/11111111-2222-4333-8444-666666666666"
SCHEMA = {"type": "object", "properties": {
    "id": {"type": "string", "format": "uuid"},
    "sources": {"type": "array", "items": {"type": "string"}},
    "tags": {"type": "array", "items": {"type": "string"}},
    "start_time": {"type": "string", "format": "date-time"},
    "end_time": {"type": "string", "format": "date-time"},
    "note": {"type": ["string", "null"]}}}


def spec(**changes):
    args = dict(key="collect.synthetic.receipt.v1", base_type="Event",
        name="Synthetic receipt events", description="Synthetic receipt tests.",
        fields={"note": {"type": ["string", "null"]}})
    return DataTypeSpec(**(args | changes))


def entry(**changes):
    return {"id": TYPE_ID, "name": spec().name, "api_version": "v1", "recordable": True,
        "deprecated": False, "fulcra_userid": ACCOUNT, "record_spec": {"type": "event"}, **changes}


def client(handler):
    class SyntheticClient(BaseFulcraClient):
        def get_token(self):
            claims = base64.urlsafe_b64encode(json.dumps({"fulcradynamics.com/userid": ACCOUNT}).encode()).decode().rstrip("=")
            return "synthetic." + claims + ".signature"
    return SyntheticClient(base_url="https://api.example", transport=httpx.MockTransport(handler))


@pytest.mark.parametrize("mode", ["substitute", "catalog_failed", "account_changed", "redirect_read_failed"])
def test_known_receipt_survives_later_proof_and_is_never_substituted(tmp_path, mode):
    journal = CreationJournal(tmp_path / "types.sqlite")
    posted, aliases = [], []
    captured = ACCOUNT

    def handler(request):
        nonlocal captured
        if request.method == "POST":
            posted.append(request.url.path)
            if mode == "redirect_read_failed":
                return httpx.Response(303, headers={"location": f"https://api.example/input/v1/data_type/{TYPE_ID}"})
            if mode == "account_changed":
                captured = OTHER_ACCOUNT
            return httpx.Response(201, json={"id": TYPE_ID})
        if request.url.path == f"/input/v1/data_type/{TYPE_ID}":
            aliases.append(request.url.path)
            return httpx.Response(503)
        if request.url.path == "/input/v1/data_type":
            if not posted:
                return httpx.Response(200, json=[])
            if mode in {"catalog_failed", "redirect_read_failed"}:
                return httpx.Response(503)
            return httpx.Response(200, json=[entry(id=SECOND_ID if mode == "substitute" else TYPE_ID)])
        return httpx.Response(200, json=SCHEMA)

    current = client(handler)
    if mode == "account_changed":
        def token():
            claims = base64.urlsafe_b64encode(json.dumps({"fulcradynamics.com/userid": captured}).encode()).decode().rstrip("=")
            return "synthetic." + claims + ".signature"
        current.get_token = token
    try:
        with pytest.raises((ValueError, RuntimeError, httpx.HTTPStatusError)):
            resolve_data_type(client=current, spec=spec(), account_id=ACCOUNT, allow_create=True, journal=journal)
        assert journal.read(ACCOUNT, spec())[1:] == ("submitted", TYPE_ID)
        assert len(posted) == 1
        assert not aliases
    finally:
        current._client().close()


def test_original_reservation_retains_conflicting_receipts_before_raising(tmp_path):
    journal = CreationJournal(tmp_path / "types.sqlite")
    reservation = journal.reserve(ACCOUNT, spec())
    journal.retain_receipt(reservation, TYPE_ID)
    assert journal.read(ACCOUNT, spec())[1:] == ("submitted", TYPE_ID)
    with pytest.raises(ValueError, match="conflict"):
        journal.retain_receipt(reservation, SECOND_ID)
    assert set(journal.receipts(reservation)) == {TYPE_ID, SECOND_ID}
    with pytest.raises(ValueError, match="conflict"):
        journal.read(ACCOUNT, spec())
    with pytest.raises(ValueError):
        journal.resolved(ACCOUNT, spec(), TYPE_ID)


@pytest.mark.parametrize("replacement", ["reserve", "begin", "adopt"])
def test_stale_receipt_is_audit_only_after_public_slot_recreation(tmp_path, replacement):
    journal = CreationJournal(tmp_path / "types.sqlite")
    original = journal.reserve(ACCOUNT, spec())
    journal._validation_rejected(ACCOUNT, spec(), reservation=original)
    corrected = spec(description="Corrected synthetic receipt specification")
    if replacement in {"reserve", "begin"}:
        if replacement == "begin":
            assert journal.begin(ACCOUNT, corrected)
            newer = journal.reservation(ACCOUNT, corrected)
        else:
            newer = journal.reserve(ACCOUNT, corrected)
        assert newer != original
        expected = ("pending", None)
    else:
        journal.resolved(ACCOUNT, corrected, SECOND_ID)
        expected = ("resolved", SECOND_ID)
    with pytest.raises(ValueError):
        journal.retain_receipt(original, TYPE_ID)
    assert journal.receipts(original) == (TYPE_ID,)
    assert journal.read(ACCOUNT, corrected)[1:] == expected


def test_receipt_retention_never_recreates_a_missing_database(tmp_path):
    journal = CreationJournal(tmp_path / "types.sqlite")
    reservation = journal.reserve(ACCOUNT, spec())
    journal.path.unlink()
    with pytest.raises((ValueError, OSError, sqlite3.DatabaseError)):
        journal.retain_receipt(reservation, TYPE_ID)
    assert not journal.path.exists()


@pytest.mark.parametrize("status,destination", [("resolved", "Event/bad"), ("submitted", "Event/bad"),
    ("pending", TYPE_ID), ("unknown", None)])
def test_receipt_survives_corrupt_current_slot_without_repair(tmp_path, status, destination):
    journal = CreationJournal(tmp_path / "types.sqlite")
    reservation = journal.reserve(ACCOUNT, spec())
    with journal._connect() as db:
        db.execute("UPDATE creations SET status=?,destination=?", (status, destination))
    with pytest.raises(ValueError):
        journal.retain_receipt(reservation, TYPE_ID)
    assert journal.receipts(reservation) == (TYPE_ID,)
    with journal._connect() as db:
        assert db.execute("SELECT status,destination FROM creations").fetchone() == (status, destination)


def test_known_redirect_location_survives_an_unreadable_body_without_following_it(tmp_path):
    journal = CreationJournal(tmp_path / "types.sqlite")
    closed, body_reads, posts = [], [], []

    class UnreadableBody(httpx.SyncByteStream):
        def __iter__(self):
            body_reads.append(True)
            raise httpx.ReadError("synthetic body failure")
            yield b""  # Make this an actual response stream.

        def close(self):
            closed.append(True)

    def handler(request):
        if request.method == "POST":
            posts.append(True)
            return httpx.Response(303, headers={"location": f"/input/v1/data_type/{TYPE_ID}"}, stream=UnreadableBody())
        assert request.url.path == "/input/v1/data_type", "a known receipt redirect must never be followed"
        return httpx.Response(503) if posts else httpx.Response(200, json=[])

    current = client(handler)
    try:
        with pytest.raises(httpx.HTTPStatusError):
            resolve_data_type(client=current, spec=spec(), account_id=ACCOUNT, allow_create=True, journal=journal)
        assert journal.read(ACCOUNT, spec())[1:] == ("submitted", TYPE_ID)
        assert not body_reads and closed == [True]
    finally:
        current._client().close()


def test_two_connections_retain_both_racing_receipts_in_a_durable_hold(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    path = tmp_path / "types.sqlite"
    first, second = CreationJournal(path), CreationJournal(path)
    original = first.reserve(ACCOUNT, spec())
    barrier = Barrier(2)

    def retain(journal, destination):
        barrier.wait(timeout=5)
        try:
            journal.retain_receipt(original, destination)
            return True
        except ValueError:
            return False

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = [executor.submit(retain, first, TYPE_ID), executor.submit(retain, second, SECOND_ID)]
        assert sorted(result.result(timeout=10) for result in results) == [False, True]
    assert set(CreationJournal(path).receipts(original)) == {TYPE_ID, SECOND_ID}
    with pytest.raises(ValueError, match="conflict"):
        CreationJournal(path).read(ACCOUNT, spec())


def test_late_resolution_and_422_cannot_change_a_newer_reservation(tmp_path):
    journal = CreationJournal(tmp_path / "types.sqlite")
    original = journal.reserve(ACCOUNT, spec())
    journal._validation_rejected(ACCOUNT, spec(), reservation=original)
    corrected = spec(description="Corrected synthetic receipt specification")
    newer = journal.reserve(ACCOUNT, corrected)
    journal._validation_rejected(ACCOUNT, spec(), reservation=original)
    with pytest.raises(ValueError):
        journal.resolved(ACCOUNT, spec(), TYPE_ID, reservation=original)
    assert journal.read(ACCOUNT, corrected)[1:] == ("pending", None)
    journal.retain_receipt(newer, SECOND_ID)
    journal.resolved(ACCOUNT, corrected, SECOND_ID, reservation=newer)
    assert journal.read(ACCOUNT, corrected)[1:] == ("resolved", SECOND_ID)


def test_reserved_resolution_never_recreates_a_deleted_current_slot(tmp_path):
    journal = CreationJournal(tmp_path / "types.sqlite")
    original = journal.reserve(ACCOUNT, spec())
    with journal._connect() as db:
        db.execute("DELETE FROM creations")
    with pytest.raises(ValueError):
        journal.resolved(ACCOUNT, spec(), TYPE_ID, reservation=original)
    with pytest.raises(ValueError):
        journal.read(ACCOUNT, spec())


def test_legacy_pending_journal_stays_readable_and_never_authorizes_another_post(tmp_path):
    path = tmp_path / "types.sqlite"
    path.touch(mode=0o600)
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE creations (account TEXT,key TEXT,fingerprint TEXT,status TEXT,destination TEXT,PRIMARY KEY(account,key))")
        db.execute("INSERT INTO creations VALUES (?,?,?,'pending',NULL)", (ACCOUNT, spec().key, spec().fingerprint))
    before = path.read_bytes()
    assert CreationJournal(path, read_only=True).read(ACCOUNT, spec())[1:] == ("pending", None)
    assert path.read_bytes() == before
    upgraded = CreationJournal(path)
    assert upgraded.reserve(ACCOUNT, spec()) is None
    assert upgraded.reservation(ACCOUNT, spec()) is None
    upgraded.resolved(ACCOUNT, spec(), TYPE_ID)
    assert upgraded.read(ACCOUNT, spec())[1:] == ("resolved", TYPE_ID)


@pytest.mark.parametrize("location", [
    f"https://other.example/input/v1/data_type/{TYPE_ID}",
    f"/input/v1/data_type/{TYPE_ID}?owner=other",
    f"/input/v1/data_type/{TYPE_ID}#fragment",
    f"/input/v1/data_type/{TYPE_ID.replace('Event/', 'Event%2F')}",
    "/input/v1/data_type/Event/bad",
    f"/input/v1/data_type/{TYPE_ID.replace('Event/', 'Metric/')}",
])
def test_untrusted_redirect_receipt_never_resolves_or_follows_alias(tmp_path, location):
    journal = CreationJournal(tmp_path / "types.sqlite")
    calls = []

    def handler(request):
        calls.append(request.method)
        if request.method == "POST":
            return httpx.Response(303, headers={"location": location})
        assert request.url.path == "/input/v1/data_type"
        return httpx.Response(200, json=[])

    current = client(handler)
    try:
        with pytest.raises(ValueError):
            resolve_data_type(client=current, spec=spec(), account_id=ACCOUNT, allow_create=True, journal=journal)
        assert journal.read(ACCOUNT, spec())[1:] == ("pending", None)
        assert journal.receipts(journal.reservation(ACCOUNT, spec())) == ()
        assert calls == ["GET", "POST"]
    finally:
        current._client().close()


@pytest.mark.parametrize("status", [201, 303])
def test_successful_receipt_still_requires_owned_schema_proof_and_never_alias_get(tmp_path, status):
    journal = CreationJournal(tmp_path / "types.sqlite")
    posts = []

    def handler(request):
        if request.method == "POST":
            posts.append(True)
            return (httpx.Response(303, headers={"location": f"/input/v1/data_type/{TYPE_ID}"})
                if status == 303 else httpx.Response(201, json={"id": TYPE_ID, "userid": ACCOUNT}))
        if request.url.path == "/input/v1/data_type":
            return httpx.Response(200, json=[entry()] if posts else [])
        assert request.url.path == f"/data/v1/catalog/{TYPE_ID}/v1/schema"
        return httpx.Response(200, json=SCHEMA)

    current = client(handler)
    try:
        assert resolve_data_type(client=current, spec=spec(), account_id=ACCOUNT,
            allow_create=True, journal=journal).data_type == TYPE_ID
        original = journal.reservation(ACCOUNT, spec())
        journal.retain_receipt(original, TYPE_ID)
        assert journal.read(ACCOUNT, spec())[1:] == ("resolved", TYPE_ID)
        assert journal.receipts(original) == (TYPE_ID,)
        assert len(posts) == 1
    finally:
        current._client().close()


def test_known_receipt_reopens_its_original_uuid_after_failure_and_rename(tmp_path):
    path = tmp_path / "types.sqlite"
    posts, ready = [], [False]

    def handler(request):
        if request.method == "POST":
            posts.append(True)
            return httpx.Response(201, json={"id": TYPE_ID})
        if request.url.path == "/input/v1/data_type":
            if not posts:
                return httpx.Response(200, json=[])
            return (httpx.Response(200, json=[entry(name="Renamed synthetic receipt type")])
                if ready[0] else httpx.Response(503))
        return httpx.Response(200, json=SCHEMA)

    current = client(handler)
    try:
        with pytest.raises(httpx.HTTPStatusError):
            resolve_data_type(client=current, spec=spec(), account_id=ACCOUNT,
                allow_create=True, journal=CreationJournal(path))
        ready[0] = True
        assert resolve_data_type(client=current, spec=spec(), account_id=ACCOUNT,
            allow_create=True, journal=CreationJournal(path)).data_type == TYPE_ID
        assert len(posts) == 1
    finally:
        current._client().close()


@pytest.mark.parametrize("status", ["submitted", "resolved"])
@pytest.mark.parametrize("corruption", ["missing", "extra"])
@pytest.mark.parametrize("during", [False, True])
def test_missing_or_conflicting_receipt_cannot_be_healed_by_resolution(tmp_path, monkeypatch, status, corruption, during):
    journal = CreationJournal(tmp_path / "types.sqlite")
    original = journal.reserve(ACCOUNT, spec())
    journal.retain_receipt(original, TYPE_ID)
    if status == "resolved":
        journal.resolved(ACCOUNT, spec(), TYPE_ID, reservation=original)

    def corrupt():
        with journal._connect() as db:
            if corruption == "missing":
                db.execute("DELETE FROM creation_receipts")
            else:
                db.execute("INSERT INTO creation_receipts VALUES (?,?)", (original.nonce, SECOND_ID))

    if during:
        read, calls = journal.read, []
        def corrupt_after_final_preflight(account, definition):
            result = read(account, definition)
            calls.append(True)
            if len(calls) == 2:
                corrupt()
            return result
        monkeypatch.setattr(journal, "read", corrupt_after_final_preflight)
    else:
        corrupt()
    current = client(lambda request: httpx.Response(200,
        json=[entry()] if request.url.path == "/input/v1/data_type" else SCHEMA))
    try:
        with pytest.raises(ValueError):
            resolve_data_type(client=current, spec=spec(), account_id=ACCOUNT, journal=journal)
        with journal._connect() as db:
            assert db.execute("SELECT status,destination FROM creations").fetchone() == (status, TYPE_ID)
    finally:
        current._client().close()


@pytest.mark.parametrize("operation", ["reserve", "adopt"])
def test_missing_current_row_with_original_history_never_authorizes_new_creation(tmp_path, operation):
    journal = CreationJournal(tmp_path / "types.sqlite")
    original = journal.reserve(ACCOUNT, spec())
    with journal._connect() as db:
        db.execute("DELETE FROM creations")
    with pytest.raises(ValueError):
        if operation == "reserve":
            journal.reserve(ACCOUNT, spec())
        else:
            journal.resolved(ACCOUNT, spec(), SECOND_ID)
    with pytest.raises(ValueError):
        journal.read(ACCOUNT, spec())
    with pytest.raises(ValueError):
        journal.retain_receipt(original, TYPE_ID)
    assert journal.receipts(original) == (TYPE_ID,)


@pytest.mark.parametrize("table", ["creation_receipts", "creation_reservations", "creation_bindings"])
@pytest.mark.parametrize("reopen", [False, True])
def test_new_journal_metadata_loss_is_not_recreated_on_reopen(tmp_path, table, reopen):
    import sqlite3
    path = tmp_path / "types.sqlite"
    journal = CreationJournal(path)
    original = journal.reserve(ACCOUNT, spec())
    journal.retain_receipt(original, TYPE_ID)
    with sqlite3.connect(path) as db:
        db.execute(f"DROP TABLE {table}")
    with pytest.raises(ValueError):
        (CreationJournal(path) if reopen else journal).read(ACCOUNT, spec())
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT name FROM sqlite_master WHERE name=?", (table,)).fetchone() is None


def test_receipt_only_write_preserves_new_evidence_without_healing_missing_audit(tmp_path):
    journal = CreationJournal(tmp_path / "types.sqlite")
    original = journal.reserve(ACCOUNT, spec())
    journal.retain_receipt(original, TYPE_ID)
    with journal._connect() as db:
        db.execute("DELETE FROM creation_receipts")
    with pytest.raises(ValueError):
        journal.retain_receipt(original, TYPE_ID)
    assert journal.receipts(original) == (TYPE_ID,)
    with pytest.raises(ValueError):
        journal.resolved(ACCOUNT, spec(), TYPE_ID, reservation=original)


def test_unknown_lost_response_can_resolve_owned_proof_without_inventing_receipt(tmp_path):
    journal = CreationJournal(tmp_path / "types.sqlite")
    original = journal.reserve(ACCOUNT, spec())
    journal.resolved(ACCOUNT, spec(), TYPE_ID, reservation=original)
    assert journal.read(ACCOUNT, spec())[1:] == ("resolved", TYPE_ID)
    assert journal.receipts(original) == ()


def test_missing_active_original_reservation_cannot_look_like_a_legacy_resolved_binding(tmp_path):
    journal = CreationJournal(tmp_path / "types.sqlite")
    original = journal.reserve(ACCOUNT, spec())
    journal.retain_receipt(original, TYPE_ID)
    journal.resolved(ACCOUNT, spec(), TYPE_ID, reservation=original)
    with journal._connect() as db:
        db.execute("UPDATE creation_reservations SET active=0")
    with pytest.raises(ValueError):
        journal.read(ACCOUNT, spec())


def test_legacy_readonly_pending_has_no_new_reservation_and_does_not_write(tmp_path):
    import sqlite3
    path = tmp_path / "legacy.sqlite"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE creations (account TEXT,key TEXT,fingerprint TEXT,status TEXT,destination TEXT,PRIMARY KEY(account,key))")
        db.execute("INSERT INTO creations VALUES (?,?,?,'pending',NULL)", (ACCOUNT, spec().key, spec().fingerprint))
    path.chmod(0o600)
    before = path.read_bytes()
    journal = CreationJournal(path, read_only=True)
    assert journal.reservation(ACCOUNT, spec()) is None
    assert path.read_bytes() == before


@pytest.mark.parametrize("remove_receipt", [False, True])
def test_lost_original_reservation_cannot_masquerade_as_legacy_adoption(tmp_path, remove_receipt):
    journal = CreationJournal(tmp_path / "types.sqlite")
    original = journal.reserve(ACCOUNT, spec())
    journal.retain_receipt(original, TYPE_ID)
    journal.resolved(ACCOUNT, spec(), TYPE_ID, reservation=original)
    with journal._connect() as db:
        db.execute("DELETE FROM creation_reservations")
        if remove_receipt:
            db.execute("DELETE FROM creation_receipts")
    with pytest.raises(ValueError):
        journal.read(ACCOUNT, spec())


@pytest.mark.parametrize("damage", ["index", "version", "column"])
def test_versioned_metadata_damage_never_repaired_by_writable_open(tmp_path, damage):
    path = tmp_path / "types.sqlite"
    journal = CreationJournal(path)
    original = journal.reserve(ACCOUNT, spec())
    journal.retain_receipt(original, TYPE_ID)
    with sqlite3.connect(path) as db:
        if damage == "index":
            db.execute("DROP INDEX creation_active_reservation")
        elif damage == "version":
            db.execute("PRAGMA user_version=2")
        else:
            db.execute("ALTER TABLE creation_reservations DROP COLUMN has_receipt")
    before = path.read_bytes()
    with pytest.raises(ValueError):
        CreationJournal(path)
    assert path.read_bytes() == before
