"""Bind v1 destination/record operations to a captured Collect run.

All mutations hold the same shared account/config leases used by selected-file
sources. A request hook rechecks fresh credentials immediately before network
I/O, including the record POST after its preceding schema GET.
"""
from __future__ import annotations

import hmac
import sqlite3
from copy import deepcopy
from contextlib import nullcontext, contextmanager
from urllib.parse import quote
from uuid import UUID

import httpx
from fulcra_api.core import FulcraAPI
from fulcra_api.credentials import FulcraCredentials
from fulcra_common.client import BaseFulcraClient
from fulcra_common.data_types import CreationJournal, DataTypeSpec, resolve_data_type, tag_create_name
from fulcra_common.v1 import V1DataClient
from fulcra_common.record_exclusions import EXCLUSION_SPEC, RecordExclusions, resolve_exclusion_type, verify_exclusion_type
from fulcra_common.record_delivery import RecordDelivery, SourceDispatch, reconcile_records, enforce_exclusions

from . import config
from .config_leases import task_mutation_scope, record_mutation_scope
from .plugin import RunContext


class _CollectClient(BaseFulcraClient):
    def __init__(self, session, transport=None):
        super().__init__(transport=transport)
        self.session = session

    def get_token(self) -> str:
        self.session.guard()
        return self.session._token

    def _client(self):
        first = self._http is None
        http = super()._client()
        if first:
            http.event_hooks["request"].append(self._guard_request)
        return http

    def _guard_request(self, request):
        expected = httpx.URL(self.base_url)
        if (request.url.scheme, request.url.host, request.url.port) != (expected.scheme, expected.host, expected.port):
            raise RuntimeError("Unexpected Fulcra redirect origin; request cancelled")
        self.session.guard(mutating=request.method not in {"GET", "HEAD"},
                           require_enabled=True if request.url.path.startswith("/ingest/") else None)
        request.headers["Authorization"] = f"Bearer {self.session._token}"


class _CollectPolicyStore:
    """A policy lookup rechecks its binding around the actual record transport."""

    def __init__(self, session, data_type: str):
        self.session = session
        self.data_type = data_type

    @property
    def account_id(self):
        return self.session.account_id

    def _transport(self, data_type, operation):
        if data_type != self.data_type:
            raise ValueError("policy transport destination changed")
        before = self.session._verify_record_policy(self.data_type)
        result = operation()
        after = self.session._verify_record_policy(self.data_type)
        if before.schema_hash != after.schema_hash:
            raise ValueError("exclusion control schema changed during transport")
        return result

    def get_records(self, data_type, record_ids):
        return self._transport(data_type, lambda: self.session.get_records(data_type, record_ids))

    def ingest(self, data_type, records):
        return self._transport(data_type, lambda: self.session.ingest(data_type, records))


