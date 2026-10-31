"""One pending v1 batch per daemon-serialized plugin worker.

The worker's dedicated SQLite outbox table holds a staged immutable payload before
any POST. Only exact visibility authorizes a receipt and clearing that payload.
Account/epoch changes retain old pending data for explicit reconciliation.
"""
from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID
from fulcra_common.record_delivery import SourceDispatch, prepared_source_dispatch

from fulcra_common.json_values import snapshot_json
from fulcra_common.v1 import _destination, record_matches

from .config import settings_fingerprint


def source_settings_fingerprint(ctx, session) -> str:
    """Use captured raw consent, including manual edits that bypass epochs."""
    captured = getattr(session, "settings_fingerprint", None)
    if captured is not None:
        return captured
    raw = getattr(ctx, "_persisted_config", None)
    return settings_fingerprint(getattr(ctx, "config", {}) if raw is None else raw)


def has_pending_records(ctx, *, key: str = "v1_pending_records") -> bool:
    """Read-only reconfiguration gate; unknown state is never clean-empty."""
    pending = ctx.outbox_get(key, None)
    if pending is None:
        return False
    if not isinstance(pending, dict) or not isinstance(pending.get("groups"), list):
        raise ValueError("pending record state is unreadable")
    return bool(pending["groups"])


def _groups(groups: list) -> list[dict]:
    """Validate/snapshot the entire local envelope before any network call."""
    if not isinstance(groups, list):
        raise ValueError("v1 outbox groups are malformed")
    try:
        snapshot = snapshot_json(groups)
    except (ValueError, TypeError):
        raise ValueError("v1 outbox requires finite JSON records") from None
    destinations = set()
    result = []
    for group in snapshot:
        if not isinstance(group, dict) or not isinstance(group.get("records"), list):
            raise ValueError("v1 outbox group is malformed")
        destination = _destination(group.get("data_type"))
        if destination in destinations:
            raise ValueError("v1 outbox repeats a destination")
        destinations.add(destination)
        unique = {}
        for record in group["records"]:
            if not isinstance(record, dict):
                raise ValueError("v1 outbox record is malformed")
            try:
                identity = str(UUID(record.get("id")))
            except (ValueError, TypeError, AttributeError):
                raise ValueError("v1 outbox record ID is invalid") from None
            if record["id"] != identity:
                raise ValueError("v1 outbox record ID must be canonical")
            if identity in unique and not (record_matches(record, unique[identity])
                                           and record_matches(unique[identity], record)):
                raise ValueError("v1 outbox has conflicting payloads for one ID")
            unique[identity] = record
        if unique:
            result.append({"data_type": destination, "records": list(unique.values())})
    return result


def dispatch_binding(pending, destination, owner=None):
    original = {k: v for k, v in (pending if owner is None else owner).items()
                if k not in {"dispatch", "dispatch_version"}}
    return {"scope": {k: pending.get(k) for k in ("account_id", "epoch", "settings_fingerprint")},
            "original": original, "destination": destination}


def dispatch_states(pending, records, *, owner=None):
    return {destination: prepared_source_dispatch(dispatch_binding(pending, destination, owner), batch)
            for destination, batch in records.items()}


def pending_dispatch(pending, owner, destination, records, *, load, save, validate):
    """Construct a dispatch guard without adopting or rewriting legacy state."""
    version = owner.get("dispatch_version")
    if "dispatch_version" not in owner:
        if "dispatch" in owner:
            raise RuntimeError("source dispatch version UNKNOWN; retain original pending batch")
        state = None
    elif type(version) is not int or version != 1 or not isinstance(owner.get("dispatch"), dict) or destination not in owner["dispatch"]:
        raise RuntimeError("source dispatch version/state UNKNOWN; retain original pending batch")
    else:
        expected = ({g["data_type"] for g in owner["groups"]} if owner is pending else set(owner["records"]))
        if set(owner["dispatch"]) != expected:
            raise RuntimeError("source dispatch destinations UNKNOWN; retain original pending batch")
        state = owner["dispatch"][destination]
        if state is None:
            raise RuntimeError("source dispatch state UNKNOWN; retain original pending batch")

    def persist(updated):
        validate()
        if load() != pending:
            raise RuntimeError("original pending source changed; retain batch for reconciliation")
        candidate = snapshot_json(pending)
        # Locate this owner by identity, preserving every original payload byte/value.
        target = candidate if owner is pending else candidate["items"][pending["items"].index(owner)]
        target["dispatch"][destination] = updated
        save(candidate)
        if load() != candidate:
            raise RuntimeError("source dispatch checkpoint UNKNOWN; retain original pending batch")
        owner["dispatch"][destination] = snapshot_json(updated)

    return SourceDispatch(dispatch_binding(pending, destination, None if owner is pending else owner), records, state, persist)


@dataclass(frozen=True)
class _DrainProgress:
    """Validated complete groups only; never delivery/commit authority."""

    verified: int = 0
    excluded: int = 0


