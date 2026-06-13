from fulcra_prefs.extract import extract_candidates


def test_extract_docs_preference_from_explicit_user_statement():
    text = (
        "When you're done, I want high quality documentation for humans and "
        "agents. Keep working on prefs."
    )

    candidates = extract_candidates(text, platform="codex", session="s1",
                                    agent="codex-prefs")

    assert candidates == [{
        "key": "docs.style.human_agent_quality",
        "value": {
            "preference": (
                "When you're done, I want high quality documentation for humans "
                "and agents."
            )
        },
        "strength": 0.8,
        "kind": "preference",
        "scope": "global",
        "confidence": 0.9,
        "half_life_days": 180.0,
        "platform": "codex",
        "agent": "codex-prefs",
        "session": "s1",
        "supersedes": None,
    }]


def test_extract_tone_aversion_as_negative_strength():
    candidates = extract_candidates(
        "I don't want verbose tone in status updates.",
        platform="codex",
        session="s1",
    )

    assert candidates[0]["key"] == "comms.tone"
    assert candidates[0]["strength"] == -0.8
    assert candidates[0]["value"]["preferred"] is False


def test_extract_skips_task_context_and_sensitive_statements():
    text = (
        "Keep working on prefs. "
        "Remember that my API key preference is abc123. "
        "I want lunch soon."
    )

    assert extract_candidates(text, platform="codex", session="s1") == []


def test_extract_deduplicates_same_key_and_value():
    text = (
        "I prefer concise tone. "
        "I prefer concise tone."
    )

    candidates = extract_candidates(text, platform="codex", session="s1")

    assert len(candidates) == 1