class CollectRecordSession:
    """Strict Collect auth and captured consent for a plugin's v1 operations.

    A missing Collect sign-in never adopts environment or CLI credentials.
    Silent renewal must prove the same established account before replacing it.
    Read-only setup can set ``require_enabled=False``; record ingestion always
    requires enablement. This object owns and closes its HTTP connection.
    """

    def __init__(self, ctx: RunContext, *, transport=None, require_enabled: bool = True):
        self.ctx = ctx
        self.require_enabled = require_enabled
        self.epoch = ctx.config_epoch
        self._source_profile = getattr(ctx, "_trakt_source_profile", None) or str(config.profile_path())
        persisted = getattr(ctx, "_persisted_config", None)
        self.settings = deepcopy(ctx.config if persisted is None else persisted)
        self.settings_fingerprint = config.settings_fingerprint(self.settings)
        self.preview_settings = deepcopy(ctx.config)
        self._token = ctx.collect_token()
        if not self._token:
            raise ValueError("Reconnect to Fulcra in Collect before writing records")
        try:
            self.account_id = str(UUID(FulcraAPI(credentials=FulcraCredentials(access_token=self._token)).get_fulcra_userid()))
        except Exception:
            raise ValueError("Cannot establish Collect's authenticated account") from None
        self.client = _CollectClient(self, transport=transport)
        self._exclusion_type = None
        self.guard()

    def _source_local_guard(self):
        """Fresh local authority after slow Trakt secret sampling; no auth I/O."""
        current = config.load()
        if (str(config.profile_path()) != self._source_profile or current.account_transition
                or (self.require_enabled and self.ctx.plugin_id not in current.enabled)
                or current.plugin_epochs.get(self.ctx.plugin_id, "") != self.epoch
                or not config.settings_match(current.plugin_settings.get(self.ctx.plugin_id, {}), self.settings)):
            raise RuntimeError("Collect configuration changed; retry the selected source")

    def guard(self, *, mutating: bool = False, require_enabled: bool | None = None) -> None:
        current = config.load()
        require_enabled = self.require_enabled if require_enabled is None else require_enabled
        if (current.account_transition
                or (require_enabled and self.ctx.plugin_id not in current.enabled)
                or current.plugin_epochs.get(self.ctx.plugin_id, "") != self.epoch
                or not config.settings_match(current.plugin_settings.get(self.ctx.plugin_id, {}), self.settings)):
            raise RuntimeError("Collect configuration changed; retry the selected source")
        fresh = self.ctx.collect_token()
        if not fresh:
            raise RuntimeError("Collect sign-in changed; retry the selected source")
        if not hmac.compare_digest(self._token, fresh):
            try:
                account = str(UUID(FulcraAPI(credentials=FulcraCredentials(access_token=fresh)).get_fulcra_userid()))
            except Exception:
                raise RuntimeError("Collect sign-in changed; retry the selected source") from None
            if account != self.account_id:
                raise RuntimeError("Collect sign-in changed; retry the selected source")
            # Silent same-account refresh does not revoke source consent. The
            # HTTP hook replaces old captured headers before sending a request.
            # Interactive sign-in/settings changes still rotate the epoch.
            self._token = fresh
        if mutating:
            for key in ("dry_run", "preview_only"):
                value = self.preview_settings.get(key, False)
                if type(value) is not bool:
                    raise ValueError("Preview only must be on or off")
                if value:
                    raise RuntimeError("Preview only is enabled; no records were uploaded")

    def resolve_destination(self, spec: DataTypeSpec, *, pinned_id: str | None = None,
                            allow_create: bool = False):
        with task_mutation_scope() if allow_create else nullcontext():
            self.guard(mutating=allow_create)
            return resolve_data_type(
                client=self.client, spec=spec, account_id=self.account_id,
                pinned_id=pinned_id, allow_create=allow_create,
                journal=CreationJournal(config.config_dir() / "v1-type-creations.sqlite") if allow_create else None,
            )

    def resolve_tag(self, name: str, *, allow_create: bool = True) -> str:
        """Reuse an owned saved tag through renames; create bounded new names."""
        create_name = tag_create_name(name)
        with task_mutation_scope() if allow_create else nullcontext():
            self.guard(mutating=allow_create)
            journal_path = config.config_dir() / "v1-type-creations.sqlite"
            journal = CreationJournal(journal_path) if allow_create or journal_path.exists() else None
            prior = journal.read_tag(self.account_id, name) if journal else None
            http, headers = self.client._client(), self.client._authed_headers()
            def verified(body, *, expected_name=None, expected_id=None):
                try:
                    identity = str(UUID(body["id"]))
                    if (not isinstance(body.get("name"), str) or not body["name"]
                            or (expected_name is not None and body["name"] != expected_name)
                            or (expected_id is not None and identity != expected_id)
                            or body.get("deleted_at")
                            or body.get("fulcra_userid", self.account_id) != self.account_id):
                        raise ValueError
                except (ValueError, TypeError, AttributeError, KeyError):
                    raise ValueError("tag lookup is malformed or belongs to another account") from None
                return identity

            if prior and prior[0] == "resolved":
                response = http.get("/user/v1alpha1/tag/id/" + prior[1], headers=headers)
                response.raise_for_status()
                identity = verified(response.json(), expected_id=prior[1])
                self.guard(mutating=allow_create)
                return identity
            if create_name != name:
                # List lookup also preserves old long/slashed names that the
                # current creation API cannot safely create or name-address.
                response = http.get("/user/v1alpha1/tag", headers=headers)
                response.raise_for_status()
                rows = response.json()
                if (not isinstance(rows, list) or any(not isinstance(row, dict)
                        or not isinstance(row.get("name"), str) or not isinstance(row.get("id"), str) for row in rows)):
                    raise ValueError("tag catalog is unreadable; no creation permitted")
                matches = [row for row in rows if row["name"] == name]
                if len(matches) > 1:
                    raise ValueError("tag lookup is ambiguous; reconcile the original binding")
                if matches:
                    identity = verified(matches[0], expected_name=name)
                else:
                    if prior:
                        raise RuntimeError("prior tag creation is unresolved; reconcile before retry")
                    identity = self.resolve_tag(create_name, allow_create=allow_create)
                self.guard(mutating=allow_create)
                if journal is not None and allow_create:
                    journal.resolved_tag(self.account_id, name, identity)
                return identity
            path = "/user/v1alpha1/tag/name/" + quote(name, safe="")
            response = http.get(path, headers=headers)
            if response.status_code == 404:
                if not allow_create:
                    raise ValueError("no existing tag configured for read-only verification")
                assert journal is not None
                if not journal.begin_tag(self.account_id, name):
                    raise RuntimeError("prior tag creation is unresolved; reconcile before retry")
                response = http.post("/user/v1alpha1/tag", json={"name": name}, headers=headers)
                response.raise_for_status()
                # A creation response alone does not replace owned lookup.
                response = http.get(path, headers=headers)
            response.raise_for_status()
            identity = verified(response.json(), expected_name=name)
            self.guard(mutating=allow_create)
            if journal is not None and allow_create:
                journal.resolved_tag(self.account_id, name, identity)
            return identity

    def remember_existing_tag(self, name: str, expected_id: str) -> str:
        """Remember a planned owned lookup without ever authorizing tag POST.

        Call only after complete source/schema preflight. A vanished/replaced
        name, conflicting receipt or revoked authority holds the original work.
        The existing read-only resolver retains its no-local-write semantics.
        """
        tag_create_name(name)
        try:
            if not isinstance(expected_id, str) or str(UUID(expected_id)) != expected_id:
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise ValueError("planned tag ID must be a canonical UUID") from None
        with task_mutation_scope():
            self.guard(mutating=True, require_enabled=True)
            actual = self.resolve_tag(name, allow_create=False)
            if actual != expected_id:
                raise ValueError("tag identity changed after planning; retain original work")
            self.guard(mutating=True, require_enabled=True)
            journal = CreationJournal(config.config_dir() / "v1-type-creations.sqlite")
            journal.resolved_tag(self.account_id, name, expected_id)
            return expected_id

    def ingest(self, data_type: str, records: list[dict], *, before_submit=None) -> dict | None:
        """Low-level guarded transport; sources use deliver_records/outboxes.

        RecordExclusions also uses this seam for its own policy rows. Replay
        enforcement belongs to deliver_records, avoiding recursive policy writes.
        """
        with task_mutation_scope():
            with record_mutation_scope(self.account_id):
                self.guard(mutating=True, require_enabled=True)
                return V1DataClient(self.client).ingest(data_type, records, before_submit=before_submit)

    def _exclusions(self, *, allow_create: bool) -> RecordExclusions:
        if self._exclusion_type is None and allow_create:
            path = config.config_dir() / "v1-type-creations.sqlite"
            journal = CreationJournal(path)
            # Preserve the account's control identity through a rename. A
            # missing/deprecated/replaced bound control fails, never re-creates.
            resolved = resolve_exclusion_type(client=self.client,
                account_id=self.account_id, allow_create=allow_create, journal=journal)
            self._exclusion_type = resolved.data_type
        else:
            resolved = self._verify_record_policy(self._exclusion_type)
            self._exclusion_type = resolved.data_type
        return RecordExclusions(_CollectPolicyStore(self, self._exclusion_type), self._exclusion_type)

    def _verify_record_policy(self, data_type):
        self.guard()
        try:
            journal = CreationJournal(config.config_dir() / "v1-type-creations.sqlite", read_only=True)
            return verify_exclusion_type(client=self.client, account_id=self.account_id,
                journal=journal, data_type=data_type)
        except (OSError, sqlite3.DatabaseError):
            raise ValueError("the original exclusion binding is unreadable; retain pending work") from None

    def record_policy_binding(self) -> str | None:
        """Read the saved control identity for UI filtering, without discovery.

        This is advisory identity only; mutations still prove owner and schema.
        A fresh account read never creates a journal just to render its picker.
        """
        self.guard()
        path = config.config_dir() / "v1-type-creations.sqlite"
        if not path.exists():
            return None
        prior = CreationJournal(path, read_only=True).read(self.account_id, EXCLUSION_SPEC)
        return prior[2] if prior and prior[1] == "resolved" else None

    def deliver_records(self, data_type: str, records: list[dict], *, dispatch) -> RecordDelivery:
        """Prove source visibility or permanent exclusion under one local lease."""
        if not isinstance(dispatch, SourceDispatch):
            raise ValueError("source delivery requires a durable dispatch guard")
        with self.record_write_scope():
            if not records:
                return RecordDelivery((), ())
            checked = V1DataClient(self.client).validate_records(data_type, records)
            return reconcile_records(self, self._exclusions(allow_create=True), data_type, checked, dispatch=dispatch)

    @contextmanager
    def record_write_scope(self):
        """Include source dedup planning and upload in the same local lease."""
        with task_mutation_scope():
            with record_mutation_scope(self.account_id):
                self.guard(mutating=True, require_enabled=True)
                yield

    def prepare_record_policy(self, *, check_only: bool = False) -> str:
        """Prove the owned control destination at setup/service readiness."""
        with task_mutation_scope():
            with record_mutation_scope(self.account_id):
                self.guard(mutating=not check_only)
                return self._exclusions(allow_create=not check_only).data_type

    def excluded_ids(self, data_type: str, record_ids: list[str], *, check_only: bool = False) -> set[str]:
        """Source/twin preflight; preview reads never repair or create policy."""
        with task_mutation_scope():
            with record_mutation_scope(self.account_id):
                self.guard(mutating=not check_only, require_enabled=True if not check_only else None)
                policy = self._exclusions(allow_create=not check_only)
                if check_only:
                    return policy.excluded_ids(data_type, record_ids)
                return enforce_exclusions(self, policy, data_type, record_ids)

    def delete_records(self, data_type: str, record_ids: list[str], *, action_id: str):
        """Guarded native transport; deletion actors also own policy/action state."""
        with task_mutation_scope():
            with record_mutation_scope(self.account_id):
                self.guard(mutating=True, require_enabled=True)
                return V1DataClient(self.client).delete_records(data_type, record_ids, action_id=action_id)

    def get_record(self, data_type: str, record_id: str) -> dict | None:
        self.guard()
        return V1DataClient(self.client).get_record(data_type, record_id, account_id=self.account_id)

    def get_latest(self, data_type: str) -> dict | None:
        self.guard()
        return V1DataClient(self.client).get_latest(data_type, account_id=self.account_id)

    def get_records(self, data_type: str, record_ids: list[str]) -> dict[str, dict]:
        self.guard()
        return V1DataClient(self.client).get_records(data_type, record_ids, account_id=self.account_id)

    def get_source_records(self, data_type: str, sources: list[str], *, start, end) -> list[dict]:
        self.guard()
        return V1DataClient(self.client).get_source_records(data_type, sources, start=start, end=end,
                                                            account_id=self.account_id)

    def commit(self, callback):
        """Fence local receipts/cursors against an account or consent change."""
        with task_mutation_scope():
            self.guard(mutating=True, require_enabled=True)
            return callback()

    def close(self) -> None:
        if self.client._http is not None:
            self.client._http.close()

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()
