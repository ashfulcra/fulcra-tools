"""Scoped private progress for the five configured standalone poll CLI routes.

Last.fm, Deezer, generic RSS, Letterboxd and Goodreads use these decisions after
strict scans and actual configured delivery. A successful ImportResult is not a
provenance capability: call decide_progress only beside the current
run.import_events(selected) return, after fully materializing the strict scan.
Recovered pending results never qualify. Collect daemon callers use their own
daemon_poll adapter and version-2 cursor scope, separate from this CLI policy.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
from uuid import UUID

from fulcra_collect import config
from fulcra_common.client import ImportResult
from .importers.poll_scan import ScanReport, fail as scan_fail

CURSOR_ERROR = "cursor state unavailable"
SELECTION_ERROR = "poll selection unavailable"
MAX_JSON_BYTES = 4096
_SCOPE_KEYS = {
    "authority_fingerprint",
    "profile_digest",
    "account_id",
    "destination_id",
    "importer",
    "category",
    "service_digest",
    "source_binding_digest",
}
_CATEGORIES = {
    "lastfm": ("listened",),
    "deezer": ("listened",),
    "letterboxd": ("watched",),
    "goodreads": ("read",),
    "generic-rss": ("watched", "listened", "read"),
}


def _error():
    raise RuntimeError(CURSOR_ERROR) from None


def _canonical(value):
    try:
        encoded = json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode("utf-8")
        if len(encoded) > MAX_JSON_BYTES:
            _error()
        return encoded
    except Exception:
        _error()


def _digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _uuid(value):
    if not isinstance(value, str) or str(UUID(value)) != value:
        _error()


def _utc(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        _error()
    return value.astimezone(timezone.utc)


def _scope(value):
    if not isinstance(value, dict) or set(value) != _SCOPE_KEYS:
        _error()
    for key in (
        "authority_fingerprint",
        "profile_digest",
        "service_digest",
        "source_binding_digest",
    ):
        if not isinstance(value[key], str) or not re.fullmatch("[0-9a-f]{64}", value[key]):
            _error()
    _uuid(value["account_id"])
    from fulcra_common.v1 import _destination

    _destination(value["destination_id"], "Event")
    if (
        value["importer"] not in _CATEGORIES
        or value["category"] not in _CATEGORIES[value["importer"]]
    ):
        _error()
    _canonical(value)


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _finite_float(text):
    value = float(text)
    if not math.isfinite(value):
        raise ValueError
    return value


def _read_credentials(path):
    """Bounded local regular-file JSON read shared by capture and revalidation."""
    path = Path(path)
    if not stat.S_ISREG(path.lstat().st_mode):
        raise ValueError
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    with os.fdopen(fd, "rb", buffering=0) as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError
        raw = stream.read(65537)
    if len(raw) > 65536:
        raise ValueError
    value = json.loads(
        raw, object_pairs_hook=_pairs, parse_float=_finite_float,
        parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
    )
    if not isinstance(value, dict):
        raise ValueError
    return value


def _consumed(value, fields):
    captured = {key: value[key] for key in fields}
    for value in captured.values():
        if not isinstance(value, str) or not value:
            raise ValueError
        value.encode("utf-8")
    return captured


def read_source_credentials(importer, *, credential_path=None):
    """Acquire only the two poll routes' consumed strings; no provider activity."""
    try:
        if importer == "lastfm":
            from .importers.lastfm import CREDS_PATH
            fields = ("username", "api_key")
        elif importer == "deezer":
            from .importers.deezer import CREDS_PATH
            fields = ("access_token",)
        else:
            raise ValueError
        return _consumed(_read_credentials(credential_path or CREDS_PATH), fields)
    except Exception:
        raise ValueError("source credentials unavailable") from None


def credential_check(path, consumed):
    """Immediate sampled consent via shared bounded reader, not atomic consent."""
    try:
        captured = _consumed(consumed, tuple(consumed))
        if not captured:
            raise ValueError
    except Exception:
        raise ValueError("source credentials unavailable") from None

    def unchanged():
        try:
            return _consumed(_read_credentials(path), tuple(captured)) == captured
        except Exception:
            return False

    return unchanged


@dataclass(frozen=True, repr=False)
class SourceBinding:
    importer: str
    digest: str
    unchanged: object = field(repr=False)


