"""Static historical codec allowlist; optional domains load only on conversion.

Raw-only ledgers remain usable without their eventual converter installed.
Nothing in a plan can name an import path or discover a plugin entry point.
"""
_MEDIA_IDS = frozenset(f'collect.media.{category}.v{version}'
    for category in ('watched', 'listened', 'read', 'activity') for version in (1, 2))
_JOURNAL_ID = 'collect.journal.reader.v1'


def resolve_converter(plan):
    """Resolve and validate a supported plan before any conversion row work."""
    converter = plan.document['converter']
    if converter in _MEDIA_IDS:
        from .migration_convert import MediaHistoryConverter
        return MediaHistoryConverter(plan)
    if converter == _JOURNAL_ID:
        try:
            from fulcra_dayone.history_v1 import JournalHistoryConverter
        except ImportError:
            raise ValueError('historical Journal converter unavailable; install the optional Day One package') from None
        return JournalHistoryConverter(plan)
    raise ValueError('unsupported historical converter version')
