"""Five Collect poll routes' strict scan and private progress authority.

This adapter uses Collect's captured session, never standalone CLI authority.
A finite returned scan and writer-accounted selected coverage permit progress;
unknown source/cursor state always retains prior evidence.
"""
from copy import deepcopy
from datetime import datetime, timezone
from functools import wraps
import hashlib
import json

from fulcra_collect import credentials
from fulcra_common.client import ImportResult

from .importers.poll_scan import ScanReport

PREFIX = 'v2_daemon_media_poll_cursor:'
MAX_CURSOR_BYTES = 4096


def _canonical(value):
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode()
        if len(encoded) > MAX_CURSOR_BYTES:
            raise ValueError
        return encoded
    except Exception:
        raise RuntimeError('daemon poll cursor state unavailable') from None


def _stamp(value):
    if type(value) is not str:
        raise RuntimeError('daemon poll cursor state unavailable')
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None or parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
            raise ValueError
        if parsed.isoformat() != value:
            raise ValueError
        return parsed
    except Exception:
        raise RuntimeError('daemon poll cursor state unavailable') from None


def _cap(value):
    # Collect's TOML reader retains Integer wrappers. Normalize only that
    # known persisted representation, never strings/bools or captured settings.
    from tomlkit.items import Integer
    if type(value) is Integer:
        value = value.unwrap()
    if value is not None and (type(value) is not int or value <= 0):
        raise ValueError('poll cap must be a positive integer or None')
    return value