class V1RecordOutbox:
    """Retain a captured batch until a guarded commit proves it visible.

    The daemon already permits one worker per plugin. Call ``drain`` before
    fetching the next source sample and before staging another batch. Helpers
    do not infer that an upload receipt, local claim or count proves delivery.
    """

    def __init__(self, ctx, session, *, key: str = "v1_pending_records"):
        self.ctx = ctx
        self.session = session
        self.key = key
        self.excluded_ids = {}
        self._progress = None
        # Small injected contexts can supply a virtual store; real RunContext
        # uses worker-bound outbox seams and never falls back to cursor KV.
        self._get = ctx.outbox_get if hasattr(ctx, "outbox_get") else ctx.kv_get
        self._set = ctx.outbox_set if hasattr(ctx, "outbox_set") else ctx.kv_set

    def _pending(self) -> dict:
        pending = self._get(self.key, None)
        if pending is None:
            return {"groups": []}
        if not isinstance(pending, dict):
            raise ValueError("v1 outbox is malformed")
        groups = _groups(pending.get("groups"))
        if groups:
            if pending.get("account_id") != self.session.account_id:
                raise RuntimeError("pending v1 outbox belongs to another account; reconcile before continuing")
            if (pending.get("epoch") != self.ctx.config_epoch
                    or pending.get("settings_fingerprint") != source_settings_fingerprint(self.ctx, self.session)):
                raise RuntimeError("pending v1 outbox source configuration changed; reconcile before continuing")
            if (not isinstance(pending.get("message"), str)
                    or not isinstance(pending.get("observed_at"), (str, type(None)))):
                raise ValueError("v1 outbox receipt is malformed")
        return snapshot_json(pending)

    def pending_destinations(self) -> tuple[str, ...]:
        """Immutable destinations from a fully validated, authority-fenced batch.

        No payload or mutable envelope escapes. Unknown local state raises;
        consumers must prove every returned target before recovery delivery.
        """
        pending = self._pending()
        return tuple(group["data_type"] for group in _groups(pending["groups"]))

    def stage(self, records: dict[str, list[dict]], *, message: str, observed_at: str | None = None) -> None:
        if not isinstance(message, str) or not isinstance(observed_at, (str, type(None))):
            raise ValueError("v1 outbox receipt is malformed")
        groups = _groups([{"data_type": destination, "records": batch} for destination, batch in records.items()])
        if not groups:
            return

        def persist():
            if self._pending()["groups"]:
                raise RuntimeError("a pending v1 outbox must be reconciled before staging another batch")
            pending = {"account_id": self.session.account_id, "epoch": self.ctx.config_epoch,
                                       "settings_fingerprint": source_settings_fingerprint(self.ctx, self.session),
                                       "groups": groups, "message": message, "observed_at": observed_at}
            pending["dispatch_version"] = 1
            pending["dispatch"] = dispatch_states(pending, {g["data_type"]: g["records"] for g in groups})
            self._set(self.key, pending)

        with self.session.record_write_scope():
            self.session.commit(persist)

    @property
    def progress(self):
        """Immutable lower bound for the last drain, including failed drains.

        Access is local memory only, including after session cleanup. The caller
        supplies serialization; this snapshot cannot authorize any mutation.
        """
        return self._progress

    def drain(self) -> int:
        # Entry fences can raise before _drain; never retain recovery evidence.
        self._progress = _DrainProgress()
        self.excluded_ids = {}
        with self.session.record_write_scope():
            return self._drain()

    def _drain(self) -> int:
        pending = self._pending()
        if not pending["groups"]:
            return 0
        count = 0
        for group in pending["groups"]:
            destination, records = group["data_type"], group["records"]
            dispatch = pending_dispatch(pending, pending, destination, records,
                load=lambda: self._get(self.key, None), save=lambda value: self._set(self.key, value),
                validate=lambda: self.session.commit(self._pending))
            result = self.session.deliver_records(destination, records, dispatch=dispatch)
            try:
                verified, excluded = tuple(result.verified_ids), tuple(result.excluded_ids)
                wanted = {r["id"] for r in records}
                if (len(verified) != len(set(verified)) or len(excluded) != len(set(excluded))
                        or set(verified) & set(excluded) or set(verified) | set(excluded) != wanted):
                    raise ValueError
            except (ValueError, TypeError, AttributeError):
                raise ValueError("v1 delivery result is incomplete or mismatched; pending batch retained") from None
            count += len(verified)
            self.excluded_ids[destination] = set(excluded)
            self._progress = _DrainProgress(count, self._progress.excluded + len(excluded))
        excluded = sum(len(ids) for ids in self.excluded_ids.values())

        def finish():
            if self._get(self.key, None) != pending:
                raise RuntimeError("original pending source changed; retain batch for reconciliation")
            message = (f"Collected {count} records; kept {excluded} records excluded"
                       if excluded else pending["message"])
            self.ctx.annotation(message, observed_at=pending.get("observed_at"))
            self._set(self.key, {"groups": []})

        self.session.commit(finish)
        return count
