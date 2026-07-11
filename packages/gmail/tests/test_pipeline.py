"""Pipeline — crash-ordering invariant + B1 contiguous-frontier cursor.

Synthetic messages only. Injected crash points exercise the file→ledger→relay→
ledger ordering; a fake client feeds out-of-order pages and controllable relay
failures to drive the frontier logic.
"""
from __future__ import annotations

import pytest

from fulcra_gmail.cursors import CursorStore
from fulcra_gmail.files_writer import FilesWriter
from fulcra_gmail.ledger import ACTION_FILE, ACTION_RELAY, Ledger
from fulcra_gmail.pipeline import (
    InjectedCrash,
    poll_account_rule,
    process_message,
)
from fulcra_gmail.relay import RelayResult, build_directive
from fulcra_gmail.rules import parse_rules

from .conftest import header, make_message


# --- fakes -----------------------------------------------------------------


class FakeFilesApi:
    def __init__(self):
        self.uploads = []
        self.store = {}

    def upload_file(self, data, file_type, file_size, filepath):
        body = data.read()
        self.uploads.append((filepath, body))
        self.store[filepath] = body
        return {"id": f"f{len(self.uploads)}"}


class FakeRelay:
    """In-memory relay that dedupes by directive identity (coord semantics)."""

    def __init__(self, *, fail=False):
        self.emits = []          # every emit call (including retries)
        self.visible = {}        # identity -> directive (deduped)
        self.fail = fail

    def _ident(self, d):
        return (d.title, d.summary, d.next_action, d.assignee)

    def emit(self, directive):
        self.emits.append(directive)
        if self.fail:
            return RelayResult(ok=False, reason="forced")
        self.visible[self._ident(directive)] = directive
        return RelayResult(ok=True, slug="slug-x")

    def exists(self, directive):
        return self._ident(directive) in self.visible


def _rule(actions=("file", "relay"), **over):
    raw = {
        "id": "receipts", "version": 1, "name": "Receipts",
        "match": "subject:receipt", "actions": list(actions),
        "relay_to": "agent:claude", "relay_priority": "P1",
    }
    raw.update(over)
    return parse_rules([raw])[0]


def _msg(mid, internal_ms, subject="Receipt"):
    return make_message(
        msg_id=mid, thread_id="t",
        headers=[header("Subject", subject), header("From", "shop@example.com")],
    ) | {"internalDate": str(internal_ms)}


def _crash_at(label):
    def hook(seen):
        if seen == label:
            raise InjectedCrash(label)
    return hook


def _deps(tmp_path, *, relay=None):
    ledger = Ledger("acct-1", root=tmp_path)
    files = FilesWriter(FakeFilesApi())
    relay = relay or FakeRelay()
    return ledger, files, relay


def _visible_directive_count(relay):
    return len(relay.visible)


# --- crash-ordering invariant ----------------------------------------------


@pytest.mark.parametrize(
    "crash_label",
    ["before_first_effect", "after_file_done", "after_relay_pending",
     "after_relay_emit", "after_relay_done"],
)
def test_crash_then_resume_yields_exactly_one_directive_and_one_file(tmp_path, crash_label):
    ledger, files, relay = _deps(tmp_path)
    rule = _rule()
    msg = _msg("m1", 1_600_000_000_000)

    # First attempt crashes at the injected point.
    with pytest.raises(InjectedCrash):
        process_message(msg, rule=rule, account_id="acct-1", ledger=ledger,
                        files_writer=files, relay_emitter=relay,
                        crash=_crash_at(crash_label))

    # Resume (no crash) — must complete.
    done = process_message(msg, rule=rule, account_id="acct-1", ledger=ledger,
                           files_writer=files, relay_emitter=relay)
    assert done is True

    # Exactly one visible directive, regardless of where the crash happened.
    assert _visible_directive_count(relay) == 1
    # The file is written at most... its content is identical; the point is we
    # never re-file AFTER a file-done barrier. A crash before file-done may
    # write once on resume; a crash after it must not re-file.
    unique_paths = {p for p, _ in files._api.uploads}
    assert len(unique_paths) == 1  # one message → one path
    if crash_label in ("after_file_done", "after_relay_pending",
                       "after_relay_emit", "after_relay_done"):
        # File-done was durable before the crash → resume must NOT re-upload.
        assert len(files._api.uploads) == 1


