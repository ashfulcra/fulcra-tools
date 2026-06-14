"""Extraction must not leak PII and must stay conservative.

Two failure modes the base extractor had:
- it stored the entire raw sentence as the preference value while only
  blocking a small keyword set, so a sentence carrying a phone/email got that
  PII written into a shareable, injectable record;
- it claimed a preference from disavowed / reported / hypothetical speech
  ("I never said I want X", 'The user said "I want X"', "If I want X"),
  violating the module's conservative contract.

Negated *preference* verbs ("I don't want X") are NOT disavowals — they are
valid aversions and must still extract with negative strength.
"""
from fulcra_prefs.extract import extract_candidates


def test_skips_sentence_with_phone_pii():
    c = extract_candidates(
        "Remember that I want brief updates; my phone is 555-123-4567.",
        platform="p", session="s")
    assert all("555-123-4567" not in str(x["value"]) for x in c)


def test_skips_sentence_with_email_pii():
    c = extract_candidates(
        "I prefer concise tone, reach me at me@example.com.",
        platform="p", session="s")
    assert all("@" not in str(x["value"]) for x in c)


def test_skips_disavowed_never_said():
    assert extract_candidates(
        "I never said I want verbose tone.", platform="p", session="s") == []


def test_skips_do_not_assume():
    assert extract_candidates(
        "Do not assume I prefer concise tone.", platform="p", session="s") == []


def test_skips_reported_speech():
    assert extract_candidates(
        "The user said I want verbose tone.", platform="p", session="s") == []


def test_skips_hypothetical_if():
    assert extract_candidates(
        "If I want concise tone, use it.", platform="p", session="s") == []


def test_skips_embedded_hypothetical_request():
    assert extract_candidates(
        "Ask me if I want concise tone.", platform="p", session="s") == []


def test_skips_when_hypothetical():
    assert extract_candidates(
        "When I want concise tone, I will say so.", platform="p",
        session="s") == []


def test_skips_preference_question():
    assert extract_candidates(
        "Do I want concise tone?", platform="p", session="s") == []


def test_still_extracts_plain_preference():
    c = extract_candidates("I want concise tone.", platform="p", session="s")
    assert c and c[0]["key"] == "comms.tone" and c[0]["strength"] == 0.8


def test_still_extracts_aversion():
    c = extract_candidates("I don't want verbose tone.", platform="p", session="s")
    assert c and c[0]["strength"] == -0.8