def source_binding(
    importer,
    *,
    credentials=None,
    credential_path=None,
    username=None,
    user_id=None,
    feed_url=None,
    service=None,
    category=None,
):
    """Route-specific exact identity construction. No raw identity is persisted."""
    try:
        if importer == "lastfm":
            from .importers.lastfm import CREDS_PATH

            captured = {key: credentials[key] for key in ("username", "api_key")}
            check = credential_check(credential_path or CREDS_PATH, captured)
            identity = ["lastfm", captured["username"]]
        elif importer == "deezer":
            from .importers.deezer import CREDS_PATH

            captured = {"access_token": credentials["access_token"]}
            check = credential_check(credential_path or CREDS_PATH, captured)
            identity = ["deezer", _digest(captured["access_token"])]
        elif importer == "letterboxd":
            from .importers.letterboxd import feed_url_for

            if not isinstance(username, str) or not re.fullmatch("[A-Za-z0-9_-]+", username):
                raise ValueError
            identity = ["letterboxd", username, feed_url_for(username)]

            def check():
                return True
        elif importer == "goodreads":
            from .importers.goodreads import feed_url_for

            if not isinstance(user_id, str) or not re.fullmatch("[0-9]+", user_id):
                raise ValueError
            identity = ["goodreads", user_id, feed_url_for(user_id)]

            def check():
                return True
        elif importer == "generic-rss":
            if not all(isinstance(v, str) and v for v in (feed_url, service, category)):
                raise ValueError
            if category not in _CATEGORIES[importer]:
                raise ValueError
            identity = ["generic-rss", feed_url, service, category]

            def check():
                return True
        else:
            raise ValueError
        # Binding input can include long URLs; only its digest enters the scope.
        digest = hashlib.sha256(
            json.dumps(
                identity, ensure_ascii=False, separators=(",", ":"), allow_nan=False
            ).encode()
        ).hexdigest()
        return SourceBinding(importer, digest, check)
    except Exception:
        raise ValueError(SELECTION_ERROR) from None


@dataclass(frozen=True, repr=False)
class PollScope:
    _encoded: bytes
    _binding: SourceBinding = field(repr=False)

    @property
    def key(self):
        return "v1_cli_media_poll_cursor:" + hashlib.sha256(self._encoded).hexdigest()

    def _value(self):
        value = json.loads(self._encoded)
        _scope(value)
        if _canonical(value) != self._encoded:
            _error()
        return value


def capture_scope(run, importer, category, service, binding):
    try:
        from .media_config import CATEGORIES, FILENAME, read_config

        if (
            importer not in _CATEGORIES
            or category not in _CATEGORIES[importer]
            or not isinstance(service, str)
            or not isinstance(binding, SourceBinding)
            or binding.importer != importer
        ):
            _error()
        session = run.session
        session._local_guard()
        profile = config.profile_path()
        if (
            not profile.is_absolute()
            or session.profile != profile.resolve()
            or session.profile != session.profile.resolve()
        ):
            _error()
        settings = session.settings
        keys = {"version", "account_id", "v1_destination_ids", "legacy_selection"}
        expected = keys | ({"preview_only"} if run._check_only else set())
        if set(settings) != expected or (run._check_only and settings["preview_only"] is not True):
            _error()
        authority = {key: settings[key] for key in keys}
        if type(authority["version"]) is not int or authority["version"] != 1:
            _error()
        saved = read_config(profile / FILENAME)
        if saved != authority or authority["account_id"] != session.account_id:
            _error()
        _uuid(session.account_id)
        for field in ("v1_destination_ids", "legacy_selection"):
            if not isinstance(authority[field], dict) or set(authority[field]) != set(CATEGORIES):
                _error()
        destination = authority["v1_destination_ids"][category]
        if run.writer.destination_ids.get(category) != destination:
            _error()
        value = dict(
            authority_fingerprint=config.settings_fingerprint(authority),
            profile_digest=_digest(str(profile.resolve())),
            account_id=session.account_id,
            destination_id=destination,
            importer=importer,
            category=category,
            service_digest=_digest(service),
            source_binding_digest=binding.digest,
        )
        _scope(value)
        return PollScope(_canonical(value), binding)
    except Exception:
        _error()


def _validate_run(run, scope):
    value = scope._value()
    # Rebuild authority without knowing or exposing the raw service.
    from .media_config import FILENAME, read_config

    run.session._local_guard()
    profile = config.profile_path()
    saved = read_config(profile / FILENAME)
    if (
        profile.resolve() != run.session.profile
        or _digest(str(profile.resolve())) != value["profile_digest"]
        or run.session.account_id != value["account_id"]
        or saved is None
        or config.settings_fingerprint(saved) != value["authority_fingerprint"]
        or run.writer.destination_ids.get(value["category"]) != value["destination_id"]
    ):
        _error()
    expected = dict(saved)
    if run._check_only:
        expected["preview_only"] = True
    if run.session.settings != expected or run.ctx.config != expected:
        _error()


