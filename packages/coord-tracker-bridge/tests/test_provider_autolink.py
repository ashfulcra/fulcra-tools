"""Linear's autolink rewrite must not read as authored drift.

Found live on 2026-09-03: after a sync reported `applied: 3`, the very next plan
proposed the same 3 description updates. The bridge wrote `https://trendshift.io/...`
and Linear returned `[https://trendshift.io/...](<https://trendshift.io/...>)`, so
desired never equalled actual. `applied` was true and convergence was still false —
the surface measured that the write was accepted, not that it settled.
"""

from __future__ import annotations

from coord_tracker_bridge.linear import (
    append_source_metadata,
    normalize_provider_markdown,
    strip_source_metadata,
)
from coord_tracker_bridge.model import SourceIdentity

URL = "https://trendshift.io/repositories/1871"
SOURCE = SourceIdentity(provider="coord-engine", namespace="fulcra/tasks", item_id="t-0000dead")


def test_autolink_of_a_bare_url_collapses_back() -> None:
    assert normalize_provider_markdown(f"Ash found free-for-dev via [{URL}](<{URL}>) and") == (
        f"Ash found free-for-dev via {URL} and"
    )


def test_every_autolink_in_one_description_collapses() -> None:
    other = "https://example.com/x"
    got = normalize_provider_markdown(f"[{URL}](<{URL}>) then [{other}](<{other}>)")
    assert got == f"{URL} then {other}"


def test_source_file_citation_autolinked_with_an_inferred_scheme_collapses() -> None:
    """The form that actually bit: a coord description citing a .py file."""

    got = normalize_provider_markdown(
        "settle writers are caches inside pure build paths - "
        "[projection.py](<http://projection.py>) calls it"
    )
    assert got == "settle writers are caches inside pure build paths - projection.py calls it"


def test_file_and_line_citation_collapses() -> None:
    got = normalize_provider_markdown("[generation.py:263](<http://generation.py:263>): missing")
    assert got == "generation.py:263: missing"


def test_a_real_authored_link_is_left_alone() -> None:
    """Only label == target carries no information. A titled link does."""

    authored = f"see [the repo]({URL}) for detail"
    assert normalize_provider_markdown(authored) == authored


def test_bare_url_is_untouched() -> None:
    assert normalize_provider_markdown(f"plain {URL} here") == f"plain {URL} here"


def test_round_trip_through_linear_is_stable() -> None:
    """What we write, read back through the provider's rewrite, must compare equal."""

    authored = f"Ash found free-for-dev via {URL} and wants a sweep."
    written = append_source_metadata(authored, SOURCE, capability="tasks")
    # What Linear hands back: our footer intact, the bare URL linkified.
    returned = written.replace(URL, f"[{URL}](<{URL}>)", 1)

    assert strip_source_metadata(returned) == authored
