"""Privacy carryover (B2) — a locally-rejected candidate leaves ZERO residue.

A message that q-hits the server query but is rejected by a local post-filter
must produce: no Files upload, no ledger entry, no bus directive, and no PII
(subject / from / body) in logs at ANY level.
"""
from __future__ import annotations

import logging

from fulcra_gmail.cursors import CursorStore
from fulcra_gmail.files_writer import FilesWriter
from fulcra_gmail.ledger import Ledger
from fulcra_gmail.pipeline import poll_account_rule
from fulcra_gmail.relay import RelayResult
from fulcra_gmail.rules import parse_rules

from .conftest import header, make_message


class FakeFilesApi:
    def __init__(self):
        self.uploads = []

    def upload_file(self, data, file_type, file_size, filepath):
        self.uploads.append((filepath, data.read()))
        return {"id": "f1"}


class FakeRelay:
    def __init__(self):
        self.emits = []

    def emit(self, directive):
        self.emits.append(directive)
        return RelayResult(ok=True, slug="s")

    def exists(self, directive):
        return True


class FakeClient:
    def __init__(self, messages):
        self._by_id = {m["id"]: m for m in messages}

    def list_message_ids(self, q):
        return list(self._by_id)

    def get_message(self, message_id, format="full"):  # noqa: A002
        return self._by_id.get(message_id)


SECRET_SUBJECT = "TotallySecretReceiptSubjectLine"
SECRET_FROM = "verysecretsender@example.com"
SECRET_BODY = "SecretBodyContentDoNotLog"


def _rejected_message():
    # q-hit (subject contains 'receipt') but rejected by from_regex (won't match).
    payload = {
        "mimeType": "text/plain",
        "headers": [
            header("Subject", SECRET_SUBJECT),
            header("From", SECRET_FROM),
        ],
        "body": {},
    }
    from .conftest import b64url
    payload["body"] = {"data": b64url(SECRET_BODY)}
    msg = make_message(msg_id="m1", payload=payload)
    return msg | {"internalDate": "1600000000000"}


def test_rejected_candidate_leaves_zero_residue_and_no_pii_in_logs(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    rule = parse_rules([{
        "id": "receipts", "version": 1, "name": "R",
        "match": "subject:receipt", "actions": ["file", "relay"],
        "relay_to": "agent:claude",
        # This from_regex will NOT match SECRET_FROM → rejected post-filter.
        "from_regex": r"@trusted-domain\.example$",
    }])[0]
    ledger = Ledger("acct-1", root=tmp_path)
    files_api = FakeFilesApi()
    relay = FakeRelay()
    cursors = CursorStore("acct-1", root=tmp_path)
    client = FakeClient([_rejected_message()])

    result = poll_account_rule(
        client=client, rule=rule, account_id="acct-1", ledger=ledger,
        cursors=cursors, files_writer=FilesWriter(files_api), relay_emitter=relay,
        now_epoch=1_600_002_000,
    )

    # Zero effective matches → zero residue everywhere.
    assert result.effective == 0
    assert files_api.uploads == []
    assert relay.emits == []
    assert ledger.entries() == []
    assert not ledger.path.exists()

    # No PII in logs at ANY level.
    blob = "\n".join(r.getMessage() for r in caplog.records)
    assert SECRET_SUBJECT not in blob
    assert SECRET_FROM not in blob
    assert SECRET_BODY not in blob