class DaemonPoll:
    def __init__(self, ctx, session, writer, preview):
        self.ctx, self.session, self.writer, self.preview = ctx, session, writer, preview
        self.report = ScanReport()
        self.scope = self.key = self.prior = self.secret = self.secret_key = None
        self.result = None

    @property
    def strict(self):
        return dict(strict_scan=True, scan_report=self.report)

    def select(self, category, service, identity, *, secret_key=None):
        """After recovery, bind the exact canonical selection actually consumed."""
        from .collect_v1 import _cursor_scope
        if self.scope is not None:
            raise RuntimeError('daemon poll selection already captured')
        if not all(isinstance(v, str) and v for v in (category, service, *identity)):
            raise ValueError('daemon poll selection unavailable')
        secret = self.ctx.credentials.get(secret_key) if secret_key else None
        if secret_key and (type(secret) is not str or not secret):
            raise ValueError('daemon poll source credential unavailable')
        self.secret, self.secret_key = secret, secret_key
        destinations = self.writer.prepare((category,))
        self.scope = {**_cursor_scope(self.ctx, self.session), 'importer': self.ctx.plugin_id,
            'category': category, 'destination': destinations[category],
            'service_digest': hashlib.sha256(service.encode()).hexdigest(),
            'selection_digest': hashlib.sha256(_canonical([service, *identity])).hexdigest(),
            'credential_digest': hashlib.sha256(_canonical(secret)).hexdigest()}
        self.scope = deepcopy(self.scope)
        self.key = PREFIX + hashlib.sha256(_canonical(self.scope)).hexdigest()
        self.prior = self.read()
        self.ctx.state.watermark = self.prior.isoformat() if self.prior else None
        self.max_entries = _cap(self.ctx.config.get('max_entries'))
        self.max_pages = _cap(self.ctx.config.get('max_pages'))
        self.report.configured_entry_cap = self.max_entries is not None
        self.report.configured_page_cap = self.max_pages is not None

    def read(self):
        missing = object()
        value = self.ctx.kv_get(self.key, missing)
        if value is missing:
            return None
        _canonical(value)
        if (type(value) is not dict or set(value) != {'version', 'scope', 'watermark'}
                or type(value['version']) is not int or value['version'] != 2
                or type(value['scope']) is not dict or value['scope'] != self.scope
                or _canonical(value['scope']) != _canonical(self.scope)):
            raise RuntimeError('daemon poll cursor state unavailable')
        return _stamp(value['watermark'])

    def diagnostic(self, state, reason):
        self.ctx.progress(stage='cursor', cursor_state=state, cursor_reason=reason)

    def deliver(self, events, label):
        """Materialized current selection and its immediate actual result stay paired."""
        from .plugins._common import report_import
        if self.scope is None or not self.report.started or not self.report.finished or self.report._phase != 'finished':
            raise RuntimeError('source scan failed')
        self.ctx.progress(stage='fetched', count=len(events))
        self.result = result = self.writer.run_import(events, check_only=self.preview)
        if not isinstance(result, ImportResult):
            self.diagnostic('unknown', 'current-result-accounting')
            raise RuntimeError('daemon poll selected coverage unavailable')
        counters = (result.total, result.skipped_existing, result.posted, result.verified)
        if (any(type(n) is not int or n < 0 for n in counters)
                or result.total != len(events) or (not self.preview and
                    (result.posted != result.verified or result.total != result.skipped_existing + result.verified))):
            self.diagnostic('unknown', 'current-result-accounting')
            raise RuntimeError('daemon poll selected coverage unavailable')
        report_import(self.ctx, result, label)
        reason = ('preview' if self.preview else 'page-cap' if self.report.configured_page_cap
            else 'entry-cap' if self.report.configured_entry_cap else 'normalization-skips'
            if self.report.malformed_rows or self.report.enrichment_errors else None)
        if reason:
            self.diagnostic('withheld', reason)
            return result
        starts = []
        for event in events:
            if (event.importer != self.scope['importer'] or event.category != self.scope['category']
                    or hashlib.sha256(event.service.encode()).hexdigest() != self.scope['service_digest']
                    or event.start_time.tzinfo is None or event.start_time.utcoffset() is None):
                raise RuntimeError('daemon poll selected coverage unavailable')
            starts.append(event.start_time.astimezone(timezone.utc))
        candidate = max(starts) if starts else None
        if candidate is None or (self.prior is not None and candidate <= self.prior):
            self.diagnostic('unchanged', 'no-events' if candidate is None else 'no-increase')
            return result
        envelope = {'version': 2, 'scope': deepcopy(self.scope), 'watermark': candidate.isoformat()}
        expected = _canonical(envelope)
        def persist():
            if self.read() != self.prior:
                raise RuntimeError('daemon poll cursor state unavailable')
            if self.secret_key:
                try:
                    fresh = credentials.get_secret(self.ctx.plugin_id, self.secret_key)
                    unchanged = type(fresh) is str and bool(fresh) and fresh == self.secret
                except Exception:
                    unchanged = False
                if not unchanged:
                    self.diagnostic('withheld', 'source-credential-changed-or-unavailable')
                    return False
            # commit's first guard preceded the potentially slow fresh secret read.
            self.session.guard(mutating=True, require_enabled=True)
            try:
                self.ctx.kv_set(self.key, envelope)
            except Exception:
                # An ambiguous acknowledgement can still be proven by this exact read.
                if _canonical(self.ctx.kv_get(self.key, None)) != expected:
                    raise RuntimeError('daemon poll cursor state unavailable') from None
            if _canonical(self.ctx.kv_get(self.key, None)) != expected:
                raise RuntimeError('daemon poll cursor state unavailable')
            return True
        try:
            advanced = self.session.commit(persist)
        except Exception:
            self.diagnostic('unknown', 'cursor-authority-or-readback')
            raise RuntimeError('daemon poll cursor state unavailable') from None
        if advanced:
            self.diagnostic('advanced', 'complete-returned-scan')
        return result


def daemon_poll(function):
    """One progress owner; recover before resolving sources, restore worker state."""
    @wraps(function)
    def run(ctx):
        from . import collect_v1
        legacy = ctx.state.watermark
        try:
            with collect_v1.CollectRecordSession(ctx) as session:
                preview = False
                for key in ('dry_run', 'preview_only'):
                    value = ctx.config.get(key, False)
                    if type(value) is not bool:
                        raise ValueError('Preview only must be on or off')
                    preview |= value
                writer = collect_v1.MediaRecordWriter(ctx, session)
                ctx._media_record_writer = writer
                ctx._daemon_poll = DaemonPoll(ctx, session, writer, preview)
                if not preview:
                    writer.resume()
                return function(ctx)
        except Exception:
            poll = getattr(ctx, '_daemon_poll', None)
            if poll is not None and poll.result is None:
                poll.diagnostic('unknown', 'source-or-cursor-unavailable')
            raise
        finally:
            ctx.state.watermark = legacy
            for key in ('_media_record_writer', '_daemon_poll'):
                if hasattr(ctx, key):
                    delattr(ctx, key)
    return run
