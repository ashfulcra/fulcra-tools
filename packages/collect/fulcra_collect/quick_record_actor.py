"""Opt-in interactive action executor; daemon/client cutover is separate.

Prepared choices supply a pure payload builder. A record's first dispatch intent
is durable before POST. A timeout thereafter permits exact-ID reconciliation,
never another create POST. Native Undo starts a new current-authority action and
requires a positively verified original. No legacy sentinel is emitted.
"""
from __future__ import annotations

from datetime import datetime

from fulcra_common.v1 import record_matches

from .quick_record_v1 import QuickRecordSession
from .record_actions import ActionJournal, RecordAction


class QuickRecordActor:
    def __init__(self, session: QuickRecordSession, journal: ActionJournal):
        self.session = session
        self.journal = journal

    def _current(self, action_id):
        self.session.guard(mutating=True)
        action = self.journal.get(action_id, account_id=self.session.account_id, epoch=self.session.epoch)
        if action is None:
            raise ValueError("quick-record action is missing")
        return action

    def _advance(self, action: RecordAction, status: str, evidence: dict):
        return self.session.commit(lambda: self.journal.advance(action.action_id,
            account_id=self.session.account_id, epoch=self.session.epoch,
            expected_status=action.status, status=status, evidence=evidence))

    def record(self, *, action_id: str, request: dict, build_payload) -> RecordAction:
        """Freeze one prepared choice before effects; repeats preserve its intent."""
        with self.session.record_write_scope():
            action = self.journal.capture(action_id=action_id, account_id=self.session.account_id,
                epoch=self.session.epoch, operation="record", request=request, build_payload=build_payload)
            return self._record(action, allow_submit=True)

    def reconcile(self, action_id: str) -> RecordAction:
        """Read the existing intent; this entry point never submits a create."""
        with self.session.record_write_scope():
            action = self._current(action_id)
            if action.operation == "delete":
                return self._undo(action)
            return self._record(action, allow_submit=False)

    def _record(self, action, *, allow_submit):
        if action.status in {"verified", "excluded", "cancelled"}:
            return action
        destination, row = action.payload["data_type"], action.payload["record"]
        # Prove the complete payload before policy discovery can create metadata.
        self.session.validate_action_record(destination, row)
        blocked = self.session.excluded_ids(destination, [row["id"]], check_only=False)
        if blocked:
            if action.status == "pending":
                return self._advance(action, "excluded", {"exclusion": "verified", "never_submitted": True})
            # A prior uncertain upload may still be indexing. Suppression alone
            # cannot prove its final absence or authorize a second create.
            return action
        actual = self.session.get_record(destination, row["id"])
        if actual is not None:
            if not record_matches(actual, row):
                raise ValueError("quick-record readback mismatch; original action retained")
        elif action.status == "pending" and allow_submit:
            def dispatch_intent():
                nonlocal action
                action = self._advance(action, "submitted", {"phase": "dispatch-intent"})

            receipt = self.session.submit_action_record(destination, row, before_submit=dispatch_intent)
            upload_id = receipt.get("upload_id") if isinstance(receipt, dict) else None
            if not isinstance(upload_id, str) or not upload_id or len(upload_id) > 1024:
                raise ValueError("quick-record receipt is unreadable; reconcile the original action")
            action = self._advance(action, "submitted", {"phase": "receipt", "upload_id": upload_id})
            actual = self.session.get_record(destination, row["id"])
            if actual is not None and not record_matches(actual, row):
                raise ValueError("quick-record readback mismatch; original action retained")
        if actual is None:
            return action
        # A policy arriving while the POST was indexing prevents a success claim.
        if self.session.excluded_ids(destination, [row["id"]], check_only=False):
            return action
        return self._advance(action, "verified", {"readback": "exact_fields", "record_id": row["id"]})

    def undo(self, *, action_id: str, record_action_id: str) -> RecordAction:
        """Authorize a new deletion of this account's verified original capture."""
        with self.session.record_write_scope():
            original = self.journal.inspect(record_action_id, account_id=self.session.account_id)
            if original is None or original.operation != "record" or original.status != "verified":
                raise ValueError("Undo requires a verified original quick-record action")
            action = self.journal.capture(action_id=action_id, account_id=self.session.account_id,
                epoch=self.session.epoch, operation="delete", request={"record_action_id": record_action_id},
                build_payload=lambda _: {"data_type": original.payload["data_type"],
                                        "record_id": original.payload["record"]["id"]})
            return self._undo(action)

    def _undo(self, action):
        if action.status in {"verified", "cancelled"}:
            return action
        original = self.journal.inspect(action.request.get("record_action_id"), account_id=self.session.account_id)
        if (original is None or original.operation != "record" or original.status != "verified"
                or original.payload["data_type"] != action.payload["data_type"]
                or original.payload["record"]["id"] != action.payload["record_id"]):
            raise ValueError("Undo requires its verified original quick-record action")
        destination, identity = action.payload["data_type"], action.payload["record_id"]
        actual = self.session.get_record(destination, identity)
        if actual is not None and not record_matches(actual, original.payload["record"]):
            raise ValueError("quick-record readback mismatch; Undo action retained")
        exclusion = self.session.exclude_action_record(data_type=destination, record_id=identity,
            action_id=action.action_id, requested_at=datetime.fromisoformat(action.created_at),
            evidence=["quick-record-action:" + original.action_id])
        if actual is not None:
            action = self._advance(action, "submitted", {"phase": "native-delete-intent",
                                                        "exclusion_id": exclusion["id"]})
            self.session.delete_records(destination, [identity], action_id=action.action_id)
        if self.session.get_record(destination, identity) is not None:
            return action
        # Re-prove the permanent policy after native absence, not just its receipt.
        if identity not in self.session.excluded_ids(destination, [identity], check_only=True):
            raise RuntimeError("Undo exclusion changed; deletion remains unverified")
        return self._advance(action, "verified", {"readback": "native_absence",
                                                 "exclusion_id": exclusion["id"]})