def test_fully_done_message_is_noop_on_reprocess(tmp_path):
    ledger, files, relay = _deps(tmp_path)
    rule = _rule()
    msg = _msg("m1", 1_600_000_000_000)
    assert process_message(msg, rule=rule, account_id="acct-1", ledger=ledger,
                           files_writer=files, relay_emitter=relay)
    # Reprocess: no new uploads, no new emits.
    before_uploads = len(files._api.uploads)
    before_emits = len(relay.emits)
    assert process_message(msg, rule=rule, account_id="acct-1", ledger=ledger,
                           files_writer=files, relay_emitter=relay)
    assert len(files._api.uploads) == before_uploads
    assert len(relay.emits) == before_emits


def test_relay_failure_leaves_message_incomplete(tmp_path):
    ledger, files, relay = _deps(tmp_path, relay=FakeRelay(fail=True))
    rule = _rule()
    msg = _msg("m1", 1_600_000_000_000)
    done = process_message(msg, rule=rule, account_id="acct-1", ledger=ledger,
                           files_writer=files, relay_emitter=relay)
    assert done is False
    # File-done recorded, relay NOT done → remaining is exactly [relay].
    assert ledger.remaining_actions("m1", "receipts", 1, [ACTION_FILE, ACTION_RELAY]) == [ACTION_RELAY]


def test_relay_rule_with_no_backend_stays_blocked_until_restored(tmp_path):
    # P1-b regression: a ["file","relay"] rule with NO relay backend must NOT
    # silently mark the message done. It stays INCOMPLETE (cursor blocked) until
    # a relay backend is configured, then completes — the email is never lost.
    ledger, files, _ = _deps(tmp_path)
    cursors = CursorStore("acct-1", root=tmp_path)
    rule = _rule(actions=("file", "relay"))
    m1 = _msg("m1", 1_600_000_000_000)

    # Run 1: no relay backend → blocked, cursor does NOT advance.
    r1 = poll_account_rule(
        client=FakeClient([m1]), rule=rule, account_id="acct-1", ledger=ledger,
        cursors=cursors, files_writer=files, relay_emitter=None,
        now_epoch=1_600_002_000,
    )
    assert r1.blocked is True
    assert r1.cursor is None  # never advanced past the un-relayed message
    # The relay is still owed (not marked done); file may have been written once.
    assert ledger.remaining_actions("m1", "receipts", 1,
                                    [ACTION_FILE, ACTION_RELAY]) == [ACTION_RELAY]

    # Run 2: relay backend restored → completes and the cursor advances.
    relay = FakeRelay()
    r2 = poll_account_rule(
        client=FakeClient([m1]), rule=rule, account_id="acct-1", ledger=ledger,
        cursors=cursors, files_writer=files, relay_emitter=relay,
        now_epoch=1_600_003_000,
    )
    assert not r2.blocked
    assert r2.cursor == 1_600_000_000
    assert _visible_directive_count(relay) == 1
    # File was written exactly once across both runs (never re-filed).
    assert len(files._api.uploads) == 1


def test_relay_only_rule_skips_file(tmp_path):
    ledger, files, relay = _deps(tmp_path)
    rule = _rule(actions=("relay",))
    msg = _msg("m1", 1_600_000_000_000)
    assert process_message(msg, rule=rule, account_id="acct-1", ledger=ledger,
                           files_writer=files, relay_emitter=relay)
    assert files._api.uploads == []
    assert _visible_directive_count(relay) == 1


# --- B1 contiguous-frontier cursor -----------------------------------------


class FakeClient:
    """Serves ids out of order and messages by id; controllable relay behavior
    is on the relay fake, not here."""

    def __init__(self, messages, *, id_order=None):
        self._by_id = {m["id"]: m for m in messages}
        self._order = id_order or list(self._by_id)
        self.queries = []

    def list_message_ids(self, q):
        self.queries.append(q)
        return list(self._order)

    def get_message(self, message_id, format="full"):  # noqa: A002
        return self._by_id.get(message_id)


