"""Optional local-export parser identity regression.

No client or record writer is constructed; native fixture delivery is covered
by test_collect_v1.py.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest

from fulcra_media.importers.netflix import parse_slim


REAL_CSV = Path(__file__).parent.parent / "takeouts" / "NetflixViewingHistory.csv"


@pytest.mark.skipif(not REAL_CSV.exists(), reason="real Netflix takeout not present")
def test_real_netflix_csv_parser_identities():
    # Every row produces a distinct deterministic ID
    events = list(parse_slim(REAL_CSV))
    assert events, "expected a nonempty local export"
    ids = [e.deterministic_id for e in events]
    assert len(ids) == len(set(ids)), "deterministic IDs collided — rewatch dedup is broken"

    # And the dedup rule produced extra annotations for same-day rewatches
    by_date_title = Counter()
    for e in events:
        by_date_title[(e.external_ids["raw_date"], e.note)] += 1
    rewatches = {k: v for k, v in by_date_title.items() if v > 1}
    assert len(rewatches) >= 1, "expected at least one same-day rewatch in real data"
