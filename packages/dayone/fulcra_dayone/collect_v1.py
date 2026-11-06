"""Import-once Journal writer with immutable, account-bound pending batches."""
import json
from contextlib import nullcontext
from datetime import datetime

from fulcra_collect.v1_outbox import V1RecordOutbox
from fulcra_common.client import ImportResult
from fulcra_common.v1 import _destination, _utc

from .v1 import JOURNAL_SPEC, build_journal_record

PENDING_KEY = "v1_dayone_pending"
_BINDING_KEY = "v1_dayone_destination"
_PREFLIGHT_TYPE = "Event/00000000-0000-0000-0000-000000000000"


class JournalWriter:
    def __init__(self, ctx, session):
        self.ctx, self.session = ctx, session
        self.outbox = V1RecordOutbox(ctx, session, key=PENDING_KEY)
        self.excluded_count = 0

    def resume(self):
        return self.outbox.drain()

    def _destination(self, *, preview):
        configured = self.ctx.config.get("v1_destination_ids", {})
        cached = self.ctx.kv_get(_BINDING_KEY, {})
        if not isinstance(configured, dict) or set(configured) - {"journal"} or not isinstance(cached, dict):
            raise ValueError("Day One v1 destination mapping is malformed")
        for pin in configured.values():
            _destination(pin, "Event")
        prior = cached if cached.get("account_id") == self.session.account_id else {}
        legacy = self.ctx.state.definition_id
        if legacy and "journal" not in configured and (
                prior.get("legacy_definition_id") != legacy or not prior.get("data_type")):
            raise RuntimeError("map the existing Day One annotation selection to v1 before collecting")
        resolved = self.session.resolve_destination(JOURNAL_SPEC,
            pinned_id=configured.get("journal", prior.get("data_type")),
            allow_create=not preview and "journal" not in configured)
        if not preview:
            self.session.commit(lambda: self.ctx.kv_set(_BINDING_KEY,
                {"account_id": self.session.account_id, "data_type": resolved.data_type,
                 "legacy_definition_id": legacy}))
        return resolved.data_type

    def run_import(self, entries, *, preview=False, chunk_size=500):
        if type(preview) is not bool or type(chunk_size) is not int or chunk_size <= 0:
            raise ValueError("Day One preview/chunk settings are invalid")
        # Validate the complete source, including collisions in later chunks,
        # before destination/tag/control creation. No source text is logged.
        unique = {}
        for entry in entries:
            row = build_journal_record(entry, _PREFLIGHT_TYPE)
            signature = json.dumps(row, sort_keys=True, allow_nan=False)
            if row["id"] in unique and unique[row["id"]][1] != signature:
                raise ValueError("conflicting Day One entries share one source identity")
            unique[row["id"]] = (entry, signature, _utc(entry.creation_date))
        if not unique:
            return ImportResult(0, 0, 0, 0)
        destination = self._destination(preview=preview)
        ordered = sorted(unique.values(), key=lambda item: item[2])
        posted = skipped = verified = 0
        for offset in range(0, len(ordered), chunk_size):
            chunk = ordered[offset:offset + chunk_size]
            records = [build_journal_record(item[0], destination) for item in chunk]
            scope = nullcontext() if preview else self.session.record_write_scope()
            with scope:
                ids = [row["id"] for row in records]
                before = self.session.excluded_ids(destination, ids, check_only=preview)
                actual = self.session.get_records(destination, ids)
                after = self.session.excluded_ids(destination, ids, check_only=preview)
                if before - after:
                    raise RuntimeError("Day One permanent exclusion changed; retry after reconciliation")
                self.excluded_count += len(after)
                pending = []
                for (entry, _, _), row in zip(chunk, records):
                    if row["id"] in after:
                        continue
                    if row["id"] in actual:
                        sources = actual[row["id"]].get("sources")
                        if not isinstance(sources, list) or row["sources"][0] not in sources:
                            raise ValueError("existing Journal ID has a conflicting source identity")
                        # Import-once: exact-ID presence preserves already
                        # imported edits/tags rather than rewriting from source.
                        skipped += 1
                        continue
                    if not preview:
                        tags = [self.session.resolve_tag(name) for name in entry.tags]
                        row = build_journal_record(entry, destination, tags=tags)
                    pending.append(row)
                if preview:
                    posted += len(pending)
                elif pending:
                    self.outbox.stage({destination: pending}, message=f"Day One: {len(pending)} entries verified",
                        observed_at=max(datetime.fromisoformat(row["start_time"]) for row in pending).isoformat().replace("+00:00", "Z"))
                    count = self.outbox.drain()
                    posted += count
                    verified += count
                    self.excluded_count += len(self.outbox.excluded_ids.get(destination, ()))
            self.session.guard(mutating=not preview)
        return ImportResult(len(unique), skipped, posted, verified)