class FetchFailingClient(FakeClient):
    """A client whose ``get_message`` returns None for a chosen id (a transient
    per-message fetch failure / mid-run auth failure)."""

    def __init__(self, messages, *, fail_ids, id_order=None):
        super().__init__(messages, id_order=id_order)
        self.fail_ids = set(fail_ids)

    def get_message(self, message_id, format="full"):  # noqa: A002
        if message_id in self.fail_ids:
            return None
        return self._by_id.get(message_id)


def test_unresolved_fetch_is_a_frontier_hole_cursor_holds(tmp_path):
    # P1-a: the oldest listed id fails to fetch; a newer id succeeds. The cursor
    # must NOT advance past the unresolved (never-evaluated) id — a second run
    # re-attempts it.
    ledger, files, relay = _deps(tmp_path)
    cursors = CursorStore("acct-1", root=tmp_path)
    rule = _rule()
    m_old = _msg("m_old", 1_600_000_000_000)
    m_new = _msg("m_new", 1_600_000_900_000)

    failing = FetchFailingClient([m_old, m_new], fail_ids={"m_old"},
                                 id_order=["m_new", "m_old"])
    r1 = poll_account_rule(
        client=failing, rule=rule, account_id="acct-1", ledger=ledger,
        cursors=cursors, files_writer=files, relay_emitter=relay,
        now_epoch=1_600_002_000,
    )
    assert r1.unresolved == 1
    assert r1.blocked is True
    # Newer one was processed, but the cursor did NOT advance past the hole.
    assert r1.cursor is None
    assert _visible_directive_count(relay) == 1  # only the newer relayed

    # Run 2: fetch of the older id now succeeds → it processes and the frontier
    # advances past both.
    healthy = FakeClient([m_old, m_new], id_order=["m_new", "m_old"])
    r2 = poll_account_rule(
        client=healthy, rule=rule, account_id="acct-1", ledger=ledger,
        cursors=cursors, files_writer=files, relay_emitter=relay,
        now_epoch=1_600_003_000,
    )
    assert r2.unresolved == 0
    assert not r2.blocked
    assert r2.cursor == 1_600_000_900
    assert _visible_directive_count(relay) == 2  # older one now relayed too


def test_frontier_processes_oldest_first_across_out_of_order_pages(tmp_path):
    ledger, files, relay = _deps(tmp_path)
    cursors = CursorStore("acct-1", root=tmp_path)
    rule = _rule()
    m_old = _msg("m_old", 1_600_000_000_000)
    m_mid = _msg("m_mid", 1_600_000_500_000)
    m_new = _msg("m_new", 1_600_000_900_000)
    # API returns them shuffled (newest, oldest, middle).
    client = FakeClient([m_old, m_mid, m_new], id_order=["m_new", "m_old", "m_mid"])

    result = poll_account_rule(
        client=client, rule=rule, account_id="acct-1", ledger=ledger,
        cursors=cursors, files_writer=files, relay_emitter=relay,
        now_epoch=1_600_002_000,
    )
    assert result.effective == 3
    assert result.processed == 3
    assert not result.blocked
    # Cursor advanced to the NEWEST candidate's internalDate (seconds), not now.
    assert result.cursor == 1_600_000_900
    assert _visible_directive_count(relay) == 3


def test_frontier_stops_at_failed_older_candidate(tmp_path):
    # A FAILED older candidate + a SUCCESSFUL newer one: the watermark must NOT
    # advance past the incomplete older one (no hole skipped).
    ledger, files = Ledger("acct-1", root=tmp_path), FilesWriter(FakeFilesApi())
    cursors = CursorStore("acct-1", root=tmp_path)
    rule = _rule()
    m_old = _msg("m_old", 1_600_000_000_000)
    m_new = _msg("m_new", 1_600_000_900_000)

    # Relay fails ONLY for the older message's outbox.
    old_key = build_directive(
        __import__("fulcra_gmail.ledger", fromlist=["outbox_key"]).outbox_key(
            "acct-1", "m_old", "receipts", 1), rule).outbox_key

    class SelectiveRelay(FakeRelay):
        def emit(self, directive):
            self.emits.append(directive)
            if directive.outbox_key == old_key:
                return RelayResult(ok=False, reason="forced-old")
            self.visible[self._ident(directive)] = directive
            return RelayResult(ok=True, slug="s")

    relay = SelectiveRelay()
    client = FakeClient([m_old, m_new], id_order=["m_new", "m_old"])
    result = poll_account_rule(
        client=client, rule=rule, account_id="acct-1", ledger=ledger,
        cursors=cursors, files_writer=files, relay_emitter=relay,
        now_epoch=1_600_002_000,
    )
    assert result.blocked is True
    # Newer one WAS processed (over-capture) but cursor did NOT advance past the
    # incomplete older one — it stays unset (first run, no contiguous prefix).
    assert result.cursor is None
    # The newer message's relay is visible; the older's is not.
    assert _visible_directive_count(relay) == 1


