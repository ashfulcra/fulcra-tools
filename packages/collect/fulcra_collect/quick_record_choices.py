"""Prepared owned v1 destinations for existing moment/duration track choices.

Legacy annotation definitions are read-only migration inputs. Track identity,
name, kind and tags are preserved explicitly; no legacy record is emitted. The
first type specification is frozen before creation so renaming a track cannot
fork a pending creation or change an existing destination binding.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json

from fulcra_common.data_types import DataTypeSpec
from fulcra_common.v1 import _canonical_uuid, _destination, build_event_record

from .record_actions import _hash, _json


def _time(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("quick-record time must include a timezone")
    return value.astimezone(timezone.utc)


def _template(value):
    if (not isinstance(value, dict)
            or set(value) != {"version", "track_id", "kind", "initial_name", "legacy_definition_id"}
            or type(value["version"]) is not int or value["version"] != 1
            or value["kind"] not in {"moment", "duration"}
            or not isinstance(value["initial_name"], str) or not value["initial_name"].strip()
            or len(value["initial_name"]) > 256):
        raise ValueError("quick-record track binding is unreadable")
    if _canonical_uuid(value["track_id"]) != _canonical_uuid(value["legacy_definition_id"]):
        raise ValueError("quick-record track provenance changed")
    return _json(value, limit=16384)


def track_spec(template):
    _template(template)
    identity, kind = template["track_id"], template["kind"]
    return DataTypeSpec(key="collect.quick-record.track." + identity + ".v1", base_type="Event",
        name="Collect: " + template["initial_name"][:70] + " [" + identity + "]",
        description="Quick-record " + kind + " events for one preserved track.",
        fields={"track_id": {"type": "string", "format": "uuid", "const": identity},
                "track_name": {"type": "string"}, "kind": {"type": "string", "const": kind},
                "legacy_definition_id": {"type": "string", "format": "uuid", "const": identity},
                "note": {"type": "string"}},
        required_fields=("track_id", "track_name", "kind", "legacy_definition_id"))


def build_quick_record(choice, *, action_id, started_at, ended_at=None, comment=None):
    """Pure first-payload builder, preserving the established primary prefix."""
    identity = _canonical_uuid(action_id)
    track = _canonical_uuid(choice["id"])
    destination = _destination(choice["data_type"], "Event")
    start = _time(started_at)
    kind = choice["kind"]
    if kind == "duration":
        end = _time(ended_at)
        if end <= start:
            raise ValueError("quick-record duration end must be after its start")
    elif kind == "moment" and ended_at is None:
        end = start
    else:
        raise ValueError("quick-record times do not match the selected track kind")
    if comment is not None and not isinstance(comment, str):
        raise ValueError("quick-record note must be text")
    fields = {"track_id": track, "track_name": choice["name"], "kind": kind,
              "legacy_definition_id": track}
    if comment:
        fields["note"] = comment
    return {"data_type": destination, "record": build_event_record(data_type=destination,
        source_id="com.fulcradynamics.fulcra-collect.quick-record." + identity,
        start_time=start, end_time=end, tags=choice["tags"], fields=fields)}


class QuickRecordChoices:
    def __init__(self, session, journal):
        self.session, self.journal = session, journal
        with journal._db(write=True) as db:
            db.execute("CREATE TABLE IF NOT EXISTS quick_record_choice_bindings ("
                "account_id TEXT NOT NULL, track_id TEXT NOT NULL, template_json TEXT NOT NULL, "
                "template_hash TEXT NOT NULL, destination TEXT, PRIMARY KEY(account_id,track_id))")

    def list(self):
        self.session.guard()
        response = self.session.client._client().get("/user/v1alpha1/annotation",
                                                    headers=self.session.client._authed_headers())
        response.raise_for_status()
        rows = response.json()
        if not isinstance(rows, list) or len(rows) > 2048:
            raise ValueError("quick-record track catalog is unreadable or exceeds its limit")
        choices, seen = [], set()
        try:
            for row in rows:
                if not isinstance(row, dict):
                    raise ValueError("quick-record track entry is unreadable")
                identity = _canonical_uuid(row.get("id"))
                if identity in seen:
                    raise ValueError("duplicate quick-record track identity")
                seen.add(identity)
                if row.get("fulcra_userid", self.session.account_id) != self.session.account_id:
                    raise ValueError("quick-record track belongs to another account")
                deleted = row.get("deleted_at")
                if deleted is not None:
                    if not isinstance(deleted, str):
                        raise ValueError("quick-record track deletion state is unreadable")
                    _time(datetime.fromisoformat(deleted.replace("Z", "+00:00")))
                    continue
                kind = row.get("annotation_type")
                if kind not in {"moment", "duration"}:
                    continue
                name = row.get("name")
                if not isinstance(name, str) or not name.strip() or len(name) > 256:
                    raise ValueError("quick-record track name is unreadable")
                tags = row.get("tags")
                if tags is None:
                    tags = []
                if not isinstance(tags, list) or len(tags) > 256:
                    raise ValueError("quick-record track tags are unreadable")
                tags = list(dict.fromkeys(_canonical_uuid(tag) for tag in tags))
                choice = {"id": identity, "name": name, "kind": kind, "tags": tags}
                digest = _hash(_json(choice, limit=32768))
                choices.append(choice | {"digest": digest, "annotation_type": kind})
        except (TypeError, AttributeError):
            raise ValueError("quick-record track catalog is malformed") from None
        self.session.guard()
        return sorted(choices, key=lambda choice: (choice["kind"] != "moment", choice["name"].casefold(), choice["id"]))

    def _binding(self, db, identity):
        row = db.execute("SELECT template_json,template_hash,destination FROM quick_record_choice_bindings "
                         "WHERE account_id=? AND track_id=?", (self.session.account_id, identity)).fetchone()
        if row is None:
            return None
        try:
            template = json.loads(row["template_json"])
            if (_template(template) != row["template_json"] or template["track_id"] != identity
                    or _hash(row["template_json"]) != row["template_hash"]):
                raise ValueError
            if row["destination"] is not None:
                _destination(row["destination"], "Event")
        except (ValueError, TypeError, KeyError):
            raise ValueError("quick-record track binding is corrupt or unreadable") from None
        return template, row["destination"]

    def prepare(self, identity, *, expected_digest):
        _canonical_uuid(identity)
        with self.session.record_write_scope():
            selected = next((choice for choice in self.list() if choice["id"] == identity), None)
            if selected is None or selected["digest"] != expected_digest:
                raise ValueError("quick-record track changed; refresh before recording")
            with self.journal._db(write=True) as db:
                prior = self._binding(db, identity)
                if prior is None:
                    template = {"version": 1, "track_id": identity, "kind": selected["kind"],
                        "initial_name": selected["name"], "legacy_definition_id": identity}
                    text = _template(template)
                    db.execute("INSERT INTO quick_record_choice_bindings VALUES (?,?,?,?,NULL)",
                               (self.session.account_id, identity, text, _hash(text)))
                    pin = None
                else:
                    template, pin = prior
                    if template["kind"] != selected["kind"]:
                        raise ValueError("quick-record track kind changed; reconcile the existing mapping")
            resolved = self.session.resolve_destination(track_spec(template), pinned_id=pin, allow_create=pin is None)

            def bind():
                with self.journal._db(write=True) as db:
                    current = self._binding(db, identity)
                    if current != (template, pin):
                        raise ValueError("quick-record destination changed; refresh before recording")
                    db.execute("UPDATE quick_record_choice_bindings SET destination=? WHERE account_id=? AND track_id=?",
                               (resolved.data_type, self.session.account_id, identity))

            self.session.commit(bind)
            return selected | {"data_type": resolved.data_type}
