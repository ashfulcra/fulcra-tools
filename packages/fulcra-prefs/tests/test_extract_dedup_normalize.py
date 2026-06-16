"""Extraction dedup should ignore case and punctuation.

dedup keyed on the raw sentence text, so the same preference restated with
different casing or terminal punctuation produced duplicate candidates,
inflating n_signals.
"""
from fulcra_prefs.extract import extract_candidates


def test_dedup_is_case_and_punctuation_insensitive():
    c = extract_candidates("I prefer concise tone. I PREFER concise tone!",
                           platform="p", session="s")
    assert len(c) == 1


def test_distinct_preferences_not_over_deduped():
    c = extract_candidates("I want concise tone. I want verbose documentation.",
                           platform="p", session="s")
    assert {x["key"] for x in c} == {"comms.tone", "docs.style.documentation"}
