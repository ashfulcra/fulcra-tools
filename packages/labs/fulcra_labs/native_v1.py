"""Native labs Metric codec and standalone durable delivery.

Canonical interpretation belongs to immutable destination metadata. Raw units
remain source provenance; optional per-record unit is always omitted.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from uuid import UUID

from fulcra_common.data_types import DataTypeSpec
from fulcra_common.v1 import V1DataClient, build_metric_record, record_matches

from . import markers
from .store import _traceability_note, archive_document, source_doc_token
from .validate import OK, REVIEW, parse_collected_at

CODEC = "labs.metric.v1"
PENDING = "v1_labs_pending"
WITNESS = "v1_labs_witness"
STATUS = "v1_labs_status"


def marker_spec(key):
    marker = markers.BY_KEY[key]
    fields = {name: {"type": "string"} for name in ("note", "marker_key", "raw_value")}
    fields.update(
        {
            name: {"type": ["string", "null"]}
            for name in (
                "raw_unit",
                "reference_range",
                "flag",
                "lab",
                "source_doc_token",
                "qualifier",
                "report_date",
            )
        }
    )
    return DataTypeSpec(
        key=f"collect.labs.{key}.v1",
        base_type="Metric",
        name=f"{marker.display_name} (Labs)",
        description=f"Laboratory observation for {marker.display_name} [{marker.key}]; "
        f"canonical unit: {marker.canonical_unit}; LOINC: {marker.loinc or 'unspecified'}.",
        fields=fields,
        required_fields=tuple(fields),
        aggregation="discrete",
        unit=marker.canonical_unit,
    )


def normalized_pins(values):
    pins = {}
    for value in values:
        key, sep, target = value.partition("=")
        if not sep or key not in markers.BY_KEY or key in pins:
            raise ValueError("marker targets require unique canonical registry keys")
        try:
            base, identity = target.split("/")
            canonical = str(UUID(identity))
        except (ValueError, AttributeError):
            raise ValueError("marker target must be canonical Metric/UUID") from None
        if base != "Metric" or identity != canonical:
            raise ValueError("marker target must be canonical Metric/UUID")
        pins[key] = target
    return dict(sorted(pins.items()))


def authority(pins):
    return dict(codec=CODEC, spec_version=1, marker_targets=dict(pins))


def legacy_evidence():
    """The retained cache is read-only migration evidence, never v1 authority."""
    from .store import load_state

    return load_state().to_dict()


def validated_status(value):
    if value is None:
        return None
    if not isinstance(value, dict):
        raise RuntimeError("native labs status UNKNOWN")
    for key in ("verified", "excluded"):
        if key in value and (type(value[key]) is not int or value[key] < 0):
            raise RuntimeError("native labs status counts UNKNOWN")
    for key in ("last_attempt", "last_verified_ingest", "current_completed_at"):
        if key in value and (
            not isinstance(value[key], str) or parse_collected_at(value[key])[0] is None
        ):
            raise RuntimeError("native labs status time UNKNOWN")
    if any(key in value for key in ("verified", "excluded", "counts_scope")):
        if (
            not {"verified", "excluded", "counts_scope", "current_completed_at"} <= set(value)
            or value["counts_scope"] != "last completed current delivery"
        ):
            raise RuntimeError("native labs status evidence UNKNOWN")
    if "last_recovery" in value:
        recovery = value["last_recovery"]
        if (
            not isinstance(recovery, dict)
            or recovery.get("state") not in ("complete", "UNKNOWN")
            or any(
                type(recovery.get(key)) is not int or recovery[key] < 0
                for key in ("verified", "excluded")
            )
        ):
            raise RuntimeError("native labs recovery status UNKNOWN")
        if (
            not isinstance(recovery.get("observed_at"), str)
            or parse_collected_at(recovery["observed_at"])[0] is None
            or (recovery["state"] == "UNKNOWN" and not isinstance(recovery.get("error_type"), str))
            or (recovery["state"] == "complete" and recovery.get("error_type") is not None)
        ):
            raise RuntimeError("native labs recovery evidence UNKNOWN")
    return value


def build_record(verdict, report, target, token=None):
    spec = marker_spec(verdict.marker_key)
    if verdict.canonical_unit != spec.unit:
        raise ValueError("labs canonical unit differs from immutable marker spec")
    when, _ = parse_collected_at(verdict.collected_at)
    if when is None or not verdict.det_source_id or verdict.canonical_value is None:
        raise ValueError("labs observation is incomplete")
    fields = dict(
        marker_key=verdict.marker_key,
        raw_value=verdict.raw_value,
        raw_unit=verdict.raw_unit,
        reference_range=verdict.reference_range,
        flag=verdict.flag,
        lab=report.lab,
        source_doc_token=token,
        qualifier=verdict.qualifier,
        report_date=report.report_date,
    )
    for key, value in fields.items():
        if not isinstance(value, str) and not (
            value is None and key not in ("marker_key", "raw_value")
        ):
            raise ValueError("labs provenance must match declared string/null schema")
    fields["note"] = _traceability_note(verdict, lab=report.lab, doc_token=token)
    return build_metric_record(
        data_type=target,
        source_id=verdict.det_source_id,
        start_time=when,
        value=verdict.canonical_value,
        fields=fields,
        tags=[],
    )


@dataclass
class Outcome:
    total: int = 0
    ingested: int = 0
    excluded: int = 0
    would_ingest: int = 0
    skipped_duplicate: int = 0
    review_held: int = 0
    rejected: int = 0
    review_items: list = field(default_factory=list)
    marker_attribution: str = "UNKNOWN"
    dry_run: bool = False
    target_hints: dict = field(default_factory=dict)

    def to_dict(self):
        return asdict(self)


def plan(report, confirmed, outcome):
    selected, seen = [], set()
    for v in report.verdicts:
        ingestable = v.verdict == OK or (
            v.verdict == REVIEW and v.det_source_id is not None and v.marker_key in confirmed
        )
        if not ingestable:
            if v.verdict == "reject":
                outcome.rejected += 1
            else:
                outcome.review_held += 1
                outcome.review_items.append(
                    dict(marker_raw=v.marker_raw, marker_key=v.marker_key, reasons=v.reasons)
                )
            continue
        if not (
            v.marker_key and v.canonical_value is not None and v.det_source_id and v.collected_at
        ):
            outcome.review_held += 1
            outcome.review_items.append(dict(marker_raw=v.marker_raw, reasons=["incomplete row"]))
            continue
        if v.det_source_id in seen:
            outcome.skipped_duplicate += 1
            continue
        seen.add(v.det_source_id)
        # Validate every selected row before resolving targets or archiving.
        build_record(v, report, "Metric/00000000-0000-4000-8000-000000000000")
        selected.append(v)
    return selected


def preview(report, confirmed, source_doc=None, pins=None):
    outcome = Outcome(total=len(report.verdicts), dry_run=True)
    selected = plan(report, confirmed, outcome)
    token = None
    if source_doc is not None:
        sha, basename = source_doc_token(source_doc)
        token = f"{sha}:{basename}"
    outcome.would_ingest = len(selected)
    outcome.target_hints = {
        v.marker_key: (pins or {}).get(v.marker_key, "requires live resolution") for v in selected
    }
    return outcome, token


class LabsWriter:
    def __init__(self, ctx, session, pins):
        from fulcra_collect.v1_outbox import V1RecordOutbox

        self.ctx, self.session, self.pins = ctx, session, pins
        self.outbox = V1RecordOutbox(ctx, session, key=PENDING)
        self.outcome = Outcome()
        self.recovery = dict(verified=0, excluded=0)

    def _witnesses(self):
        value = self.ctx.kv_get(WITNESS, {})
        if not isinstance(value, dict):
            raise RuntimeError("labs destination witness UNKNOWN")
        return value

    def _witness(self, target, key):
        return dict(
            account=self.session.account_id,
            marker=key,
            fingerprint=marker_spec(key).fingerprint,
            codec=CODEC,
            target=target,
        )

    def resume(self):
        self.recovery = dict(verified=0, excluded=0, state="UNKNOWN")
        destinations = self.outbox.pending_destinations()
        if not destinations:
            self.recovery["state"] = "none"
            return
        try:
            witnesses = self._witnesses()
            # All target proofs precede drain; full payload validation remains
            # per-group in the kernel, preserving honest partial progress.
            for target in destinations:
                witness = witnesses.get(self.session.account_id + ":" + target)
                if (
                    not isinstance(witness, dict)
                    or witness.get("marker") not in markers.BY_KEY
                    or witness != self._witness(target, witness["marker"])
                ):
                    raise RuntimeError(
                        "labs pending destination witness UNKNOWN; retain original batch"
                    )
                self.session.resolve_destination(
                    marker_spec(witness["marker"]), pinned_id=target, allow_create=False
                )
            self.outbox.drain()
        except Exception as exc:
            self._recovery_progress("UNKNOWN")
            self.recovery["error_type"] = type(exc).__name__
            self._status(recovery=self.recovery)
            raise
        else:
            self._recovery_progress("complete")
            self._status(recovery=self.recovery)

    def _recovery_progress(self, state):
        p = self.outbox.progress
        self.recovery = dict(
            verified=p.verified if p else 0, excluded=p.excluded if p else 0, state=state
        )

    def run(self, report, confirmed, source_doc=None):
        self.outcome.total = len(report.verdicts)
        selected = plan(report, confirmed, self.outcome)
        legacy = legacy_evidence()
        for v in selected:
            if v.marker_key in legacy["markers"] and v.marker_key not in self.pins:
                raise ValueError("selected legacy marker requires explicit --marker-target")
        targets = {}
        for v in selected:
            key = v.marker_key
            if key not in targets:
                targets[key] = self.session.resolve_destination(
                    marker_spec(key), pinned_id=self.pins.get(key), allow_create=True
                ).data_type
        token = archive_document(source_doc) if source_doc is not None else None
        groups = {}
        for v in selected:
            target = targets[v.marker_key]
            groups.setdefault(target, []).append(build_record(v, report, target, token))
        with self.session.record_write_scope():
            # Complete served-schema preflight across all groups before staging/POST.
            api = V1DataClient(self.session.client)
            for target, records in groups.items():
                api.validate_records(target, records)
            for target, records in groups.items():
                existing = self.session.get_records(target, [row["id"] for row in records])
                if any(
                    row["id"] in existing and not record_matches(existing[row["id"]], row)
                    for row in records
                ):
                    raise RuntimeError(
                        "labs current observation identity conflict; no current batch staged"
                    )
            # This local mutation lease covers current proof through staging/drain.
            # External hosts can still edit after preflight; no cross-host CAS is claimed.
            witnesses = self._witnesses()
            for key, target in targets.items():
                index = self.session.account_id + ":" + target
                expected = self._witness(target, key)
                if index in witnesses and witnesses[index] != expected:
                    raise RuntimeError("labs destination witness conflict; retain original batch")
                witnesses[index] = expected

            def remember():
                self.ctx.kv_set(WITNESS, witnesses)
                if self.ctx.kv_get(WITNESS, None) != witnesses:
                    raise RuntimeError("labs destination witness checkpoint UNKNOWN")

            self.session.commit(remember)
            self._status(attempt=True)
            self.outbox.stage(groups, message="Imported laboratory observations")
            try:
                self.outbox.drain()
            finally:
                p = self.outbox.progress
                if p is not None:
                    self.outcome.ingested, self.outcome.excluded = p.verified, p.excluded
            if groups:
                self._status(verified=True)
        return self.outcome

    def _status(self, *, attempt=False, verified=False, recovery=None):
        key = STATUS + ":" + self.session.account_id
        prior = validated_status(self.ctx.kv_get(key, {}))
        updated = dict(prior)
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        if attempt:
            updated["last_attempt"] = now
        if verified:
            updated.update(
                verified=self.outcome.ingested,
                excluded=self.outcome.excluded,
                counts_scope="last completed current delivery",
                current_completed_at=now,
            )
            if self.outcome.ingested:
                updated["last_verified_ingest"] = now
        if recovery is not None:
            updated["last_recovery"] = dict(
                recovery, observed_at=now, error_type=recovery.get("error_type")
            )
            if recovery["verified"]:
                updated["last_verified_ingest"] = now

        def persist():
            # standalone_runtime and these KV callbacks use db.open's same
            # profile/thread cached connection. Never commit unrelated work.
            from fulcra_collect import db

            connection = db.open(self.session.profile / "state.db")
            if connection.in_transaction:
                raise RuntimeError(
                    "native labs status transaction UNKNOWN; existing transaction retained"
                )
            committing = False
            try:
                connection.execute("BEGIN IMMEDIATE")
                self.ctx.kv_set(key, updated)
                if self.ctx.kv_get(key, None) != updated:
                    raise RuntimeError("native labs status checkpoint UNKNOWN")
                self.session.guard(mutating=True)
                committing = True
                connection.execute("COMMIT")
            except Exception:
                # COMMIT may have succeeded without a usable acknowledgement.
                # Roll back only a transaction SQLite still reports active;
                # never claim rollback when the COMMIT outcome is uncertain.
                try:
                    if connection.in_transaction:
                        connection.execute("ROLLBACK")
                except Exception:
                    raise RuntimeError("native labs status rollback UNKNOWN") from None
                detail = "COMMIT outcome" if committing else "transaction"
                raise RuntimeError(f"native labs status {detail} UNKNOWN") from None

        try:
            self.session.commit(persist)
        except Exception as exc:
            if isinstance(exc, RuntimeError) and str(exc).startswith("native labs status"):
                raise
            raise RuntimeError("native labs status authority/commit UNKNOWN") from None


@contextmanager
def open_labs(pins, *, client, on_writer):
    # Optional runtime remains lazy: local registry/check/preview need no Collect.
    from fulcra_collect.standalone_v1 import standalone_runtime

    transport = client._transport
    with standalone_runtime(
        "cli-labs",
        authority(pins),
        client=client,
        transport=transport,
        identity_transport=transport,
    ) as (ctx, session):
        writer = LabsWriter(ctx, session, pins)
        on_writer(writer)
        writer.resume()
        yield writer