def test_second_run_retries_incomplete_and_advances(tmp_path):
    # After a failed-older run, a healthy re-run completes the older one and the
    # cursor advances past both (no re-file/re-relay of the newer one).
    ledger, files = Ledger("acct-1", root=tmp_path), FilesWriter(FakeFilesApi())
    cursors = CursorStore("acct-1", root=tmp_path)
    rule = _rule()
    m_old = _msg("m_old", 1_600_000_000_000)
    m_new = _msg("m_new", 1_600_000_900_000)
    old_key = build_directive(
        __import__("fulcra_gmail.ledger", fromlist=["outbox_key"]).outbox_key(
            "acct-1", "m_old", "receipts", 1), rule).outbox_key

    class SelectiveRelay(FakeRelay):
        def __init__(self):
            super().__init__()
            self.block_old = True

        def emit(self, directive):
            self.emits.append(directive)
            if self.block_old and directive.outbox_key == old_key:
                return RelayResult(ok=False, reason="forced")
            self.visible[self._ident(directive)] = directive
            return RelayResult(ok=True, slug="s")

    relay = SelectiveRelay()
    client = FakeClient([m_old, m_new], id_order=["m_new", "m_old"])
    poll_account_rule(client=client, rule=rule, account_id="acct-1", ledger=ledger,
                      cursors=cursors, files_writer=files, relay_emitter=relay,
                      now_epoch=1_600_002_000)
    uploads_after_first = len(files._api.uploads)

    # Heal the relay and re-run.
    relay.block_old = False
    result = poll_account_rule(client=client, rule=rule, account_id="acct-1",
                               ledger=ledger, cursors=cursors, files_writer=files,
                               relay_emitter=relay, now_epoch=1_600_003_000)
    assert not result.blocked
    assert result.cursor == 1_600_000_900  # advanced past both
    # The newer message was NOT re-filed (still 2 unique uploads total).
    assert len(files._api.uploads) == uploads_after_first  # no new uploads (only relay retried)
    assert _visible_directive_count(relay) == 2


def test_backfill_wider_than_overlap_is_safe(tmp_path):
    ledger, files, relay = _deps(tmp_path)
    cursors = CursorStore("acct-1", root=tmp_path)
    rule = _rule(backfill=30)  # 30-day first-run window, wider than 24h overlap
    m1 = _msg("m1", 1_600_000_000_000)
    client = FakeClient([m1])
    result = poll_account_rule(client=client, rule=rule, account_id="acct-1",
                               ledger=ledger, cursors=cursors, files_writer=files,
                               relay_emitter=relay, now_epoch=1_600_002_000)
    # First-run query used the backfill window.
    assert "newer_than:30d" in client.queries[0]
    assert result.processed == 1
    assert result.cursor == 1_600_000_000


def test_cross_overlap_dedupe_skips_already_done(tmp_path):
    # A message fully done on run 1 reappears in run 2's overlap window and is
    # skipped (no re-file, no re-relay).
    ledger, files, relay = _deps(tmp_path)
    cursors = CursorStore("acct-1", root=tmp_path)
    rule = _rule()
    m1 = _msg("m1", 1_600_000_000_000)
    client = FakeClient([m1])
    poll_account_rule(client=client, rule=rule, account_id="acct-1", ledger=ledger,
                      cursors=cursors, files_writer=files, relay_emitter=relay,
                      now_epoch=1_600_002_000)
    uploads1, emits1 = len(files._api.uploads), len(relay.emits)
    poll_account_rule(client=client, rule=rule, account_id="acct-1", ledger=ledger,
                      cursors=cursors, files_writer=files, relay_emitter=relay,
                      now_epoch=1_600_090_000)
    assert len(files._api.uploads) == uploads1  # no re-file
    assert len(relay.emits) == emits1           # no re-relay
