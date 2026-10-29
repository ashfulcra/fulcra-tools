"""Standalone strict poll orchestration; no daemon or legacy cursor coupling."""
from datetime import datetime
import re

from .cli_common import emit_result, import_result_to_dict, media_failure_envelope
from .importers.poll_scan import ScanReport
from .media_config import configured_media
from .poll_cursors import (
    capture_scope, commit_progress, decide_progress, read_cursor,
    read_source_credentials, resolve_since, source_binding,
)

_MESSAGES = {
    'args': 'poll arguments unavailable',
    'setup': 'configured media unavailable',
    'auth': 'source credentials unavailable',
    'fetch': 'source scan failed',
    'import': 'media import failed',
}


def run_poll(importer, state_path, *, since_iso, check_only, json_mode,
             max_pages=None, max_entries=None, overlap=1, **selection):
    """Recover before capture, materialize before delivery, publish after cleanup."""
    run = result = decision = since_str = None
    stage = 'args'
    try:
        if (max_pages is not None and not 1 <= max_pages <= 10000
                or max_entries is not None and max_entries <= 0 or overlap < 0):
            raise ValueError
        explicit = datetime.fromisoformat(since_iso.replace('Z', '+00:00')) if since_iso is not None else None
        category = selection.get('category', {'lastfm': 'listened', 'deezer': 'listened',
                                 'letterboxd': 'watched', 'goodreads': 'read'}.get(importer))
        service = selection.get('service', importer)
        if importer == 'letterboxd' and not re.fullmatch('[A-Za-z0-9_-]+', selection['username']):
            raise ValueError
        if importer == 'goodreads' and not re.fullmatch('[0-9]+', selection['user_id']):
            raise ValueError
        if importer == 'generic-rss' and (not selection['feed_url'] or not service or category not in ('watched', 'listened')):
            raise ValueError
        stage = 'setup'
        with configured_media(state_path, check_only=check_only) as run:
            credentials = None
            if importer in ('lastfm', 'deezer'):
                stage = 'auth'
                credentials = read_source_credentials(importer)
                binding = source_binding(importer, credentials=credentials)
            else:
                stage = 'args'
                binding = source_binding(importer, **selection)
            stage = 'import'
            scope = capture_scope(run, importer, category, service, binding)
            prior = read_cursor(run, scope)
            stage = 'args'
            since = resolve_since(explicit, prior, overlap)
            since_str = since.isoformat() if since else None
            report = ScanReport(configured_page_cap=max_pages is not None,
                                configured_entry_cap=max_entries is not None)
            stage = 'fetch'
            events = _scan(importer, credentials, selection, since, max_pages, report)
            if importer not in ('lastfm', 'deezer'):
                if since is not None:
                    events = [event for event in events if event.start_time >= since]
                if max_entries is not None:
                    events = events[:max_entries]
            stage = 'import'
            result = run.import_events(events)
            proposed = decide_progress(scope, prior, events, report, result, check_only)
            decision = commit_progress(run, scope, proposed)
        envelope = import_result_to_dict(
            importer, result, since_watermark=since_str,
            new_watermark=decision.new_watermark, cursor=decision.public(),
            would_post=result.posted if check_only else None,
        )
    except Exception:
        envelope = media_failure_envelope(
            importer, stage=stage, message=_MESSAGES[stage], run=run,
            result=result, check_only=check_only,
        )
        envelope.since_watermark = since_str
        if decision is not None:
            envelope.cursor = decision.public()
            envelope.new_watermark = decision.new_watermark
    emit_result(envelope, json_mode=json_mode)


def _scan(importer, credentials, selection, since, max_pages, report):
    strict = dict(scan_report=report, strict_scan=True)
    if importer == 'lastfm':
        from .importers import lastfm
        raw = list(lastfm.fetch_recent_tracks(credentials, since=since, max_pages=max_pages, **strict))
        return list(lastfm.normalize_history(raw, **strict))
    if importer == 'deezer':
        from .importers import deezer
        raw = list(deezer.fetch_history(credentials, since=since, max_pages=max_pages, **strict))
        return list(deezer.normalize_history(raw, **strict))
    if importer == 'generic-rss':
        from .importers import generic_rss
        return list(generic_rss.normalize_feed(**selection, **strict))
    if importer == 'letterboxd':
        from .importers import letterboxd
        return list(letterboxd.fetch_diary(selection['username'], **strict))
    from .importers import goodreads
    return list(goodreads.fetch_diary(selection['user_id'], **strict))