def read_cursor(run, scope):
    try:
        _validate_run(run, scope)
        missing = object()
        value = run.ctx.kv_get(scope.key, missing)
        if value is missing:
            return None
        _canonical(value)
        if (
            not isinstance(value, dict)
            or set(value) != {"version", "scope", "watermark"}
            or type(value["version"]) is not int
            or value["version"] != 1
        ):
            _error()
        _scope(value["scope"])
        if value["scope"] != scope._value() or not isinstance(value["watermark"], str):
            _error()
        return _utc(datetime.fromisoformat(value["watermark"].replace("Z", "+00:00")))
    except Exception:
        _error()


def resolve_since(explicit, saved, overlap_hours):
    try:
        if (
            type(overlap_hours) not in (int, float)
            or not math.isfinite(overlap_hours)
            or overlap_hours < 0
        ):
            raise ValueError
        if saved is not None:
            saved = _utc(saved)
        if explicit is not None:
            if not isinstance(explicit, datetime):
                raise ValueError
            return _utc(
                explicit.replace(tzinfo=timezone.utc) if explicit.tzinfo is None else explicit
            )
        return saved - timedelta(hours=overlap_hours) if saved is not None else None
    except Exception:
        raise ValueError(SELECTION_ERROR) from None


@dataclass(frozen=True)
class CursorDecision:
    state: str
    reason: str
    _candidate: datetime | None = field(default=None, repr=False)
    _prior: datetime | None = field(default=None, repr=False)
    _scope_key: str | None = field(default=None, repr=False)
    _committed: bool = field(default=False, repr=False)

    @property
    def new_watermark(self):
        return self._candidate.isoformat() if self._committed and self.state == "advanced" else None

    def public(self):
        if self.state == "advanced" and not self._committed:
            _error()
        return {"state": self.state, "reason": self.reason}


def decide_progress(scope, prior, events, report, result, preview):
    """Pure decision only; counts are necessary, never proof of provenance."""
    if (
        not isinstance(report, ScanReport)
        or not report.started
        or not report.finished
        or report._phase != "finished"
    ):
        scan_fail()
    try:
        if type(preview) is not bool:
            _error()
        if not isinstance(result, ImportResult) or not isinstance(events, list):
            _error()
        counters = (result.total, result.skipped_existing, result.posted, result.verified)
        if any(type(v) is not int or v < 0 for v in counters) or result.total != len(events):
            _error()
        if not preview and (
            result.posted != result.verified
            or result.total != result.skipped_existing + result.verified
        ):
            _error()
        value = scope._value()
        if prior is not None:
            prior = _utc(prior)
        starts = []
        for event in events:
            if (
                event.category != value["category"]
                or event.importer != value["importer"]
                or _digest(event.service) != value["service_digest"]
            ):
                _error()
            starts.append(_utc(event.start_time))
        reason = (
            "preview"
            if preview
            else "page-cap"
            if report.configured_page_cap
            else "entry-cap"
            if report.configured_entry_cap
            else "normalization-skips"
            if report.malformed_rows or report.enrichment_errors
            else "no-events"
            if not starts
            else "no-increase"
            if prior is not None and max(starts) <= prior
            else "complete"
        )
        state = (
            "advanced"
            if reason == "complete"
            else "unchanged"
            if reason in ("no-events", "no-increase")
            else "withheld"
        )
        return CursorDecision(
            state, reason, max(starts) if reason == "complete" else None, prior, scope.key
        )
    except Exception:
        _error()


def commit_progress(run, scope, decision, source_unchanged=None):
    """Commit sampled source consent and local authority immediately before KV.

    Exceptions preserve the caller's actual writer result; never replace counts
    after delivery. A failed KV acknowledgement is unknown until the next read.
    """
    try:
        _validate_run(run, scope)
        if decision._scope_key != scope.key:
            _error()
        if decision.reason == "preview":
            if not run._check_only:
                _error()
            return decision
        if run._check_only or decision._committed:
            _error()
        envelope = None
        if decision.state == "advanced":
            candidate = _utc(decision._candidate)
            envelope = {"version": 1, "scope": scope._value(), "watermark": candidate.isoformat()}
            _canonical(envelope)

        def persist():
            if decision.state == "advanced" and read_cursor(run, scope) != decision._prior:
                _error()
            try:
                unchanged = scope._binding.unchanged() is True and (
                    source_unchanged is None or source_unchanged() is True
                )
            except Exception:
                unchanged = False
            if not unchanged:
                return replace(decision, state="withheld", reason="source-changed", _candidate=None)
            if decision.state != "advanced":
                return decision
            run.session._local_guard()
            run.ctx.kv_set(scope.key, envelope)
            return replace(decision, _committed=True)

        return run.session.commit(persist)
    except Exception:
        _error()
